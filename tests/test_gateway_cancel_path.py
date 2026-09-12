"""Cancellation reaches the browser instead of abandoning the turn."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from fancy_gpt.gateway import CancelToken, GatewayCancelled, GatewayService, normalize_openai
from fancy_gpt.models import AutomatedModelResponse
from fancy_gpt.providers.web_automation import ChatGPTWebAutomationProvider
from fancy_gpt.tunnels.models import TunnelHealth, TunnelHealthState, TunnelSelection


class CancellableProvider:
    """Stands in for the browser: stops when told, like a stop-button click."""

    name = "fake-web"

    def __init__(self) -> None:
        self.requests: list = []
        self.cancels: list[tuple[str, str]] = []
        self.active_turn_id: str | None = None
        self.stopped_early = False

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def cancel(self, turn_id: str, *, generation_epoch: int = 0, reason: str = "cancelled") -> bool:
        self.cancels.append((turn_id, reason))
        self._cancelled.set()
        return True

    def execute(self, request):
        self.requests.append(request)
        self.active_turn_id = f"bridge-{request.request_id}"
        self._cancelled = threading.Event()
        # Generation that ends early when the browser is told to stop.
        if self._cancelled.wait(timeout=3.0):
            self.stopped_early = True
            raise RuntimeError("generation stopped by user")
        self.active_turn_id = None
        return AutomatedModelResponse(
            request_id=request.request_id, stage="agent", provider=self.name,
            raw_text=json.dumps({"type": "message", "text": "gateway-ok"}),
            response_identity="r1", conversation_id="conversation-1",
        )

    _cancelled = threading.Event()


class Manager:
    def __init__(self, provider) -> None:
        self.model = provider

    def select(self, **_kwargs):
        return TunnelSelection(
            tunnel_id="edge-remote", reason="test", explicit=True,
            health=TunnelHealth(tunnel_id="edge-remote", state=TunnelHealthState.HEALTHY, detail="ok"),
        )

    def provider(self, _selection):
        return self.model


def test_cancellation_is_carried_into_the_browser(tmp_path: Path) -> None:
    provider = CancellableProvider()
    service = GatewayService(tmp_path, manager=Manager(provider))
    token = CancelToken()
    turn = normalize_openai({"model": "gemini-web", "input": "hello"}).model_copy(
        update={"session_id": "gw_cancel_browser"}
    )
    outcome: list[BaseException] = []

    def run() -> None:
        try:
            service.execute(turn, cancel_token=token)
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    # Let the turn reach the provider, then disconnect.
    deadline = time.monotonic() + 3
    while not provider.requests and time.monotonic() < deadline:
        time.sleep(0.01)
    token.cancel("client disconnected")
    worker.join(timeout=10)

    # The browser was told to stop, naming the in-flight bridge turn.
    assert provider.cancels, "cancellation never reached the provider"
    turn_id, reason = provider.cancels[0]
    assert turn_id.startswith("bridge-")
    assert reason == "client disconnected"
    assert provider.stopped_early is True
    assert outcome, "the turn should not have completed normally"
    # The lock is not left held.
    assert service.locks.active_keys() == []


def test_a_provider_without_cancel_support_is_not_an_error(tmp_path: Path) -> None:
    """A driver with no cancel path must degrade, not crash."""

    class Plain(CancellableProvider):
        cancel = None  # type: ignore[assignment]

    provider = Plain()
    service = GatewayService(tmp_path, manager=Manager(provider))
    token = CancelToken()
    token.cancel("client disconnected")
    turn = normalize_openai({"model": "gemini-web", "input": "hi"}).model_copy(update={"session_id": "gw_nocancel"})
    with pytest.raises(GatewayCancelled):
        service.execute(turn, cancel_token=token)


# -- provider level ----------------------------------------------------------


class DriverWithCancel:
    def __init__(self) -> None:
        self.cancelled: list[tuple[str, int, str]] = []

    def cancel_turn(self, turn_id: str, *, generation_epoch: int = 0, reason: str = "cancelled") -> bool:
        self.cancelled.append((turn_id, generation_epoch, reason))
        return True


class DriverWithoutCancel:
    pass


def test_provider_delegates_cancel_to_the_driver() -> None:
    driver = DriverWithCancel()
    provider = ChatGPTWebAutomationProvider(driver)  # type: ignore[arg-type]
    assert provider.cancel("bridge-1", generation_epoch=2, reason="client gone") is True
    assert driver.cancelled == [("bridge-1", 2, "client gone")]


def test_provider_reports_unsupported_rather_than_pretending() -> None:
    provider = ChatGPTWebAutomationProvider(DriverWithoutCancel())  # type: ignore[arg-type]
    assert provider.cancel("bridge-1") is False


def test_a_driver_that_raises_does_not_propagate() -> None:
    class Exploding:
        def cancel_turn(self, *_args, **_kwargs):
            raise RuntimeError("bridge unreachable")

    provider = ChatGPTWebAutomationProvider(Exploding())  # type: ignore[arg-type]
    assert provider.cancel("bridge-1") is False
