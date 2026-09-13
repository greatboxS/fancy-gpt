"""Where a verbatim block ends, and why that decides whether a file survives.

A block's contents are written into the repository verbatim. If the parser
cannot tell where the block stopped, whatever the model said next is written
in as if it were source.
"""

from __future__ import annotations

from fancy_gpt.response_parser import parse_json_object, parse_json_object_with_blocks


# What the adapter now produces for a rendered reply: markdown renders the
# fences away, and the adapter puts them back from the DOM, where the boundary
# was never ambiguous.
RENDERED_REPLY = "\n".join([
    "```json",
    '{"edit":{"new_ref":"E1-NEW"}}',
    "```",
    "```fancygpt:E1-NEW",
    "    def alpha():",
    "        return 2",
    "```",
    "Let me know if you want tests added.",
])


def test_a_closing_sentence_is_not_written_into_the_file() -> None:
    _, blocks = parse_json_object_with_blocks(RENDERED_REPLY)
    assert blocks["E1-NEW"] == "    def alpha():\n        return 2"
    assert "Let me know" not in blocks["E1-NEW"], "prose after the block is not source"


def test_leading_indentation_survives_byte_for_byte() -> None:
    _, blocks = parse_json_object_with_blocks(RENDERED_REPLY)
    # The contract matches an OLD block character for character, and re-indented
    # code is a refused edit at best and a wrong one at worst.
    assert blocks["E1-NEW"].startswith("    def alpha():")


def test_the_document_is_read_from_its_own_fence() -> None:
    payload, _ = parse_json_object_with_blocks(RENDERED_REPLY)
    assert payload == {"edit": {"new_ref": "E1-NEW"}}


def test_a_reply_the_model_wrapped_in_a_fence_still_parses() -> None:
    """Seen live: the model wrapped its whole reply in ```json.

    Rendered, that arrived as a bare `JSON` line above the document and parsed
    as neither JSON nor prose. With the fence restored it is an ordinary fenced
    reply again.
    """
    assert parse_json_object('```JSON\n{"a": 1}\n```') == {"a": 1}


def test_an_unfenced_reply_still_works() -> None:
    # Nothing about the restoration requires a fence to be there.
    assert parse_json_object('{"a": 1}') == {"a": 1}
