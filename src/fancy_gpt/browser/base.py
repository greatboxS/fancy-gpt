from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class BrowserTurn:
    """Opaque identity for one ChatGPT Web turn.

    The provider must never infer a response by display index. A driver owns the
    browser-specific identity and returns it to the provider as an opaque value.
    """

    turn_id: str
    request_id: str
    stage: str


@dataclass(frozen=True)
class BrowserResponse:
    turn_id: str
    text: str
    response_identity: str
    conversation_binding: str | None = None


class BrowserDriver(Protocol):
    """Minimal browser boundary used by ChatGPT Web automation.

    Implementations own authentication/profile state. Core fancy-gpt code never
    reads cookies, local storage, access tokens, or private ChatGPT endpoints.
    """

    name: str

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def health_check(self) -> None:
        """Raise if the owned browser surface is not ready/authenticated."""
        ...

    def begin_turn(self, *, request_id: str, stage: str) -> BrowserTurn:
        """Create/lease a fresh task-bound browser surface for a turn."""
        ...

    def submit(self, turn: BrowserTurn, prompt: str) -> None:
        """Submit exactly one prompt for the turn. Must not silently retry sends."""
        ...

    def wait_for_response(self, turn: BrowserTurn, *, timeout_s: float) -> BrowserResponse:
        """Wait until one uniquely-bound assistant response is complete."""
        ...

    def close_turn(self, turn: BrowserTurn) -> None:
        ...
