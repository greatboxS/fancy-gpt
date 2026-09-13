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
from dataclasses import dataclass, field
from typing import Any, Iterable


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
        nonlocal last_path
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
            apply(operation, entry.get("p") if isinstance(entry.get("p"), str) else last_path, entry.get("v"))
            return
        if "v" in entry:
            # No operation: the encoding continues the previous path. Which
            # path that is after a patch batch has not been measured, so the
            # one case we are sure of is honoured and anything else is refused
            # rather than resolved by preference. Refusing costs a fallback to
            # the page; guessing costs a reply that is quietly wrong, and a
            # bare value is reply text often enough that losing one matters.
            if last_path is not None and _TEXT_PATH in last_path:
                apply("append", last_path, entry["v"])
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


DECODERS = {
    # One entry per site, added once that site's stream has been measured on a
    # live turn. A site with no entry is still observed; it simply has no
    # decoder yet, which is the honest state rather than a pattern widened
    # until it matches something.
    "chatgpt": decode_chatgpt_stream,
}


def decode_stream(site: str, events: Iterable[str]) -> DecodedReply | None:
    decoder = DECODERS.get(site)
    return decoder(list(events)) if decoder else None
