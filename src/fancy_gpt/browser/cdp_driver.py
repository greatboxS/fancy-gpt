from __future__ import annotations

from typing import Any

from fancy_gpt.web.runtime.browser_page import CDPPageRuntime
from fancy_gpt.web.sites.chatgpt import ChatGPTSelectors
from fancy_gpt.web.sites.chatgpt_browser import ChatGPTSiteDriver


class CDPChatGPTDriver(ChatGPTSiteDriver):
    """Compatibility composition: ChatGPT site adapter + attached CDP runtime."""

    name = "cdp-chatgpt-web"

    def __init__(
        self,
        endpoint: str,
        *,
        max_concurrent_turns: int = 5,
        temporary_chat_url: str = "https://chatgpt.com/?temporary-chat=true",
        selectors: ChatGPTSelectors | None = None,
        stable_polls: int = 3,
        poll_interval_s: float = 0.5,
        **_ignored: Any,
    ) -> None:
        self.endpoint = endpoint
        runtime = CDPPageRuntime(endpoint)
        super().__init__(
            runtime,
            max_concurrent_turns=max_concurrent_turns,
            temporary_chat_url=temporary_chat_url,
            selectors=selectors,
            stable_polls=stable_polls,
            poll_interval_s=poll_interval_s,
        )
