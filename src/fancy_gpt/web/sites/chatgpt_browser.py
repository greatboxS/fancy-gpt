from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from fancy_gpt.browser.base import BrowserResponse, BrowserTurn
from fancy_gpt.browser.binding import ResponseBindingTracker
from fancy_gpt.browser.errors import (
    BrowserCapacityError,
    BrowserNotAuthenticatedError,
    BrowserTurnTimeoutError,
    BrowserUiDriftError,
)
from fancy_gpt.web.runtime.browser_page import PageRuntime

from .chatgpt import CHATGPT_SITE, ChatGPTSelectors


@dataclass
class _PageLease:
    page: Any
    baseline_turn_ids: set[str]
    submitted: bool = False


class ChatGPTSiteDriver:
    """ChatGPT site semantics composed with a site-neutral page runtime."""

    name = "chatgpt-site-driver"

    def __init__(
        self,
        runtime: PageRuntime,
        *,
        max_concurrent_turns: int = 5,
        temporary_chat_url: str | None = None,
        selectors: ChatGPTSelectors | None = None,
        stable_polls: int = 3,
        poll_interval_s: float = 0.5,
    ) -> None:
        self.runtime = runtime
        self.max_concurrent_turns = max_concurrent_turns
        self.temporary_chat_url = temporary_chat_url or CHATGPT_SITE.metadata["fresh_url"]
        self.selectors = selectors or ChatGPTSelectors()
        self.stable_polls = stable_polls
        self.poll_interval_s = poll_interval_s
        self._leases: dict[str, _PageLease] = {}

    def start(self) -> None:
        self.runtime.start()

    def stop(self) -> None:
        try:
            for turn_id in list(self._leases):
                lease = self._leases.pop(turn_id)
                try:
                    lease.page.close()
                except Exception:
                    pass
        finally:
            self.runtime.stop()

    @staticmethod
    def _first_visible(page: Any, selectors: tuple[str, ...]) -> Any | None:
        for selector in selectors:
            locator = page.locator(selector)
            try:
                if locator.count() == 1 and locator.is_visible():
                    return locator
            except Exception:
                continue
        return None

    def _wait_for_composer(self, page: Any, timeout_ms: int = 20_000) -> Any:
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            locator = self._first_visible(page, self.selectors.composer)
            if locator is not None:
                return locator
            time.sleep(0.2)
        raise BrowserNotAuthenticatedError(
            "ChatGPT composer did not become available; sign in inside the selected browser session"
        )

    def login(self, *, timeout_s: float = 300.0) -> None:
        page = self.runtime.new_page()
        try:
            page.goto(self.temporary_chat_url, wait_until="domcontentloaded", timeout=30_000)
            self._wait_for_composer(page, timeout_ms=int(timeout_s * 1000))
        finally:
            page.close()

    def health_check(self) -> None:
        page = self.runtime.new_page()
        try:
            page.goto(self.temporary_chat_url, wait_until="domcontentloaded", timeout=30_000)
            self._wait_for_composer(page)
        finally:
            page.close()

    def _turn_ids(self, page: Any) -> list[str]:
        try:
            values = page.locator(self.selectors.turn_container).evaluate_all(
                "els => els.map(e => e.getAttribute('data-turn-id')).filter(Boolean)"
            )
        except Exception as exc:
            raise BrowserUiDriftError("cannot inspect logical ChatGPT turn identities") from exc
        return [str(value) for value in values]

    def begin_turn(self, *, request_id: str, stage: str) -> BrowserTurn:
        if len(self._leases) >= self.max_concurrent_turns:
            raise BrowserCapacityError("maximum concurrent ChatGPT Web turns reached")
        page = self.runtime.new_page()
        try:
            page.goto(self.temporary_chat_url, wait_until="domcontentloaded", timeout=30_000)
            self._wait_for_composer(page)
            baseline = set(self._turn_ids(page))
        except Exception:
            page.close()
            raise
        turn = BrowserTurn(turn_id=uuid.uuid4().hex, request_id=request_id, stage=stage)
        self._leases[turn.turn_id] = _PageLease(page=page, baseline_turn_ids=baseline)
        return turn

    def submit(self, turn: BrowserTurn, prompt: str) -> None:
        lease = self._leases.get(turn.turn_id)
        if lease is None:
            raise RuntimeError("turn is not active")
        if lease.submitted:
            raise RuntimeError("prompt already submitted; automatic resend is forbidden")
        composer = self._wait_for_composer(lease.page)
        try:
            tag_name = composer.evaluate("el => el.tagName.toLowerCase()")
            if tag_name == "textarea":
                composer.fill(prompt)
            else:
                composer.click()
                composer.fill(prompt)
        except Exception as exc:
            raise BrowserUiDriftError("failed to populate ChatGPT composer") from exc
        send = self._first_visible(lease.page, self.selectors.send_button)
        if send is None:
            raise BrowserUiDriftError("unique ChatGPT send button not found")
        send.click()
        lease.submitted = True

    def _assistant_text_for_turn(self, page: Any, turn_id: str) -> str | None:
        turn = page.locator(f'{self.selectors.turn_container}[data-turn-id="{turn_id}"]')
        if turn.count() != 1:
            return None
        for selector in self.selectors.assistant_in_turn:
            try:
                if bool(turn.evaluate("(el, selector) => el.matches(selector)", selector)):
                    text = turn.inner_text().strip()
                    if text:
                        return text
                child = turn.locator(selector)
                if child.count() == 1:
                    text = child.inner_text().strip()
                    if text:
                        return text
            except Exception:
                continue
        return None

    def wait_for_response(self, turn: BrowserTurn, *, timeout_s: float) -> BrowserResponse:
        lease = self._leases.get(turn.turn_id)
        if lease is None or not lease.submitted:
            raise RuntimeError("turn is not submitted")
        deadline = time.monotonic() + timeout_s
        tracker = ResponseBindingTracker(
            baseline_ids=set(lease.baseline_turn_ids),
            stable_polls=self.stable_polls,
        )
        while time.monotonic() < deadline:
            all_ids = self._turn_ids(lease.page)
            assistant_candidates: list[tuple[str, str]] = []
            for candidate in all_ids:
                text = self._assistant_text_for_turn(lease.page, candidate)
                if text is not None:
                    assistant_candidates.append((candidate, text))
            stop_visible = self._first_visible(lease.page, self.selectors.stop_button) is not None
            completed = tracker.observe(assistant_candidates, stop_visible=stop_visible)
            if completed is not None:
                response_identity, text = completed
                return BrowserResponse(
                    turn_id=turn.turn_id,
                    text=text,
                    response_identity=response_identity,
                )
            time.sleep(self.poll_interval_s)
        raise BrowserTurnTimeoutError(f"ChatGPT Web response did not complete within {timeout_s:.1f}s")

    def close_turn(self, turn: BrowserTurn) -> None:
        lease = self._leases.pop(turn.turn_id, None)
        if lease is not None:
            lease.page.close()
