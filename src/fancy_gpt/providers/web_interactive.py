from __future__ import annotations

import webbrowser
from pathlib import Path

from fancy_gpt.models import InteractionRequired, ModelRequest


class ChatGPTWebInteractiveProvider:
    """Human-in-the-loop ChatGPT Web provider.

    This provider deliberately does not treat ChatGPT Web as a private API. It
    only materializes a prompt bundle, optionally opens the public web UI, and
    returns an interaction-required record. Browser auth/session ownership stays
    entirely with the user's browser.
    """

    name = "chatgpt-web-interactive"

    def prepare(
        self,
        request: ModelRequest,
        prompt_file: Path,
        open_browser: bool = False,
    ) -> InteractionRequired:
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(request.prompt, encoding="utf-8")
        if open_browser:
            webbrowser.open("https://chatgpt.com/")
        return InteractionRequired(
            request_id=request.request_id,
            stage=request.stage,
            prompt_file=str(prompt_file),
            instructions=[
                "Open a fresh ChatGPT Web conversation using the Plus account.",
                f"Paste the complete prompt from {prompt_file}.",
                "Allow ChatGPT to use online research/search when the prompt requests it.",
                "Save the returned raw JSON exactly as produced; do not summarize it manually.",
                "Import that JSON back into fancy-gpt to continue the state machine.",
            ],
        )
