from __future__ import annotations

import threading
from typing import Callable

import os
from pathlib import Path
from importlib.resources import files

from fancy_gpt.bridge.client import SiteHealthUnsupported
from fancy_gpt.extension_utils import adapter_build_id
from fancy_gpt.browser import BrowserDriver, BrowserPromptTooLargeError, BrowserUiDriftError
from fancy_gpt.models import AutomatedModelResponse, ModelRequest
from fancy_gpt.response_parser import parse_json_object
from fancy_gpt.stream_decoding import decode_stream


def _stale_side_hint() -> str:
    """Name both remedies, and never assert which side is stale.

    An earlier version concluded from "this process started before the package
    was last written" that the process must be running old code. It does not
    follow, and the first time it fired it was wrong: the build id is computed
    from files on disk at the moment it is asked, so a process holding older
    code still reports the current value whenever the id calculation itself has
    not changed. The extension was the stale side and the message sent the user
    to restart the runtime.

    That is the very failure this hint exists to prevent, so it states the fact
    it can establish and leaves the conclusion to whoever can see both sides.
    """
    hint = (
        "Re-export with 'fancy-gpt extension export-all <dir>' and reload the extension if the "
        "install is the newer side; restart this process (reconnect the MCP server, or restart "
        "the bridge) if it has been running since before the last install."
    )
    try:
        package = Path(str(files("fancy_gpt").joinpath("extension_utils.py")))
        installed_at = package.stat().st_mtime
        started_at = Path(f"/proc/{os.getpid()}").stat().st_mtime
    except Exception:
        return hint
    if installed_at > started_at:
        age = int((installed_at - started_at) / 60)
        return hint + (
            f" For what it is worth, this process started about {age} minutes before the installed "
            "package was last written, which makes a restart worth trying -- though a process can "
            "still report the current build id from files it re-reads."
        )
    return hint


def _best_reading(site: str, response) -> str:
    """The reply as the stream carried it, whenever the stream can be trusted.

    Both are read on every turn, and the page was preferred at first as the
    path with the longest history behind it. Measurement changed that. Across
    every turn where both were read, the stream either agreed with the page or
    was more faithful than it, and never worse:

    - A frozen page holds a fragment. A hidden document stops being painted, so
      the reply stops growing in the DOM while the response that carried it
      finished long ago.
    - A rendered page cannot round-trip markdown. The model writes ```json and
      `inline code`; the renderer turns those into elements, and the characters
      themselves are not in the document at all. Reading the page returns an
      answer with its backticks missing, on a turn that reports success -- which
      is how that went unnoticed until the two readings were compared.

    So a trustworthy decoding wins. Trustworthy is strict: everything placed,
    nothing unrecognised, the response seen through to its end. A decoder that
    is unsure changes nothing and the page answers exactly as before, because
    refusing costs one reading while guessing costs an answer that is quietly
    wrong, with nothing downstream able to tell.
    """
    page_text = response.text or ""
    diagnostics = response.diagnostics or {}
    decoded = decode_stream(
        site,
        captures=diagnostics.get("streams") or None,
        bodies=diagnostics.get("streamBodies") or None,
    )
    if decoded is None or not decoded.trustworthy:
        return page_text
    return decoded.text


