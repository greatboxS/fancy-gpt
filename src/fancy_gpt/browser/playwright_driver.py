from __future__ import annotations

from pathlib import Path

from fancy_gpt.web.runtime.browser_page import PlaywrightPageRuntime
from fancy_gpt.web.sites.chatgpt import ChatGPTSelectors
from fancy_gpt.web.sites.chatgpt_browser import ChatGPTSiteDriver


class PlaywrightChatGPTDriver(ChatGPTSiteDriver):
    """Compatibility composition: ChatGPT site adapter + Playwright runtime."""

    name = "playwright-chatgpt-web"

    def __init__(
        self,
        profile_dir: Path | str,
        *,
        browser_type: str = "chromium",
        headless: bool = False,
        max_concurrent_turns: int = 5,
        temporary_chat_url: str = "https://chatgpt.com/?temporary-chat=true",
        selectors: ChatGPTSelectors | None = None,
        stable_polls: int = 3,
        poll_interval_s: float = 0.5,
    ) -> None:
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        self.browser_type = browser_type
        self.headless = headless
        runtime = PlaywrightPageRuntime(self.profile_dir, browser_type=browser_type, headless=headless)
        super().__init__(
            runtime,
            max_concurrent_turns=max_concurrent_turns,
            temporary_chat_url=temporary_chat_url,
            selectors=selectors,
            stable_polls=stable_polls,
            poll_interval_s=poll_interval_s,
        )

    @staticmethod
    def _import_playwright():
        return PlaywrightPageRuntime._import_playwright()
