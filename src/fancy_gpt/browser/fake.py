from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .base import BrowserResponse, BrowserTurn
from .errors import (
    BrowserCapacityError,
    BrowserNotAuthenticatedError,
    BrowserTurnTimeoutError,
)

FakeResponse = str | Callable[[BrowserTurn], str]


@dataclass
class FakeBrowserDriver:
    """Deterministic in-memory browser used by unit/integration tests.

    `responses` are consumed in submission order. A response may be plain text
    or a callable receiving the BrowserTurn, useful for embedding the generated
    request_id in a final structured report.
    """

    responses: list[FakeResponse]
    authenticated: bool = True
    max_concurrent_turns: int = 5
    name: str = "fake-chatgpt-web"
    started: bool = False
    events: list[tuple[str, str, str]] = field(default_factory=list)
    prompts: list[tuple[str, str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._responses = deque(self.responses)
        self._turn_counter = 0
        self._active: dict[str, BrowserTurn] = {}
        self._submitted: set[str] = set()

    def start(self) -> None:
        self.started = True
        self.events.append(("start", "", ""))

    def stop(self) -> None:
        self.started = False
        self._active.clear()
        self.events.append(("stop", "", ""))

    def health_check(self) -> None:
        if not self.started:
            raise RuntimeError("fake browser not started")
        if not self.authenticated:
            raise BrowserNotAuthenticatedError("fake ChatGPT profile is not authenticated")
        self.events.append(("health", "", ""))

    def begin_turn(
        self,
        *,
        request_id: str,
        stage: str,
        conversation_id: str | None = None,
        conversation_mode: str = "temporary",
    ) -> BrowserTurn:
        self.health_check()
        if len(self._active) >= self.max_concurrent_turns:
            raise BrowserCapacityError("browser turn capacity exhausted")
        self._turn_counter += 1
        turn = BrowserTurn(
            turn_id=f"fake-turn-{self._turn_counter}",
            request_id=request_id,
            stage=stage,
            conversation_id=conversation_id,
            conversation_mode=conversation_mode,
        )
        self._active[turn.turn_id] = turn
        self.events.append(("begin", request_id, stage))
        self.events.append(("conversation", conversation_mode, conversation_id or ""))
        return turn

    def submit(self, turn: BrowserTurn, prompt: str) -> None:
        if turn.turn_id not in self._active:
            raise RuntimeError("turn is not active")
        if turn.turn_id in self._submitted:
            raise RuntimeError("turn prompt already submitted")
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        self._submitted.add(turn.turn_id)
        self.prompts.append((turn.request_id, turn.stage, prompt))
        self.events.append(("submit", turn.request_id, turn.stage))

    def wait_for_response(self, turn: BrowserTurn, *, timeout_s: float) -> BrowserResponse:
        if turn.turn_id not in self._submitted:
            raise RuntimeError("turn has not been submitted")
        if timeout_s <= 0:
            raise BrowserTurnTimeoutError("timeout expired")
        if not self._responses:
            raise BrowserTurnTimeoutError("no scripted response available")
        scripted = self._responses.popleft()
        text = scripted(turn) if callable(scripted) else scripted
        self.events.append(("response", turn.request_id, turn.stage))
        resolved_conversation_id = turn.conversation_id
        if resolved_conversation_id is None and turn.conversation_mode == "persistent":
            resolved_conversation_id = f"fake-conv-{turn.request_id}"
        return BrowserResponse(
            turn_id=turn.turn_id,
            text=text,
            response_identity=f"fake-response-{self._turn_counter}",
            conversation_id=resolved_conversation_id,
        )

    def close_turn(self, turn: BrowserTurn) -> None:
        self._active.pop(turn.turn_id, None)
        self.events.append(("close", turn.request_id, turn.stage))
