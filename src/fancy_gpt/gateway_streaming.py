"""Turn cumulative web-UI snapshots into append-only text deltas.

The browser gives us the assistant's *whole* reply so far, repeatedly, scraped
from a rendered page. Vendor streaming protocols want the opposite: small
append-only deltas that can never be taken back. Converting between the two is
where the sharp edges are.

Three rules, each answering a way the naive version is wrong:

* **Resolve by path, not by substring.** The reply is one JSON envelope, so the
  streamable prose is the string at root ``["text"]``. A ``"text"`` key nested
  inside tool-call arguments must be ignored - matching it by substring is the
  same class of bug as identifying tool calls by regex over rendered prose.
* **Never emit a character that could still change.** Incomplete escapes,
  unpaired surrogates, and the newest characters are held back, because the page
  re-renders while it streams.
* **Monotonic prefix lock.** Every snapshot must start with exactly what was
  already emitted. Holdback alone cannot absorb a rewrite that crosses the
  emitted frontier, so when that happens the stream fails loudly rather than
  emitting text that contradicts what the client already has.
"""

from __future__ import annotations

from dataclasses import dataclass

class DeltaStreamError(RuntimeError):
    """A snapshot contradicted text that was already sent to the client."""

    def __init__(self, emitted: str, snapshot_text: str, detail: str = "") -> None:
        common = 0
        for index, (left, right) in enumerate(zip(emitted, snapshot_text)):
            if left != right:
                break
            common = index + 1
        super().__init__(
            detail
            or (
                "streamed text was rewritten past the point already sent to the client "
                f"(diverged at character {common} of {len(emitted)} emitted)"
            )
        )
        self.emitted = emitted
        self.snapshot_text = snapshot_text
        self.diverged_at = common


#: Characters withheld from the end of an unfinished value. The page re-renders
#: as it streams (markdown, code fences), and the newest characters are the ones
#: most likely to be rewritten.
DEFAULT_HOLDBACK = 24


def find_envelope_start(snapshot: str) -> int:
    """Index of the JSON envelope, skipping prose or a ``` fence before it.

    Returns -1 when no object has started yet. Anchoring on the first ``{``
    keeps character offsets stable even when the model writes a preamble that
    the UI later reflows.
    """
    fence = snapshot.find("```")
    if fence != -1:
        newline = snapshot.find("\n", fence)
        if newline != -1:
            nested = snapshot.find("{", newline)
            if nested != -1:
                return nested
    return snapshot.find("{")


@dataclass
class _Scan:
    raw: str
    closed: bool
    found: bool
    #: A second root-level "text" key. JSON allows duplicates and json.loads
    #: keeps the last, while a linear scan necessarily emits the first, so the
    #: two disagree and already-sent text cannot be retracted.
    duplicate: bool = False


def scan_root_text(snapshot: str) -> _Scan:
    """Extract the raw (still escaped) value of root ``["text"]``.

    Walks the envelope tracking depth and string state, so only a key at depth 1
    counts. Everything nested - notably tool-call arguments that may carry their
    own ``"text"`` - is skipped structurally rather than matched textually.
    """
    start = find_envelope_start(snapshot)
    if start < 0:
        return _Scan("", False, False)

    index = start
    length = len(snapshot)
    depth = 0
    # Whether the key we just read at depth 1 was exactly "text".
    pending_key: str | None = None
    awaiting_value = False
    found: _Scan | None = None

    def read_string(position: int) -> tuple[str, int, bool]:
        """Read a JSON string starting at the opening quote."""
        cursor = position + 1
        chunk: list[str] = []
        while cursor < length:
            char = snapshot[cursor]
            if char == "\\":
                if cursor + 1 >= length:
                    return "".join(chunk) + "\\", cursor + 1, False
                chunk.append(char)
                chunk.append(snapshot[cursor + 1])
                cursor += 2
                continue
            if char == '"':
                return "".join(chunk), cursor + 1, True
            chunk.append(char)
            cursor += 1
        return "".join(chunk), cursor, False

    while index < length:
        char = snapshot[index]
        if char == '"':
            raw, next_index, closed = read_string(index)
            if awaiting_value and depth == 1 and pending_key == "text":
                if found is not None:
                    return _Scan(found.raw, found.closed, True, duplicate=True)
                found = _Scan(raw, closed, True)
                if not closed:
                    # Nothing can follow an unterminated string.
                    return found
            if not awaiting_value and depth == 1 and closed:
                pending_key = _decode_complete(raw)
            awaiting_value = False
            index = next_index
            continue
        if char == "{":
            depth += 1
            awaiting_value = False
            pending_key = None
            index += 1
            continue
        if char == "[":
            depth += 1
            awaiting_value = False
            index += 1
            continue
        if char in "}]":
            depth -= 1
            awaiting_value = False
            pending_key = None
            index += 1
            continue
        if char == ":":
            awaiting_value = True
            index += 1
            continue
        if char == ",":
            awaiting_value = False
            pending_key = None
            index += 1
            continue
        index += 1
    return found or _Scan("", False, False)


_SIMPLE_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _decode_complete(raw: str) -> str:
    decoded, _ = decode_prefix(raw, complete=True)
    return decoded