def _parses(text: str) -> bool:
    try:
        parse_json_object(text)
        return True
    except ValueError:
        return False


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
        self.site_health_checked: set[str] = set()
        #: Bridge turn id of the turn currently in flight, so an out-of-band
        #: cancel can name it while execute() is still blocked.
        self.active_turn_id: str | None = None
        self.active_generation_epoch: int = 0
        self._pushed_progress_turn: str | None = None
        #: How partial output is being obtained, and why if it degraded.
        self.progress_mode: str = "none"
        self.progress_fallback_reason: str = ""

    def start(self) -> None:
        if not self._started:
            self.driver.start()
            try:
                self.driver.health_check()
            except Exception:
                self.driver.stop()
                raise
            self._started = True

    @staticmethod
    def _require_current_adapter(payload: dict) -> None:
        """Refuse to drive a browser running a different build of the adapter.

        The browser usually runs on another machine, so an extension that was
        never reloaded is indistinguishable from one that was -- until it fails
        somewhere unrelated, like a selector that this build no longer uses.
        Stopping here costs one second; the alternative costs a model turn and
        points at the wrong thing.
        """
        if os.getenv("FANCY_GPT_ALLOW_ADAPTER_DRIFT"):
            return
        expected = adapter_build_id()
        live = str(payload.get("build") or "")
        if live == expected:
            return
        running = f"build {live}" if live else "a build too old to report one"
        raise BrowserUiDriftError(
            f"the browser extension is running {running}, but this process expects {expected}. "
            + _stale_side_hint()
            + " Set FANCY_GPT_ALLOW_ADAPTER_DRIFT=1 to run anyway."
        )

    @staticmethod
    def _stale_side_hint() -> str:
        """Say which side is behind, when that can be established.

        Two ids that differ say only that. Either could be the stale one, and
        the guard fired in three different directions in a single day: a
        browser behind the install, a browser ahead of it, and a long-running
        process holding code from before the install it is reporting. Sending
        everyone to reload the extension is wrong in two of those three, and a
        message that points at the wrong side costs more than no message.

        A process older than the package it imported is a fact, so it is
        checked rather than guessed at.
        """
        return _stale_side_hint()

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
        site = str(request.metadata.get("site") or "chatgpt")
        site_health = getattr(self.driver, "site_health", None)
        if callable(site_health) and site not in self.site_health_checked:
            try:
                self._require_current_adapter(site_health(site, timeout_s=min(20.0, self.timeout_s)))
            except SiteHealthUnsupported:
                pass
            self.site_health_checked.add(site)
        turn = self.driver.begin_turn(
            request_id=request.request_id,
            stage=request.stage,
            conversation_id=request.metadata.get("conversation_id"),
            conversation_mode=request.metadata.get("conversation_mode", "temporary"),
            site=site,
            generation_epoch=int(request.metadata.get("generation_epoch", 0)),
        )
        self.active_turn_id = turn.turn_id
        self.active_generation_epoch = turn.generation_epoch
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
                raw_text=_best_reading(site, response),
                response_identity=response.response_identity,
                conversation_id=response.conversation_id,
                diagnostics=response.diagnostics,
            )
        finally:
            self.active_turn_id = None
            self.active_generation_epoch = 0
            if poller is not None:
                poller.stop()
            if self._pushed_progress_turn is not None:
                stop_watching = getattr(self.driver, "stop_watching_progress", None)
                if callable(stop_watching):
                    try:
                        stop_watching(self._pushed_progress_turn)
                    except Exception:
                        pass
                self._pushed_progress_turn = None
            self.driver.close_turn(turn)

    def cancel(self, turn_id: str, *, generation_epoch: int = 0, reason: str = "cancelled") -> bool:
        """Stop generation for an in-flight turn, if the driver supports it.

        Returns False when the driver has no cancel path, so callers can tell
        "refused" apart from "not supported" instead of assuming success.
        """
        cancel = getattr(self.driver, "cancel_turn", None)
        if not callable(cancel):
            return False
        try:
            return bool(cancel(turn_id, generation_epoch=generation_epoch, reason=reason))
        except Exception:
            return False

    def _start_progress_poller(
        self, turn_id: str, on_progress: Callable[[str], None] | None
    ) -> "_ProgressPoller | None":
        """Prefer a pushed feed; poll only when the driver cannot push.

        Polling puts a floor under streaming latency equal to its interval, so
        it is the fallback rather than the design.
        """
        if on_progress is None:
            return None
        watch = getattr(self.driver, "watch_progress", None)
        if callable(watch):
            try:
                if watch(turn_id, on_progress):
                    self._pushed_progress_turn = turn_id
                    self.progress_mode = "push"
                    return None
                self.progress_fallback_reason = "the bridge declined the progress subscription"
            except Exception as exc:
                # Falling back silently is how a broken push path hides as
                # merely slow streaming. Record why, then degrade.
                self.progress_fallback_reason = f"{type(exc).__name__}: {exc}"[:200]
        poll = getattr(self.driver, "poll_progress", None)
        if poll is None:
            return None
        self.progress_mode = "poll"
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
