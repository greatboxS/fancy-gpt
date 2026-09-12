"""Every turn must leave an explicit, safe record of what happened to it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fancy_gpt.browser_errors import (
    BrowserFailure,
    BrowserTurnError,
    classify,
    classify_exception,
    policy_for,
)
from fancy_gpt.gateway import GatewayService, normalize_openai
from fancy_gpt.gateway_trace import (
    FORBIDDEN_KEYS,
    MAX_EVENTS,
    TraceStage,
    TurnTrace,
    read_trace,
    sanitize,
    shape_of,
)
from fancy_gpt.models import AutomatedModelResponse
from fancy_gpt.tunnels.models import TunnelHealth, TunnelHealthState, TunnelSelection


class Provider:
    name = "fake-web"

    def __init__(self) -> None:
        self.requests: list = []
        self.fail_with: Exception | None = None

    def start(self) -> None: ...
    def stop(self) -> None: ...

    def execute(self, request):
        self.requests.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        return AutomatedModelResponse(
            request_id=request.request_id, stage="agent", provider=self.name,
            raw_text=json.dumps({"type": "message", "text": "gateway-ok"}),
            response_identity="r1", conversation_id="conversation-1",
        )


class Manager:
    def __init__(self, provider: Provider) -> None:
        self.model = provider

    def select(self, **_kwargs):
        return TunnelSelection(
            tunnel_id="edge-remote", reason="test", explicit=True,
            health=TunnelHealth(tunnel_id="edge-remote", state=TunnelHealthState.HEALTHY, detail="ok"),
        )

    def provider(self, _selection):
        return self.model


# -- the trace never carries content or credentials --------------------------


def test_content_and_urls_are_never_recorded() -> None:
    trace = TurnTrace("resp_x")
    trace.event(
        TraceStage.BROWSER, "submitted",
        prompt="THE SECRET PROMPT",
        text="assistant reply body",
        url="https://chatgpt.com/c/private-thread-id",
        authorization="Bearer abcdef",
        cookie="session=xyz",
        chars=42, site="chatgpt",
    )
    data = trace.events[0].data
    serialized = json.dumps(trace.as_dicts())
    for forbidden in ("THE SECRET PROMPT", "assistant reply body", "private-thread-id", "abcdef", "xyz"):
        assert forbidden not in serialized
    # The useful, safe facts survive.
    assert data == {"chars": 42, "site": "chatgpt"}


def test_forbidden_keys_are_dropped_at_any_depth() -> None:
    cleaned = sanitize({"outer": {"prompt": "secret", "kept": 1}, "token": "abc", "ok": True})
    assert cleaned == {"outer": {"kept": 1}, "ok": True}
    assert "prompt" in FORBIDDEN_KEYS and "url" in FORBIDDEN_KEYS


def test_shape_describes_content_without_storing_it() -> None:
    shape = shape_of("hello world")
    assert shape["present"] is True and shape["chars"] == 11
    assert "hello" not in json.dumps(shape)
    # The same text always yields the same digest, so two turns can be compared.
    assert shape_of("hello world")["digest"] == shape["digest"]
    assert shape_of(None) == {"present": False}


def test_tracing_never_raises_on_unserializable_input() -> None:
    trace = TurnTrace("resp_x")
    assert trace.event(TraceStage.ROUTE, "odd", value=object()) is not None
    assert trace.events[0].data["value"]


def test_the_trace_is_bounded() -> None:
    trace = TurnTrace("resp_x")
    for index in range(MAX_EVENTS + 25):
        trace.event(TraceStage.STREAM, "delta", chars=index)
    assert len(trace.events) <= MAX_EVENTS + 1
    assert trace.events[-1].event == "trace-truncated"


# -- a real turn leaves a usable trace ---------------------------------------


def test_a_completed_turn_records_every_stage(tmp_path: Path) -> None:
    service = GatewayService(tmp_path, manager=Manager(Provider()))
    result = service.execute(normalize_openai({"model": "gemini-web", "input": "hello"}))

    events = read_trace(service.requests.request_dir(result.response_id) / "trace.jsonl")
    stages = [event["stage"] for event in events]
    names = [event["event"] for event in events]

    assert "route" in stages and "correlation" in stages and "terminal" in stages
    assert "tunnel-selected" in names
    assert "completed" in names
    # Timing is recorded so a slow stage can be found later.
    assert all(isinstance(event["elapsed_ms"], int) for event in events)


def test_a_failed_turn_records_the_classified_reason(tmp_path: Path) -> None:
    provider = Provider()
    provider.fail_with = RuntimeError("You've reached your limit. Try again in 3 hours")
    service = GatewayService(tmp_path, manager=Manager(provider))

    turn = normalize_openai({"model": "gemini-web", "input": "hi"}).model_copy(update={"session_id": "gw_limit"})
    with pytest.raises(BrowserTurnError) as excinfo:
        service.execute(turn)

    assert excinfo.value.failure is BrowserFailure.RATE_LIMITED
    assert excinfo.value.retryable is True
    assert excinfo.value.needs_user_action is False

    directory = next(service.requests.requests_dir.glob("resp_*"))
    traced = [event for event in read_trace(directory / "trace.jsonl") if event["event"] == "failed"]
    assert traced and traced[0]["data"]["failure"] == "rate-limited"
    assert traced[0]["data"]["retryable"] is True


def test_the_engine_exposes_the_trace(tmp_path: Path) -> None:
    from fancy_gpt.engine import ReviewEngine

    service = GatewayService(tmp_path, manager=Manager(Provider()))
    result = service.execute(normalize_openai({"model": "gemini-web", "input": "hello"}))

    engine = ReviewEngine(tmp_path)
    trace = engine.request_trace(result.response_id)
    assert trace["summary"]["outcome"] == "completed"
    assert trace["summary"]["events"] > 0
    assert trace["events"]

    summary_only = engine.request_trace(result.response_id, summary_only=True)
    assert summary_only["events"] == []
    assert summary_only["summary"]["stage_reached_ms"]


def test_a_trace_request_id_cannot_escape_the_store(tmp_path: Path) -> None:
    from fancy_gpt.engine import ReviewEngine

    engine = ReviewEngine(tmp_path)
    with pytest.raises(ValueError):
        engine.request_trace("../../etc")


# -- browser failure classification ------------------------------------------


@pytest.mark.parametrize(
    "message, expected",
    [
        ("ChatGPT composer unavailable; sign in first", BrowserFailure.AUTH_REQUIRED),
        ("You've reached your limit for today", BrowserFailure.RATE_LIMITED),
        ("ambiguous ChatGPT response: multiple new assistant turns", BrowserFailure.RESPONSE_AMBIGUOUS),
        ("the browser extension is running build abc", BrowserFailure.ADAPTER_DRIFT),
        ("Extension context invalidated", BrowserFailure.WORKER_EVICTED),
        ("gateway model returned an invalid response envelope", BrowserFailure.MALFORMED_ENVELOPE),
        ("navigator reported offline", BrowserFailure.OFFLINE),
        ("nothing recognisable", BrowserFailure.UNKNOWN),
    ],
)
def test_failures_are_identified_specifically(message: str, expected: BrowserFailure) -> None:
    assert classify(message).failure is expected


def test_exception_type_identifies_a_timeout_whatever_the_wording() -> None:
    policy = classify_exception(TimeoutError("browser stopped responding after submit"))
    assert policy.failure is BrowserFailure.TIMEOUT
    assert policy.retryable is True


def test_a_specific_message_beats_a_generic_wrapper_type() -> None:
    """A usage limit reported through a TimeoutError is still a usage limit."""
    policy = classify_exception(TimeoutError("You've reached your limit; try again in 2 hours"))
    assert policy.failure is BrowserFailure.RATE_LIMITED


def test_an_extension_reported_code_is_trusted_over_the_wording() -> None:
    policy = classify("some vague text", reported="blocked-by-dialog")
    assert policy.failure is BrowserFailure.BLOCKED_BY_DIALOG
    assert policy.needs_user_action is True


def test_every_failure_has_a_policy_and_guidance() -> None:
    for failure in BrowserFailure:
        policy = policy_for(failure)
        assert policy.guidance
        assert 400 <= policy.http_status <= 599
        # Something that needs a human must not be advertised as retryable.
        if policy.needs_user_action and failure is not BrowserFailure.BLOCKED_BY_DIALOG:
            assert policy.retryable is False


# -- classified failures reach the client as the right status ----------------


@pytest.mark.parametrize(
    "message, protocol, status",
    [
        ("You've reached your limit", "openai", 429),
        ("You've reached your limit", "anthropic", 529),
        ("composer unavailable; sign in first", "openai", 401),
        ("the model declined this request", "gemini", 422),
        ("browser worker timed out", "openai", 504),
    ],
)
def test_browser_failures_map_to_protocol_status(message: str, protocol: str, status: int) -> None:
    from fancy_gpt.gateway import classify_error

    error = BrowserTurnError(message, classify(message))
    mapped, body, _retry = classify_error(error, protocol)
    assert mapped == status
    # The guidance travels with it, so the caller learns what to do.
    assert classify(message).guidance[:20] in json.dumps(body)


def test_a_rate_limit_carries_a_retry_hint() -> None:
    from fancy_gpt.gateway import classify_error

    error = BrowserTurnError("You've reached your limit", classify("You've reached your limit"))
    _status, _body, retry = classify_error(error, "openai")
    assert retry and retry >= 30


def test_a_failure_needing_a_human_carries_no_retry_hint() -> None:
    from fancy_gpt.gateway import classify_error

    error = BrowserTurnError("please sign in", classify("please sign in"))
    _status, _body, retry = classify_error(error, "openai")
    assert retry is None
