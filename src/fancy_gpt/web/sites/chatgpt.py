from __future__ import annotations

from dataclasses import dataclass

from .base import SiteContract


CHATGPT_SITE = SiteContract(
    id="chatgpt",
    hosts=("chatgpt.com",),
    supports_fresh_conversation=True,
    operations=("model.turn",),
    required_browser_features=("dom", "javascript", "persistent-auth"),
    metadata={"fresh_url": "https://chatgpt.com/?temporary-chat=true"},
)


@dataclass(frozen=True)
class ChatGPTSelectors:
    """Public DOM assumptions owned by the ChatGPT site layer only."""

    composer: tuple[str, ...] = (
        "#prompt-textarea",
        "textarea",
        '[contenteditable="true"]',
    )
    send_button: tuple[str, ...] = (
        'button[data-testid="send-button"]',
        'button[aria-label*="Send"]',
    )
    stop_button: tuple[str, ...] = (
        'button[data-testid="stop-button"]',
        'button[aria-label*="Stop"]',
    )
    turn_container: str = "[data-turn-id]"
    assistant_in_turn: tuple[str, ...] = (
        '[data-message-author-role="assistant"]',
        '[data-testid="conversation-turn-assistant"]',
    )
