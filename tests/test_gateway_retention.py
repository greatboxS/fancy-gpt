"""What the gateway keeps in memory, and for how long.

A gateway is a long-lived server process. Anything it files away per turn and
never releases grows for as long as it runs, so these pin the two structures
that did exactly that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fancy_gpt.gateway import CancelToken, GatewayCancelled, GatewayService, normalize_openai
from fancy_gpt.gateway_trace import TurnTrace
from fancy_gpt.tunnels.models import TunnelHealth, TunnelHealthState, TunnelSelection


class RefusingProvider:
    """Every turn fails, which is the path that used to leak."""

    name = "fake-web"
    active_turn_id = None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def execute(self, request, on_progress=None):
        raise RuntimeError("browser refused the turn")


class Manager:
    def __init__(self, provider) -> None:
        self._provider = provider

    def select(self, *, tunnel_id=None, policy="auto", require_automatic=True) -> TunnelSelection:
        return TunnelSelection(
            tunnel_id="fake-remote",
            health=TunnelHealth(tunnel_id="fake-remote", state=TunnelHealthState.HEALTHY, detail="ok"),
            policy=policy,
            considered=[],
        )

    def provider(self, selection) -> object:
        return self._provider


def a_turn(session: str):
    return normalize_openai({"model": "fancy-gemini", "input": "hello"}).model_copy(
        update={"session_id": session}
    )


def test_a_failed_turn_does_not_keep_its_delta_stream(tmp_path: Path) -> None:
    """The stream holds every character emitted so far.

    It used to be released only on the way out of a successful turn, so each
    cancelled, failed or rejected one left its text behind for the life of the
    server.
    """
    service = GatewayService(tmp_path, manager=Manager(RefusingProvider()))
    for index in range(5):
        with pytest.raises(Exception):
            service.execute(a_turn(f"gw_leak_{index}"), on_delta=lambda _text: None)
    assert service._delta_streams == {}, "a turn that did not succeed still has to give its stream back"


def test_traces_do_not_grow_without_bound(tmp_path: Path) -> None:
    service = GatewayService(tmp_path, manager=Manager(RefusingProvider()))
    for index in range(service.TRACE_CACHE_SIZE + 25):
        service._remember_trace(f"resp-{index}", TurnTrace(f"resp-{index}"))
    assert len(service._traces) == service.TRACE_CACHE_SIZE
    # The oldest go first, so a turn someone is still asking about survives.
    assert "resp-0" not in service._traces
    assert f"resp-{service.TRACE_CACHE_SIZE + 24}" in service._traces
