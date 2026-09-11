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
    conversation_id: str | None = None
    conversation_mode: str = "temporary"


@dataclass(frozen=True)
class BrowserResponse:
    turn_id: str
    text: str
    response_identity: str
    conversation_id: str | None = None


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

    def begin_turn(
        self,
        *,
        request_id: str,
        stage: str,
        conversation_id: str | None = None,
        conversation_mode: str = "temporary",
    ) -> BrowserTurn:
        """Create/lease a task-bound browser surface for a turn.

        `conversation_id` set means continue that existing ChatGPT
        conversation instead of starting a fresh one. `conversation_mode`
        controls what a fresh conversation becomes when no id is given:
        "temporary" (default, ephemeral, not resumable) or "persistent"
        (saved to the account so its resulting id can be reused later).
        A driver that cannot support continuation may ignore both and
        always start a fresh temporary conversation.
        """
        ...

    def submit(self, turn: BrowserTurn, prompt: str) -> None:
        """Submit exactly one prompt for the turn. Must not silently retry sends."""
        ...

    def wait_for_response(self, turn: BrowserTurn, *, timeout_s: float) -> BrowserResponse:
        """Wait until one uniquely-bound assistant response is complete."""
        ...

    def close_turn(self, turn: BrowserTurn) -> None:
        ...
