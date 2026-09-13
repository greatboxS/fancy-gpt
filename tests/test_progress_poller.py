from __future__ import annotations

import time

from fancy_gpt.browser.base import BrowserResponse, BrowserTurn
from fancy_gpt.models import ModelRequest
from fancy_gpt.providers.web_automation import ChatGPTWebAutomationProvider


class _DriverWithProgress:
    name = "fake-with-progress"

    def __init__(self, progress_by_turn: dict[str, list[str]]) -> None:
        self._progress_by_turn = progress_by_turn
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def health_check(self) -> None:
        return None

    def begin_turn(
        self,
        *,
        request_id: str,
        stage: str,
        conversation_id: str | None = None,
        conversation_mode: str = "temporary",
        site=None,
        generation_epoch: int = 0,
    ) -> BrowserTurn:
        return BrowserTurn(turn_id=f"t-{stage}", request_id=request_id, stage=stage)

    def submit(self, turn: BrowserTurn, prompt: str) -> None:
        return None

    def wait_for_response(self, turn: BrowserTurn, *, timeout_s: float) -> BrowserResponse:
        time.sleep(0.35)
        return BrowserResponse(turn_id=turn.turn_id, text="final answer", response_identity="r1")

    def close_turn(self, turn: BrowserTurn) -> None:
        return None

    def poll_progress(self, turn_id: str) -> str | None:
        values = self._progress_by_turn.get(turn_id, [])
        return values.pop(0) if values else None


class _DriverWithoutProgress(_DriverWithProgress):
    poll_progress = None  # type: ignore[assignment]


def test_execute_streams_progress_via_on_progress_callback():
    driver = _DriverWithProgress({"t-final": ["par", "partial", "partial answer"]})
    provider = ChatGPTWebAutomationProvider(driver, timeout_s=5, progress_interval_s=0.05)
    provider.start()
    seen: list[str] = []
    try:
        response = provider.execute(
            ModelRequest(request_id="r1", stage="final", prompt="hi", title="t", response_schema={}),
            on_progress=seen.append,
        )
    finally:
        provider.stop()

    assert response.raw_text == "final answer"
    # The poller runs on a background thread every ~150ms in this test's
    # timing window (0.35s wait); it should have delivered at least one
    # distinct progress update before the final result.
    assert len(seen) >= 1
    assert all(isinstance(text, str) and text for text in seen)


def test_execute_without_on_progress_never_calls_driver_poll_progress():
    driver = _DriverWithProgress({"t-final": ["should-not-be-consumed"]})
    provider = ChatGPTWebAutomationProvider(driver, timeout_s=5)
    provider.start()
    try:
        provider.execute(ModelRequest(request_id="r1", stage="final", prompt="hi", title="t", response_schema={}))
    finally:
        provider.stop()

    assert driver._progress_by_turn["t-final"] == ["should-not-be-consumed"]


def test_execute_tolerates_driver_without_poll_progress():
    driver = _DriverWithoutProgress({})
    provider = ChatGPTWebAutomationProvider(driver, timeout_s=5)
    provider.start()
    try:
        response = provider.execute(
            ModelRequest(request_id="r1", stage="final", prompt="hi", title="t", response_schema={}),
            on_progress=lambda text: None,
        )
    finally:
        provider.stop()
    assert response.raw_text == "final answer"
