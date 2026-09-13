"""Read a reply out of the stream the site sent, rather than the page it drew.

A browser stops painting a document it is not showing, so a reply read from
the rendered page freezes part-written whenever the tab is not in front. The
network does not stop. The extension therefore captures the events verbatim
and this decodes them, which puts the part that changes when a site changes
where it can be corrected and tested without asking anyone to reload a
browser.

ChatGPT's encoding, measured on live turns and matching how it is described
publicly: an SSE stream that announces `delta_encoding` and then patches a
document. Each event carries an operation `o`, a JSON-pointer path `p` and a
value `v`; an event that carries only `v` continues the previous path. That
last rule is the one a decoder written from assumption gets wrong, and getting
it wrong yields text that reads correctly and is not what the model said.

Nothing here guesses. An operation it does not know is counted and the result
declares itself untrustworthy, because a reply that is quietly short is worse
than one that is obviously missing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


@dataclass
class DecodedReply:
    text: str = ""
    conversation_id: str | None = None
    message_id: str | None = None
    status: str | None = None
    end_turn: bool = False
    saw_done: bool = False
    applied: int = 0
    skipped: int = 0
    skipped_text: int = 0
    unknown_ops: set[str] = field(default_factory=set)
    skipped_paths: set[str] = field(default_factory=set)

    @property
    def trustworthy(self) -> bool:
        """Whether this reply may be used as the answer.

        Everything placed, nothing unrecognised, and the stream seen through to
        its end. Anything less and the page remains the source of truth: the
        point of decoding the stream is to stop losing text, so a decoder that
        might have lost some has no claim on the turn.
        """
        return (
            self.saw_done
            and not self.unknown_ops
            and self.skipped_text == 0
            and bool(self.text)
        )


_TEXT_PATH = "/content/parts"


def _segments(pointer: str) -> list[str]:
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]


def _shape(pointer: str) -> str:
    return "/".join("<n>" if part.isdigit() else part for part in pointer.split("/"))[:60]


class _Document:
    """The message being patched, and where each patch landed."""

    def __init__(self) -> None:
        self.root: dict[str, Any] = {}

    def place(self, pointer: str, create: bool) -> tuple[Any, str] | None:
        parts = _segments(pointer)
        if not parts:
            return None
        node: Any = self.root
        for part in parts[:-1]:
            if isinstance(node, list):
                if not part.isdigit() or int(part) >= len(node):
                    return None
                node = node[int(part)]
                continue
            if not isinstance(node, dict):
                return None
            if part not in node:
                if not create:
                    return None
                node[part] = {}
            node = node[part]
        return (node, parts[-1])


def decode_chatgpt_stream(events: Iterable[str]) -> DecodedReply:
    """Decode ChatGPT's delta_encoding v1 events into the reply they carry."""
    reply = DecodedReply()
    document = _Document()
    last_path: str | None = None
    last_op: str | None = None

    def note_skip(pointer: str | None) -> None:
        reply.skipped += 1
        shape = _shape(pointer or "")
        reply.skipped_paths.add(shape)
        if _TEXT_PATH in shape:
            reply.skipped_text += 1

    def apply(operation: str, pointer: str | None, value: Any) -> None:
        if pointer in ("", None):
            if operation in ("add", "replace") and isinstance(value, dict):
                document.root = value
                reply.applied += 1
                return
            note_skip(pointer)
            return
        placed = document.place(pointer, create=operation in ("add", "replace", "append"))
        if placed is None:
            note_skip(pointer)
            return
        # Checked before anything is written, and for every kind of target. An
        # earlier version tested this only for objects, so an operation it did
        # not know, aimed at an array, was quietly carried out as a replace --
        # the decoder guessing, which is the one thing it must never do.
        if operation not in ("append", "add", "replace"):
            reply.unknown_ops.add(operation[:24])
            note_skip(pointer)
            return
        node, key = placed
        if isinstance(node, list):
            if not key.isdigit():
                note_skip(pointer)
                return
            index = int(key)
            while len(node) <= index:
                node.append("")
            if operation == "append":
                node[index] = f"{node[index] or ''}{value if isinstance(value, str) else ''}"
            else:
                node[index] = value
            reply.applied += 1
            return
        if not isinstance(node, dict):
            note_skip(pointer)
            return
        if operation == "append":
            node[key] = f"{node.get(key) or ''}{value if isinstance(value, str) else ''}"
        else:
            node[key] = value
        reply.applied += 1

    def handle(entry: Any) -> None:
        nonlocal last_path, last_op
        if not isinstance(entry, dict):
            return
        operation = entry.get("o") if isinstance(entry.get("o"), str) else None
        if operation == "patch":
            members = entry.get("v")
            if isinstance(members, list):
                for member in members:
                    handle(member)
            return
        if isinstance(entry.get("p"), str):
            last_path = entry["p"]
        if operation:
            last_op = operation
            apply(operation, entry.get("p") if isinstance(entry.get("p"), str) else last_path, entry.get("v"))
            return
        if "v" in entry:
            # No operation and no path: the encoding continues both. Measured
            # on a live turn -- an opening `{"o":"add","p":"","v":<snapshot>}`
            # is followed by bare values carrying later snapshots, and an
            # `{"o":"append","p":"/message/content/parts/0"}` by bare values
            # carrying the rest of the text. Continuing only the path, as an
            # earlier version did, appends a whole snapshot object where a
            # string belongs.
            #
            # A batch does not set either, because which one it would leave
            # behind has not been measured, and a bare value with nothing to
            # continue is refused rather than placed by preference: refusing
            # costs a fallback to the page, guessing costs a reply that is
            # quietly wrong.
            if last_op is not None and last_path is not None:
                apply(last_op, last_path, entry["v"])
            else:
                note_skip(last_path)
                reply.skipped_text += 1

    for raw in events:
        if raw == "[DONE]":
            reply.saw_done = True
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and "message" in parsed and "o" not in parsed and "p" not in parsed:
            # The opening snapshot arrives as a whole conversation event.
            document.root = {"message": parsed["message"], "conversation_id": parsed.get("conversation_id")}
            reply.applied += 1
            continue
        handle(parsed)

    message = document.root.get("message") if isinstance(document.root, dict) else None
    if isinstance(message, dict):
        reply.message_id = message.get("id")
        reply.status = message.get("status")
        reply.end_turn = message.get("end_turn") is True
        content = message.get("content")
        if isinstance(content, dict):
            parts = content.get("parts")
            if isinstance(parts, list):
                reply.text = "".join(part for part in parts if isinstance(part, str))
    if isinstance(document.root, dict):
        reply.conversation_id = document.root.get("conversation_id") or reply.conversation_id
    return reply


