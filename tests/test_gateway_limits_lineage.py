"""The bound on tool loops, legal state transitions, and retry lineage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fancy_gpt.gateway import GatewayLimits, GatewayService, ToolLoopExhausted, classify_error, normalize_openai
from fancy_gpt.gateway_state import IllegalTransition, TurnRecord, TurnState
from fancy_gpt.models import AutomatedModelResponse
from fancy_gpt.tunnels.models import TunnelHealth, TunnelHealthState, TunnelSelection

TOOLS = [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}]


class Provider:
    name = "fake-web"

    def __init__(self, always_tool_call: bool = True) -> None:
        self.requests: list = []
        self.always_tool_call = always_tool_call

    def start(self) -> None: ...
    def stop(self) -> None: ...

    def execute(self, request):
        self.requests.append(request)
        envelope = (
            {"type": "tool_calls", "calls": [{"id": f"call_{len(self.requests)}", "name": "read_file", "arguments": {}}]}
            if self.always_tool_call
            else {"type": "message", "text": "done"}
        )
        return AutomatedModelResponse(
            request_id=request.request_id, stage="agent", provider=self.name,
            raw_text=json.dumps(envelope), response_identity="r", conversation_id="conversation-1",
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


def turn(session: str, text: str = "go"):
    return normalize_openai({"model": "fancy-gemini", "input": text, "tools": TOOLS}).model_copy(
        update={"session_id": session}
    )


# -- the tool loop is actually bounded ---------------------------------------


def test_an_endless_tool_loop_is_refused(tmp_path: Path) -> None:
    """Previously declared as a limit and never enforced."""
    provider = Provider(always_tool_call=True)
    service = GatewayService(tmp_path, manager=Manager(provider), limits=GatewayLimits(max_tool_loop_iterations=3))

    for _ in range(3):
        result = service.execute(turn("gw_loop"))
        assert result.tool_calls, "each turn asks for another tool call"

    with pytest.raises(ToolLoopExhausted) as excinfo:
        service.execute(turn("gw_loop"))
    assert excinfo.value.limit == 3
    # The refusal happens before anything reaches the browser.
    assert len(provider.requests) == 3


def test_a_final_answer_resets_the_loop(tmp_path: Path) -> None:
    provider = Provider(always_tool_call=True)
    service = GatewayService(tmp_path, manager=Manager(provider), limits=GatewayLimits(max_tool_loop_iterations=3))
    service.execute(turn("gw_reset"))
    service.execute(turn("gw_reset"))
    assert service.state.tool_loop_depth("gw_reset") == 2

    provider.always_tool_call = False
    service.execute(turn("gw_reset"))
    assert service.state.tool_loop_depth("gw_reset") == 0, "a text answer ends the loop"

    # The budget is available again.
    provider.always_tool_call = True
    service.execute(turn("gw_reset"))
    assert service.state.tool_loop_depth("gw_reset") == 1


def test_the_loop_bound_is_per_session(tmp_path: Path) -> None:
    provider = Provider(always_tool_call=True)
    service = GatewayService(tmp_path, manager=Manager(provider), limits=GatewayLimits(max_tool_loop_iterations=2))
    service.execute(turn("gw_a"))
    service.execute(turn("gw_a"))
    # A different session is unaffected by another's exhausted loop.
    assert service.execute(turn("gw_b")).tool_calls
    with pytest.raises(ToolLoopExhausted):
        service.execute(turn("gw_a"))


def test_tool_loop_exhaustion_maps_to_a_client_error() -> None:
    status, body, retry = classify_error(ToolLoopExhausted(32, 32), "openai")
    assert status == 409
    assert retry is None
    assert "tool loop" in json.dumps(body)


# -- transitions cannot lie --------------------------------------------------


def test_a_completed_turn_cannot_be_resurrected(tmp_path: Path) -> None:
    service = GatewayService(tmp_path, manager=Manager(Provider(False)))
    record = service.state.save_turn(
        TurnRecord(response_id="resp_x", session_id="gw", protocol="openai", model="fancy-gemini", site="gemini")
    )
    service.state.transition(record, TurnState.SUBMITTING)
    service.state.transition(record, TurnState.SUBMITTED)
    service.state.transition(record, TurnState.COMPLETED)

    with pytest.raises(IllegalTransition):
        service.state.transition(record, TurnState.SUBMITTING)
    assert service.state.load_turn("resp_x").state is TurnState.COMPLETED


def test_a_stale_copy_cannot_overwrite_a_finished_turn(tmp_path: Path) -> None:
    """The check reads disk, not the caller's possibly stale object."""
    service = GatewayService(tmp_path, manager=Manager(Provider(False)))
    record = service.state.save_turn(
        TurnRecord(response_id="resp_y", session_id="gw", protocol="openai", model="fancy-gemini", site="gemini")
    )
    stale = service.state.load_turn("resp_y")
    service.state.transition(record, TurnState.CANCELLED, "client went away")

    assert stale.state is TurnState.QUEUED, "this copy predates the cancellation"
    with pytest.raises(IllegalTransition):
        service.state.transition(stale, TurnState.SUBMITTING)


