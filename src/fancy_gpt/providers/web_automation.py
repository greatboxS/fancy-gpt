from __future__ import annotations

import threading
from typing import Callable

from fancy_gpt.bridge.client import SiteHealthUnsupported
from fancy_gpt.browser import BrowserDriver, BrowserPromptTooLargeError
from fancy_gpt.models import AutomatedModelResponse, ModelRequest


class ChatGPTWebAutomationProvider:
    """Automatic ChatGPT Web provider behind a replaceable BrowserDriver.

    The provider owns orchestration only. Authentication/session persistence and
    all UI-specific behavior belong to the driver. This separation allows full
    unit/integration testing with FakeBrowserDriver without network access.
    """

    name = "chatgpt-web-automation"

    def __init__(
        self,
        driver: BrowserDriver,
        *,
        timeout_s: float = 300.0,
        max_prompt_chars: int = 300_000,
        tunnel_id: str | None = None,
        progress_interval_s: float = 1.5,
    ) -> None:
        self.driver = driver
        self.timeout_s = timeout_s
        self.max_prompt_chars = max_prompt_chars
        self.tunnel_id = tunnel_id
        self.progress_interval_s = progress_interval_s
        if tunnel_id:
            self.name = f"chatgpt-web-automation:{tunnel_id}"
        self._started = False
        self.site_health_checked: bool | None = None

    def start(self) -> None:
        if not self._started:
            self.driver.start()
            try:
                self.driver.health_check()
                site_health = getattr(self.driver, "site_health", None)
                if callable(site_health):
                    try:
                        site_health(timeout_s=min(20.0, self.timeout_s))
                    except SiteHealthUnsupported:
                        # An older extension cannot answer the readiness probe.
                        # That is exactly the pre-probe behaviour, so continue
                        # rather than making the whole runtime unusable until
                        # the browser-side bundle is reloaded.
                        self.site_health_checked = False
                    else:
                        self.site_health_checked = True
            except Exception:
                self.driver.stop()
                raise
            self._started = True

    def stop(self) -> None:
        if self._started:
            self.driver.stop()
            self._started = False

    def execute(
        self,
        request: ModelRequest,
        *,
        on_progress: Callable[[str], None] | None = None,
    ) -> AutomatedModelResponse:
        if not self._started:
            raise RuntimeError("automation provider is not started")
        if len(request.prompt) > self.max_prompt_chars:
            raise BrowserPromptTooLargeError(
                f"compiled prompt has {len(request.prompt)} characters; "
                f"configured browser limit is {self.max_prompt_chars}"
            )
        turn = self.driver.begin_turn(
            request_id=request.request_id,
            stage=request.stage,
            conversation_id=request.metadata.get("conversation_id"),
            conversation_mode=request.metadata.get("conversation_mode", "temporary"),
        )
        poller = self._start_progress_poller(turn.turn_id, on_progress)
        try:
            self.driver.submit(turn, request.prompt)
            response = self.driver.wait_for_response(turn, timeout_s=self.timeout_s)
            if response.turn_id != turn.turn_id:
                raise RuntimeError("browser response is not bound to the submitted turn")
            return AutomatedModelResponse(
                request_id=request.request_id,
                stage=request.stage,
                provider=self.name,
                raw_text=response.text,
                response_identity=response.response_identity,
                conversation_id=response.conversation_id,
            )
        finally:
            if poller is not None:
                poller.stop()
            self.driver.close_turn(turn)

    def _start_progress_poller(
        self, turn_id: str, on_progress: Callable[[str], None] | None
    ) -> "_ProgressPoller | None":
        poll = getattr(self.driver, "poll_progress", None)
        if on_progress is None or poll is None:
            return None
        return _ProgressPoller(lambda: poll(turn_id), on_progress, interval_s=self.progress_interval_s).start()

    def __enter__(self) -> "ChatGPTWebAutomationProvider":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.stop()


class _ProgressPoller:
    """Polls a driver's `poll_progress` on a daemon thread and forwards new text.

    Best-effort visibility only: any exception from the poll function or the
    callback is swallowed, since this must never affect the actual turn.
    """

    def __init__(self, poll: Callable[[], str | None], on_progress: Callable[[str], None], interval_s: float = 1.5) -> None:
        self._poll = poll
        self._on_progress = on_progress
        self._interval_s = interval_s
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "_ProgressPoller":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        last_text: str | None = None
        while not self._stop_event.wait(self._interval_s):
            try:
                text = self._poll()
            except Exception:
                continue
            if text and text != last_text:
                last_text = text
                try:
                    self._on_progress(text)
                except Exception:
                    pass