def decode_gemini_body(body: str) -> DecodedReply:
    """Decode Gemini's StreamGenerate response into the reply it carries.

    Measured on live turns rather than taken from documentation. The body is
    Google's batchexecute framing: a `)]}'` anti-hijacking guard, then pairs of
    a length line and a chunk. Each chunk is `[["wrb.fr", null, "<json>"]]`
    whose third element is itself a JSON string, and inside that the reply sits
    at `[4][0][1][0]`.

    The chunks are cumulative snapshots, not fragments: each carries the answer
    as it stands, so the last one that has it wins and concatenating them would
    repeat the reply several times over.

    Unlike ChatGPT's stream there is no sentinel, and none is needed. This is
    read from a completed response, so arriving at all means the reply
    finished -- the request ending is the turn ending.
    """
    reply = DecodedReply(saw_done=True)
    text = body.lstrip()
    if text.startswith(")]}'"):
        text = text[4:]
    lines = text.split("\n")

    chunks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line:
            continue
        if not line.isdigit():
            # Not a length line: either the framing changed or this is a body
            # we were not built for. Either way, do not improvise.
            reply.skipped += 1
            reply.skipped_paths.add("<unframed>")
            continue
        if index < len(lines):
            chunks.append(lines[index])
            index += 1

    for chunk in chunks:
        try:
            outer = json.loads(chunk)
        except ValueError:
            reply.skipped += 1
            reply.skipped_paths.add("<chunk>")
            continue
        for entry in outer if isinstance(outer, list) else []:
            if not (isinstance(entry, list) and len(entry) > 2 and entry[0] == "wrb.fr"):
                continue
            try:
                payload = json.loads(entry[2]) if isinstance(entry[2], str) else None
            except ValueError:
                reply.skipped += 1
                reply.skipped_paths.add("<payload>")
                continue
            if not isinstance(payload, list):
                continue
            identity = payload[1] if len(payload) > 1 else None
            if isinstance(identity, list) and identity:
                reply.conversation_id = identity[0] or reply.conversation_id
                if len(identity) > 1:
                    reply.message_id = identity[1] or reply.message_id
            candidate = _gemini_candidate_text(payload)
            if candidate is not None:
                # Cumulative: the latest snapshot replaces the one before it.
                reply.text = candidate
                reply.applied += 1

    reply.end_turn = bool(reply.text)
    reply.status = "finished" if reply.text else None
    return reply