def decode_prefix(raw: str, *, complete: bool) -> tuple[str, int]:
    """Decode as much of a JSON string body as is safe.

    Returns the decoded text and how many raw characters it consumed. Decoding
    stops before anything that could still change: a trailing backslash, a
    partial ``\\uXXXX``, or a high surrogate whose partner has not arrived.
    Python strings are code points, so only escape sequences can split a
    character; a literal emoji in the raw text is already whole.
    """
    out: list[str] = []
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= length:
            break  # dangling backslash: the escape is still arriving
        marker = raw[index + 1]
        if marker in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[marker])
            index += 2
            continue
        if marker != "u":
            # Not valid JSON, but the page may simply be mid-render.
            break
        if index + 6 > length:
            break  # partial \uXXXX
        hex_digits = raw[index + 2 : index + 6]
        try:
            code = int(hex_digits, 16)
        except ValueError:
            break
        if 0xD800 <= code <= 0xDBFF:
            # High surrogate: only emit once its low partner has arrived, so a
            # split emoji never reaches the client as half a character.
            if index + 12 > length or raw[index + 6 : index + 8] != "\\u":
                break
            try:
                low = int(raw[index + 8 : index + 12], 16)
            except ValueError:
                break
            if not 0xDC00 <= low <= 0xDFFF:
                break
            out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
            index += 12
            continue
        if 0xDC00 <= code <= 0xDFFF:
            break  # lone low surrogate: never emit it
        out.append(chr(code))
        index += 6
    text = "".join(out)
    # A scraped snapshot can end on a literal high surrogate, not only on an
    # escaped one. Emitting it would hand the client half a character.
    if text and 0xD800 <= ord(text[-1]) <= 0xDBFF:
        text = text[:-1]
    return text, index


def _align_after_rerender(emitted: str, decoded: str) -> int | None:
    """Locate the frontier again after a whitespace-only re-render.

    A rendered page collapses runs of whitespace and drops newlines as markdown
    settles, which changes text the client already has without changing what it
    says. Killing the stream for that would fail healthy turns, so whitespace is
    compared loosely while every other character must still match exactly.

    Returns the index in ``decoded`` just past the re-rendered equivalent of
    ``emitted``, or None when the difference is a real rewrite.
    """
    emitted_index = 0
    decoded_index = 0
    while emitted_index < len(emitted):
        if emitted[emitted_index].isspace():
            emitted_index += 1
            continue
        while decoded_index < len(decoded) and decoded[decoded_index].isspace():
            decoded_index += 1
        if decoded_index >= len(decoded) or decoded[decoded_index] != emitted[emitted_index]:
            return None
        emitted_index += 1
        decoded_index += 1
    return decoded_index


class TextDeltaStream:
    """Converts cumulative snapshots into append-only deltas for one turn."""

    def __init__(self, holdback: int = DEFAULT_HOLDBACK) -> None:
        self.holdback = holdback
        self.emitted = ""
        self.failed = False
        #: How often a whitespace-only re-render moved the frontier. Worth
        #: surfacing: a page doing this constantly is a scraping problem.
        self.realignments = 0
        self._last_decoded = ""

    def push(self, snapshot: str) -> str:
        """Feed a snapshot; return the newly emitted text (possibly empty).

        Raises :class:`DeltaStreamError` when the snapshot contradicts text the
        client already received.
        """
        scan = scan_root_text(snapshot)
        if not scan.found:
            return ""
        if scan.duplicate:
            self.failed = True
            raise DeltaStreamError(
                self.emitted, "",
                detail="the reply carries two root-level \"text\" keys, so the streamed value and "
                       "the authoritative parse would disagree and emitted text cannot be retracted",
            )
        decoded, _ = decode_prefix(scan.raw, complete=scan.closed)
        self._last_decoded = decoded

        frontier = len(self.emitted)
        if not decoded.startswith(self.emitted):
            realigned = _align_after_rerender(self.emitted, decoded)
            if realigned is None:
                self.failed = True
                raise DeltaStreamError(self.emitted, decoded)
            # Benign: the page re-rendered whitespace. The client keeps the
            # spacing it already has and continues from the right place.
            self.realignments += 1
            frontier = realigned

        # While the value is still open the newest characters may be rewritten,
        # so they are withheld until more text confirms them.
        safe_end = len(decoded) if scan.closed else max(0, len(decoded) - self.holdback)
        if safe_end <= frontier:
            return ""
        delta = decoded[frontier:safe_end]
        self.emitted = self.emitted + delta
        return delta

    def finish(self, final_text: str) -> str:
        """Reconcile against the authoritative final parse.

        Returns the remaining suffix to emit before the terminal event.
        """
        if final_text.startswith(self.emitted):
            remainder = final_text[len(self.emitted) :]
            self.emitted = final_text
            return remainder
        realigned = _align_after_rerender(self.emitted, final_text)
        if realigned is None:
            self.failed = True
            raise DeltaStreamError(self.emitted, final_text)
        self.realignments += 1
        remainder = final_text[realigned:]
        self.emitted = self.emitted + remainder
        return remainder
