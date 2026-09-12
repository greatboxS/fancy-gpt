from __future__ import annotations

from dataclasses import dataclass

from .base import SiteContract


GEMINI_SITE = SiteContract(
    id="gemini",
    hosts=("gemini.google.com",),
    # Gemini has no equivalent of a not-saved chat that still yields a URL, so a
    # "fresh" conversation here is simply a new one in the account's history.
    supports_fresh_conversation=True,
    operations=("model.turn",),
    required_browser_features=("dom", "javascript", "persistent-auth"),
    metadata={"fresh_url": "https://gemini.google.com/app"},
)


@dataclass(frozen=True)
class GeminiSelectors:
    """Public DOM assumptions owned by the Gemini site layer only."""

    composer: tuple[str, ...] = (
        'rich-textarea div[contenteditable="true"]',
        'div[contenteditable="true"][role="textbox"]',
        "textarea",
    )
    send_button: tuple[str, ...] = (
        'button[aria-label*="Send" i]',
        "button.send-button",
    )
    stop_button: tuple[str, ...] = (
        'button[aria-label*="Stop" i]',
        "button.stop-button",
    )
    response_container: tuple[str, ...] = (
        "model-response",
        "message-content.model-response-text",
    )