def _gemini_candidate_text(payload: list) -> str | None:
    """The reply, at the one place it was measured to be: [4][0][1][0].

    Returned as None rather than searched for elsewhere when it is absent. A
    decoder that goes looking finds something eventually, and what it finds is
    not necessarily the answer.
    """
    candidates = payload[4] if len(payload) > 4 else None
    if not isinstance(candidates, list) or not candidates:
        return None
    first = candidates[0]
    if not isinstance(first, list) or len(first) < 2:
        return None
    parts = first[1]
    if not isinstance(parts, list) or not parts:
        return None
    return parts[0] if isinstance(parts[0], str) else None


# What each site's capture looks like, because they genuinely differ: ChatGPT
# streams server-sent events and Gemini returns one response body that grew
# while it loaded. Naming the shape here keeps the difference visible instead
# of hiding it behind a decoder that quietly accepts either.
EVENT_DECODERS = {
    "chatgpt": decode_chatgpt_stream,
}
BODY_DECODERS = {
    "gemini": decode_gemini_body,
}

# Which request carries the reply, measured per site. The extension hands over
# everything it captured and the choosing happens here, because a page makes
# many requests and only the runtime knows which of them is an answer -- and
# because getting it wrong should be a fix in this repository rather than a
# reload in everyone's browser.
REPLY_PATHS = {
    "chatgpt": re.compile(r"/backend-api/[^/]*/?conversation$"),
    "gemini": re.compile(r"StreamGenerate"),
}


def _pick(site: str, captures: Sequence[dict] | None, field: str) -> Any:
    """The capture that looks like this site's reply, else the largest.

    Size is the fallback rather than the rule: a reply is usually the biggest
    thing a page received, but "usually" is how a telemetry call of a few
    hundred characters came to stand in for an answer of eleven thousand.
    """
    if not captures:
        return None
    pattern = REPLY_PATHS.get(site)
    if pattern is not None:
        matching = [c for c in captures if pattern.search(str(c.get("path") or ""))]
        if matching:
            captures = matching
    return max(captures, key=lambda c: len(c.get(field) or "")).get(field)


def decode_stream(
    site: str,
    *,
    events: Iterable[str] | None = None,
    body: str | None = None,
    captures: Sequence[dict] | None = None,
    bodies: Sequence[dict] | None = None,
) -> DecodedReply | None:
    """Decode whatever this site's capture is, or None if it has no decoder.

    A site absent from both tables is not broken. It has not been measured,
    and its turn reads the rendered page exactly as before.
    """
    if captures is not None:
        events = _pick(site, captures, "events") or None
    if bodies is not None:
        body = _pick(site, bodies, "text") or None
    if events is not None:
        decoder = EVENT_DECODERS.get(site)
        return decoder(list(events)) if decoder else None
    if body is not None:
        decoder = BODY_DECODERS.get(site)
        return decoder(body) if decoder else None
    return None
