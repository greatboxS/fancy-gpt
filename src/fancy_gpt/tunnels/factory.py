from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

from fancy_gpt.bridge import BridgeBrowserDriver, load_or_create_token
from fancy_gpt.browser import CDPChatGPTDriver, PlaywrightChatGPTDriver
from fancy_gpt.browser.base import BrowserDriver
from fancy_gpt.runtime_paths import default_browser_profile, user_data_dir

from .models import TunnelSpec


DriverBuilder = Callable[[TunnelSpec], BrowserDriver]


class TunnelDriverFactory:
    """Constructs a browser driver from a validated tunnel composition.

    This is the only layer that maps abstract runtime/transport tuples to concrete
    browser-control implementations. ReviewEngine and MCP never branch on browser
    families or transport mechanisms.
    """

    def __init__(
        self,
        *,
        bridge_token_file: Path | None = None,
        headless: bool = True,
        timeout_s: float = 300.0,
        builders: dict[str, DriverBuilder] | None = None,
    ) -> None:
        self.bridge_token_file = bridge_token_file or (user_data_dir() / "bridge-token")
        self.headless = headless
        self.timeout_s = timeout_s
        self._builders: dict[str, DriverBuilder] = {
            "extension": self._extension,
            "playwright": self._playwright,
            "cdp": self._cdp,
        }
        if builders:
            self._builders.update(builders)

    @staticmethod
    def _env_key(tunnel_id: str, suffix: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", tunnel_id).upper().strip("_")
        return f"FANCY_GPT_TUNNEL_{normalized}_{suffix}"

    @classmethod
    def endpoint(cls, spec: TunnelSpec) -> str | None:
        per_tunnel = os.getenv(cls._env_key(spec.id, "ENDPOINT"))
        if per_tunnel:
            return per_tunnel
        if spec.runtime.value == "extension":
            return os.getenv("FANCY_GPT_BRIDGE_ENDPOINT", spec.endpoint or "ws://127.0.0.1:8765")
        if spec.runtime.value == "cdp":
            return os.getenv("FANCY_GPT_CDP_ENDPOINT", spec.endpoint or "http://127.0.0.1:9222")
        return spec.endpoint

    def token_file(self, spec: TunnelSpec) -> Path:
        per_tunnel = os.getenv(self._env_key(spec.id, "TOKEN_FILE"))
        if per_tunnel:
            return Path(per_tunnel).expanduser().resolve()
        configured = spec.config.get("token_file")
        if configured:
            return Path(str(configured)).expanduser().resolve()
        return self.bridge_token_file

    def _extension(self, spec: TunnelSpec) -> BrowserDriver:
        endpoint = self.endpoint(spec)
        if endpoint is None:
            raise RuntimeError(f"extension tunnel {spec.id} requires a bridge endpoint")
        token = load_or_create_token(self.token_file(spec))
        return BridgeBrowserDriver(endpoint, token, spec.id, site=spec.site, job_timeout_s=self.timeout_s)

    def _playwright(self, spec: TunnelSpec) -> BrowserDriver:
        browser_type = "firefox" if spec.browser == "firefox" else "chromium"
        suffix = "firefox" if browser_type == "firefox" else "chromium"
        profile = default_browser_profile().with_name(default_browser_profile().name + f"-{suffix}")
        return PlaywrightChatGPTDriver(profile, browser_type=browser_type, headless=self.headless)

    def _cdp(self, spec: TunnelSpec) -> BrowserDriver:
        endpoint = self.endpoint(spec)
        if endpoint is None:
            raise RuntimeError(f"CDP tunnel {spec.id} requires an endpoint")
        return CDPChatGPTDriver(endpoint, headless=False)

    def build(self, spec: TunnelSpec) -> BrowserDriver:
        try:
            builder = self._builders[spec.runtime.value]
        except KeyError as exc:
            raise RuntimeError(f"runtime {spec.runtime.value} has no automatic driver factory") from exc
        return builder(spec)
