"""The re-ask mechanism every stage shares.

These pin the two properties that matter more than the retry itself: that a
reply is never edited into shape, and that a stage which cannot be parsed
eventually fails instead of looping.
"""

from __future__ import annotations

import pytest

from fancy_gpt.models import ModelRequest
from fancy_gpt.response_parser import parse_json_object
from fancy_gpt.stage_json import parse_or_reask


class Reply:
    def __init__(self, raw_text: str, conversation_id: str | None = "conv-1") -> None:
        self.raw_text = raw_text
        self.conversation_id = conversation_id


class ScriptedProvider:
    """Answers with the next scripted reply and records what it was asked."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.metadata: list[dict] = []

    def execute(self, request, on_progress=None):
        self.prompts.append(request.prompt)
        self.metadata.append(dict(request.metadata))
        return Reply(self.replies.pop(0))


def a_request() -> ModelRequest:
    return ModelRequest(
        request_id="req-1",
        stage="planner",
        title="t",
        prompt="original question",
        response_schema={},
        metadata={"site": "chatgpt", "conversation_mode": "temporary"},
    )


# The exact shape that ended a 26KB two-turn run: one unescaped quote.
BAD = '{"tags": ["world: "MAIN""], "ok": true}'
GOOD = '{"tags": ["world: MAIN"], "ok": true}'


def test_a_reply_that_parses_is_returned_untouched() -> None:
    provider = ScriptedProvider()
    value, reply = parse_or_reask(provider, a_request(), Reply(GOOD), parse_json_object)
    assert value == {"tags": ["world: MAIN"], "ok": True}
    assert reply.raw_text == GOOD
    assert provider.prompts == [], "a reply that parses must not cost another turn"


def test_an_unparseable_reply_is_re_asked_in_the_same_conversation() -> None:
    provider = ScriptedProvider(GOOD)
    value, reply = parse_or_reask(provider, a_request(), Reply(BAD), parse_json_object)
    assert value["ok"] is True
    assert reply.raw_text == GOOD
    assert len(provider.prompts) == 1
    # The model can only correct its own words if it can still see them.
    assert provider.metadata[0]["conversation_id"] == "conv-1"
    assert provider.metadata[0]["conversation_mode"] == "persistent"
    # The complaint is quoted back rather than left for the model to guess at.
    assert "could not be parsed" in provider.prompts[0]


def test_the_reply_is_never_edited_into_shape() -> None:
    """Data integrity: a value that parses because we changed it is worse than
    one that does not parse, because it is the one that gets acted on."""
    provider = ScriptedProvider(GOOD)
    _, reply = parse_or_reask(provider, a_request(), Reply(BAD), parse_json_object)
    assert reply.raw_text == GOOD, "the parsed value must come from the model, not from a repair"


def test_it_gives_up_rather_than_looping() -> None:
    provider = ScriptedProvider(BAD, BAD)
    with pytest.raises(ValueError, match="re-ask"):
        parse_or_reask(provider, a_request(), Reply(BAD), parse_json_object, attempts=2)
    assert len(provider.prompts) == 2, "bounded: every attempt is a real browser turn"


def test_every_attempt_is_kept_for_diagnosis() -> None:
    seen: list[tuple[str, int]] = []
    provider = ScriptedProvider(GOOD)
    parse_or_reask(
        provider,
        a_request(),
        Reply(BAD),
        parse_json_object,
        on_response=lambda reply, attempt: seen.append((reply.raw_text, attempt)),
    )
    assert seen == [(BAD, 0), (GOOD, 1)], "the failed reply must remain inspectable"


def test_a_turn_with_no_conversation_is_not_promoted_into_a_saved_one() -> None:
    """A fresh or temporary turn never reports a conversation id.

    Asking to continue one we do not have opens a brand new chat: the model
    would be corrected about a reply it has never seen, and a deliberately
    temporary turn would leave a saved thread behind. So the reply is quoted
    into the prompt instead, and the turn keeps the mode it was given.
    """
    provider = ScriptedProvider(GOOD)
    request = a_request()
    parse_or_reask(provider, request, Reply(BAD, conversation_id=None), parse_json_object)
    assert "conversation_id" not in provider.metadata[0] or not provider.metadata[0]["conversation_id"]
    assert provider.metadata[0]["conversation_mode"] == "temporary", "must not become a saved chat"
    assert BAD in provider.prompts[0], "the model must be shown the reply it has to correct"


def test_an_existing_conversation_is_continued_rather_than_quoted() -> None:
    provider = ScriptedProvider(GOOD)
    parse_or_reask(provider, a_request(), Reply(BAD, conversation_id="conv-9"), parse_json_object)
    assert provider.metadata[0]["conversation_id"] == "conv-9"
    assert provider.metadata[0]["conversation_mode"] == "persistent"
    assert BAD not in provider.prompts[0], "no need to resend what the chat already holds"


def test_a_reply_too_large_to_quote_is_still_re_asked() -> None:
    from fancy_gpt.stage_json import QUOTE_BACK_LIMIT

    huge = '{"a": "' + "x" * (QUOTE_BACK_LIMIT + 10) + '"'
    provider = ScriptedProvider(GOOD)
    parse_or_reask(provider, a_request(), Reply(huge, conversation_id=None), parse_json_object)
    assert len(provider.prompts[0]) < QUOTE_BACK_LIMIT, "an oversized prompt would fail the turn outright"


def test_zero_attempts_restores_the_old_strictness() -> None:
    provider = ScriptedProvider()
    with pytest.raises(ValueError):
        parse_or_reask(provider, a_request(), Reply(BAD), parse_json_object, attempts=0)
    assert provider.prompts == []
