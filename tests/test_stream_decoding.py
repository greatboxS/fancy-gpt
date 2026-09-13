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
    # Each site is decoded from the shape it actually produces, and a site with
    # no entry reads the page exactly as before.
    assert decode_stream("grok", events=["{}"]) is None
    assert decode_stream("grok", body="anything") is None
    assert decode_stream("chatgpt", events=["[DONE]"]) is not None


# -- Gemini ------------------------------------------------------------------
#
# Captured from a live turn. The framing was measured rather than assumed:
# a )]}' guard, then length-prefixed chunks of [["wrb.fr", null, "<json>"]],
# with the reply at [4][0][1][0] of the inner payload.

import pathlib

from fancy_gpt.response_parser import parse_json_object
from fancy_gpt.stream_decoding import decode_gemini_body

GEMINI_BODY = (pathlib.Path(__file__).parent / "fixtures" / "gemini_streamgenerate.txt").read_text(encoding="utf-8")


def test_gemini_reply_is_recovered_from_its_own_response() -> None:
    reply = decode_gemini_body(GEMINI_BODY)
    assert reply.trustworthy
    assert reply.conversation_id and reply.message_id
    assert parse_json_object(reply.text)["answer"].startswith("A hash collision occurs")


def test_gemini_chunks_are_snapshots_not_fragments() -> None:
    """Each chunk carries the answer as it stands.

    Concatenating them would repeat the reply several times over, which is a
    plausible-looking result and the reason this was measured rather than
    guessed at.
    """
    reply = decode_gemini_body(GEMINI_BODY)
    assert reply.text.count('"request_id"') == 1


def test_gemini_needs_no_sentinel_to_know_it_finished() -> None:
    # Unlike ChatGPT there is no [DONE]; the request ending is the turn ending,
    # so a body that arrived at all is a reply that completed.
    assert decode_gemini_body(GEMINI_BODY).saw_done is True


def test_gemini_framing_it_does_not_recognise_is_refused() -> None:
    reply = decode_gemini_body(")]}'\n\nnot a length line\n[[1]]\n")
    assert not reply.text
    assert not reply.trustworthy, "an unfamiliar body must not be improvised over"


def test_gemini_is_registered_for_the_site() -> None:
    from fancy_gpt.stream_decoding import decode_stream

    assert decode_stream("gemini", body=GEMINI_BODY) is not None
    # And not by the shape the other site produces.
    assert decode_stream("gemini", events=[GEMINI_BODY]) is None


# -- choosing which capture is the reply --------------------------------------


def test_the_reply_is_chosen_by_path_not_by_arriving_last() -> None:
    """A telemetry call that finished later used to stand in for the answer.

    The extension kept only its most recent capture, so a few hundred
    characters of batchexecute replaced eleven thousand characters of reply and
    the turn decoded an empty answer -- or reported no stream at all.
    """
    captures = [
        {"path": "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate", "text": GEMINI_BODY},
        {"path": "/_/BardChatUi/data/batchexecute", "text": ")]}'\n\n12\n[[\"wrb.fr\"]]\n"},
    ]
    reply = decode_stream("gemini", bodies=captures)
    assert reply is not None and reply.trustworthy
    assert parse_json_object(reply.text)["answer"].startswith("A hash collision occurs")


def test_the_largest_capture_is_the_fallback_not_the_rule() -> None:
    # With nothing matching the site's known path, size is a reasonable guess --
    # but only once the path has failed to decide, since "usually the biggest"
    # is exactly the assumption that let telemetry stand in for an answer.
    captures = [
        {"path": "/unknown/small", "text": "tiny"},
        {"path": "/unknown/large", "text": GEMINI_BODY},
    ]
    reply = decode_stream("gemini", bodies=captures)
    assert reply is not None and reply.trustworthy


def test_choosing_never_invents_a_decoder() -> None:
    captures = [{"path": "/whatever", "text": GEMINI_BODY}]
    assert decode_stream("grok", bodies=captures) is None


# -- which reading a turn returns ---------------------------------------------


class _Reply:
    def __init__(self, text, diagnostics):
        self.text = text
        self.diagnostics = diagnostics


def _captures(body: str) -> dict:
    return {"streamBodies": [{"path": "StreamGenerate", "text": body}]}


def test_a_page_reading_that_parses_keeps_the_turn() -> None:
    """A reading that parses is a complete envelope by the contract's own
    definition, and the page is the path with the longest history behind it."""
    from fancy_gpt.providers.web_automation import _best_reading

    page = '{"request_id": "x", "answer": "complete", "confidence": 1}'
    assert _best_reading("gemini", _Reply(page, _captures(GEMINI_BODY))) == page


def test_the_stream_takes_over_when_the_page_froze() -> None:
    """What a hidden document leaves behind: a fragment that will not parse,
    while the response that carried the answer finished long ago."""
    from fancy_gpt.providers.web_automation import _best_reading

    frozen = '{"request_id": "x", "ans'
    recovered = _best_reading("gemini", _Reply(frozen, _captures(GEMINI_BODY)))
    assert recovered != frozen
    assert parse_json_object(recovered)["answer"].startswith("A hash collision occurs")


def test_an_unsure_decoder_changes_nothing() -> None:
    # Refusing costs a turn read from the page exactly as before; guessing
    # would cost an answer that is quietly wrong.
    from fancy_gpt.providers.web_automation import _best_reading

    frozen = '{"request_id": "x", "ans'
    assert _best_reading("gemini", _Reply(frozen, _captures(")]}'\n\ngarbage\n"))) == frozen
    assert _best_reading("grok", _Reply(frozen, _captures(GEMINI_BODY))) == frozen