def test_an_uncertain_turn_can_still_be_resolved(tmp_path: Path) -> None:
    """Uncertain is not terminal: we may still learn what the browser did."""
    service = GatewayService(tmp_path, manager=Manager(Provider(False)))
    record = service.state.save_turn(
        TurnRecord(response_id="resp_z", session_id="gw", protocol="openai", model="fancy-gemini", site="gemini")
    )
    service.state.transition(record, TurnState.SUBMITTING)
    service.state.transition(record, TurnState.UNCERTAIN, "browser stopped responding")
    service.state.transition(record, TurnState.COMPLETED, "recovered the reply")
    assert service.state.load_turn("resp_z").state is TurnState.COMPLETED


# -- retry lineage -----------------------------------------------------------


def test_a_retry_is_a_new_record_linked_to_the_one_it_supersedes(tmp_path: Path) -> None:
    service = GatewayService(tmp_path, manager=Manager(Provider(False)))
    first = service.state.save_turn(
        TurnRecord(response_id="resp_1", session_id="gw", protocol="openai", model="fancy-gemini", site="gemini")
    )
    second = service.state.record_retry(first, "resp_2")
    third = service.state.record_retry(second, "resp_3")

    assert (second.attempt, second.retry_of) == (2, "resp_1")
    assert (third.attempt, third.retry_of) == (3, "resp_2")

    lineage = service.state.retry_lineage("resp_3")
    assert [item.response_id for item in lineage] == ["resp_1", "resp_2", "resp_3"]
    assert [item.attempt for item in lineage] == [1, 2, 3]


def test_lineage_of_a_first_attempt_is_just_itself(tmp_path: Path) -> None:
    service = GatewayService(tmp_path, manager=Manager(Provider(False)))
    service.state.save_turn(
        TurnRecord(response_id="resp_solo", session_id="gw", protocol="openai", model="fancy-gemini", site="gemini")
    )
    assert [item.response_id for item in service.state.retry_lineage("resp_solo")] == ["resp_solo"]


def test_retrying_after_a_clean_failure_records_the_lineage(tmp_path: Path) -> None:
    """A key reused after a pre-submit failure is a real second attempt."""
    provider = Provider(always_tool_call=False)
    service = GatewayService(tmp_path, manager=Manager(provider))

    class Broken(Provider):
        def execute(self, request):
            self.requests.append(request)
            return AutomatedModelResponse(
                request_id=request.request_id, stage="agent", provider=self.name,
                raw_text=json.dumps({"type": "nonsense"}), response_identity="r", conversation_id="conversation-1",
            )

    broken = Broken(False)
    service.manager = Manager(broken)
    with pytest.raises(ValueError):
        service.execute(turn("gw_retry", "x"), idempotency_key="k1")

    service.manager = Manager(provider)
    service.execute(turn("gw_retry", "x"), idempotency_key="k1")
    attempts = [item for item in service.state.session_turns("gw_retry") if item.attempt > 1]
    assert attempts, "the second use of the key must be recorded as an attempt"
    assert attempts[0].retry_of is not None


def test_inspection_exposes_state_transitions_and_lineage(tmp_path: Path) -> None:
    """Diagnosing a turn needs its history, not just its last state."""
    from fancy_gpt.engine import ReviewEngine

    service = GatewayService(tmp_path, manager=Manager(Provider(False)))
    result = service.execute(normalize_openai({"model": "fancy-gemini", "input": "hello"}))

    trace = ReviewEngine(tmp_path).request_trace(result.response_id)
    summary = trace["summary"]
    assert summary["state"] == "completed"
    assert summary["attempt"] == 1
    states = [item["state"] for item in summary["transitions"]]
    # The whole path is visible, not only where it ended up.
    assert states[0] == "queued"
    assert states[-1] == "completed"
    assert "submitting" in states and "submitted" in states
    assert [item["response_id"] for item in summary["retry_lineage"]] == [result.response_id]
