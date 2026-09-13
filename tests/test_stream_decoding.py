"""Reading the reply out of the stream the site sent.

These exist because the alternative is silence: a decoder that mishandles this
encoding does not crash, it returns text that reads correctly and is missing
pieces. Every case here is a way that happens.
"""

from __future__ import annotations

import json

from fancy_gpt.stream_decoding import decode_chatgpt_stream, decode_stream


def events(*items) -> list[str]:
    return [item if isinstance(item, str) else json.dumps(item) for item in items]


OPENING = {
    "message": {"id": "m1", "content": {"content_type": "text", "parts": [""]}, "status": "in_progress"},
    "conversation_id": "c1",
}


def finished(*deltas) -> list[str]:
    return events(
        OPENING,
        *deltas,
        {"p": "/message/status", "o": "replace", "v": "finished_successfully"},
        {"p": "/message/end_turn", "o": "replace", "v": True},
        "[DONE]",
    )


def test_a_bare_value_continues_the_previous_path() -> None:
    """The rule a decoder written from assumption gets wrong.

    Only the first delta names its path; the rest carry a value alone. Treating
    those as unplaceable loses all but the first fragment, and the result still
    looks like a sentence.
    """
    reply = decode_chatgpt_stream(finished(
        {"p": "/message/content/parts/0", "o": "append", "v": "Hello"},
        {"v": ", world"},
        {"v": "!"},
    ))
    assert reply.text == "Hello, world!"
    assert reply.trustworthy


def test_a_patch_batch_applies_each_member() -> None:
    reply = decode_chatgpt_stream(finished(
        {"p": "/message/content/parts/0", "o": "append", "v": "a"},
        {"o": "patch", "v": [
            {"p": "/message/content/parts/0", "o": "append", "v": "b"},
            {"p": "/message/status", "o": "replace", "v": "in_progress"},
        ]},
    ))
    assert reply.text == "ab"


def test_a_bare_value_continues_the_operation_as_well_as_the_path() -> None:
    """Measured on a live turn, and the half that is easy to miss.

    The stream opens with {"o":"add","p":"","v":<snapshot>} and then sends two
    more snapshots as bare values -- same path, same operation, a counter
    apart. Continuing only the path appends a whole snapshot object where a
    string belongs; continuing neither throws the later snapshots away.
    """
    later = dict(OPENING)
    later["message"] = dict(OPENING["message"], id="m2")
    reply = decode_chatgpt_stream(events(
        {"c": 0, "o": "add", "p": "", "v": OPENING},
        {"c": 1, "v": later},
        {"p": "/message/content/parts/0", "o": "append", "v": "hello"},
        {"v": " there"},
        {"p": "/message/status", "o": "replace", "v": "finished_successfully"},
        {"p": "/message/end_turn", "o": "replace", "v": True},
        "[DONE]",
    ))
    assert reply.message_id == "m2", "the later snapshot replaces the earlier one"
    assert reply.text == "hello there"
    assert reply.trustworthy


def test_a_bare_value_with_nothing_to_continue_is_refused() -> None:
    """Refusing costs a fallback to the page; guessing costs a wrong reply."""
    reply = decode_chatgpt_stream(events(
        {"v": "orphaned"},
        {"p": "/message/status", "o": "replace", "v": "finished_successfully"},
        "[DONE]",
    ))
    assert "orphaned" not in reply.text
    assert not reply.trustworthy


def test_identity_comes_from_the_stream() -> None:
    # Which is how a turn binds to its conversation without reading the URL.
    reply = decode_chatgpt_stream(finished({"p": "/message/content/parts/0", "o": "append", "v": "x"}))
    assert (reply.conversation_id, reply.message_id) == ("c1", "m1")
    assert reply.status == "finished_successfully"
    assert reply.end_turn is True


def test_a_stream_that_never_ended_is_not_trusted() -> None:
    # No [DONE]: the tab may have been closed, or the reply may still be coming.
    reply = decode_chatgpt_stream(events(
        OPENING, {"p": "/message/content/parts/0", "o": "append", "v": "half"},
    ))
    assert reply.text == "half"
    assert not reply.trustworthy, "a truncated reply must not be usable as the answer"


def test_an_unknown_operation_makes_the_reply_untrusted() -> None:
    reply = decode_chatgpt_stream(finished(
        {"p": "/message/content/parts/0", "o": "append", "v": "kept"},
        {"p": "/message/content/parts/0", "o": "splice", "v": "???"},
    ))
    assert "splice" in reply.unknown_ops
    assert not reply.trustworthy, "a site that changed its encoding must not be guessed at"


def test_a_dropped_text_fragment_is_counted_separately() -> None:
    # Metadata nobody can place changes nothing; a piece of the reply does.
    reply = decode_chatgpt_stream(finished(
        {"p": "/message/content/parts/0", "o": "append", "v": "kept"},
        {"p": "/message/metadata/unplaceable/deep", "o": "append", "v": "ignored"},
    ))
    assert reply.skipped_text == 0
    assert reply.text == "kept"


def test_malformed_events_do_not_stop_the_decode() -> None:
    reply = decode_chatgpt_stream(finished(
        {"p": "/message/content/parts/0", "o": "append", "v": "a"},
        "not json at all",
        {"v": "b"},
    ))
    assert reply.text == "ab"


def test_an_unmeasured_site_has_no_decoder_rather_than_a_guessed_one() -> None:
    assert decode_stream("gemini", ["{}"]) is None
    assert decode_stream("chatgpt", ["[DONE]"]) is not None
