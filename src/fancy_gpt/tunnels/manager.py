from __future__ import annotations

import importlib.util
import time
import urllib.request
import importlib.metadata
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Any

from fancy_gpt.bridge import (
    BridgeBrowserDriver,
    available_tunnels,
    load_or_create_token,
    probe_bridge_stats,
    probe_bridge_workers,
)
from fancy_gpt.providers import ChatGPTWebAutomationProvider
from fancy_gpt.runtime_paths import user_data_dir

from .factory import TunnelDriverFactory
from .health import TunnelLayerInspector
from .models import TunnelHealth, TunnelHealthState, TunnelPolicy, TunnelSelection, TunnelSpec
from .registry import TunnelRegistry
from .resolver import TunnelResolver


class TunnelManager:
    def __init__(
        self,
        registry: TunnelRegistry | None = None,
        *,
        timeout_s: float = 300.0,
        headless: bool = True,
        bridge_token_file: Path | None = None,
        driver_factory: TunnelDriverFactory | None = None,
    ) -> None:
        self.registry = registry or TunnelRegistry()
        self.timeout_s = timeout_s
        self.headless = headless
        self.bridge_token_file = bridge_token_file or (user_data_dir() / "bridge-token")
        self.driver_factory = driver_factory or TunnelDriverFactory(
            bridge_token_file=self.bridge_token_file, headless=headless, timeout_s=timeout_s
        )
        self.layer_inspector = TunnelLayerInspector()
        self._bridge_probe_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._playwright_paths: list[Path] | None = None
        self.resolver = TunnelResolver(self.registry, self.probe)

    def _installed_playwright_paths(self) -> list[Path]:
        """Inspect installed browsers without starting Playwright's async driver."""
        if self._playwright_paths is not None:
            return self._playwright_paths
        version = importlib.metadata.version("playwright")
        completed = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--list"],
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
        marker = f"Playwright version: {version}"
        section = completed.stdout.split(marker, 1)[1] if marker in completed.stdout else ""
        section = section.split("Playwright version:", 1)[0]
        candidates = [line.strip() for line in section.splitlines()]
        self._playwright_paths = [Path(value) for value in candidates
                                  if Path(value).is_absolute() or PureWindowsPath(value).is_absolute()]
        return self._playwright_paths

    def _endpoint(self, spec: TunnelSpec) -> str | None:
        return self.driver_factory.endpoint(spec)

    def probe(self, spec: TunnelSpec) -> TunnelHealth:
        started = time.monotonic()
        layers = self.layer_inspector.inspect(spec)
        if any(item.state == "unavailable" for item in layers):
            return TunnelHealth(
                tunnel_id=spec.id,
                state=TunnelHealthState.UNAVAILABLE,
                detail="static layer contract failed",
                layers=layers,
            )
        try:
            if spec.runtime.value == "extension":
                token_file = self.driver_factory.token_file(spec)
                token = load_or_create_token(token_file)
                endpoint = self._endpoint(spec)
                assert endpoint is not None
                cache_key = (endpoint, str(token_file))
                cached = self._bridge_probe_cache.get(cache_key)
                now = time.monotonic()
                if cached is None or now - cached[0] > 1.0:
                    try:
                        bridge_info = probe_bridge_stats(endpoint, token, open_timeout_s=0.75)
                    except Exception:
                        workers = probe_bridge_workers(endpoint, token, open_timeout_s=0.75)
                        bridge_info = {"stats": {}, "workers": workers}
                    self._bridge_probe_cache[cache_key] = (now, bridge_info)
                else:
                    bridge_info = cached[1]
                tunnel_ids = available_tunnels(bridge_info.get("workers", []))
                metadata: dict[str, Any] = {"bridge": bridge_info}
                if spec.id not in tunnel_ids and "*" not in tunnel_ids:
                    return TunnelHealth(
                        tunnel_id=spec.id,
                        state=TunnelHealthState.UNAVAILABLE,
                        detail="bridge is reachable but no worker registered for this tunnel",
                        latency_ms=(time.monotonic() - started) * 1000,
                        browser_connected=False,
                        layers=layers,
                        metadata=metadata,
                    )
                return TunnelHealth(
                    tunnel_id=spec.id,
                    state=TunnelHealthState.HEALTHY,
                    detail="matching browser extension worker is connected to the bridge",
                    latency_ms=(time.monotonic() - started) * 1000,
                    browser_connected=True,
                    layers=layers,
                    metadata=metadata,
                )

            if spec.runtime.value == "playwright":
                if importlib.util.find_spec("playwright") is None:
                    return TunnelHealth(
                        tunnel_id=spec.id,
                        state=TunnelHealthState.UNAVAILABLE,
                        detail="Playwright Python package is not installed",
                        layers=layers,
                    )
                paths = self._installed_playwright_paths()
                prefix = "firefox-" if spec.browser == "firefox" else "chromium-"
                installed = next((path for path in paths if path.name.startswith(prefix)), None)
                if installed is None or not installed.is_dir():
                    return TunnelHealth(
                        tunnel_id=spec.id,
                        state=TunnelHealthState.UNAVAILABLE,
                        detail=f"Playwright {spec.browser} browser runtime is not installed for the current package version",
                        layers=layers,
                    )
                return TunnelHealth(
                    tunnel_id=spec.id,
                    state=TunnelHealthState.DEGRADED,
                    detail=f"Playwright browser runtime is installed at {installed}; ChatGPT session is verified when opened",
                    layers=layers,
                )

            if spec.runtime.value == "cdp":
                endpoint = self._endpoint(spec)
                assert endpoint is not None
                url = endpoint.rstrip("/") + "/json/version"
                with urllib.request.urlopen(url, timeout=2) as response:  # nosec B310 - explicit configured endpoint
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status}")
                return TunnelHealth(
                    tunnel_id=spec.id,
                    state=TunnelHealthState.HEALTHY,
                    detail="CDP endpoint is reachable",
                    latency_ms=(time.monotonic() - started) * 1000,
                    browser_connected=True,
                    layers=layers,
                )

            if spec.runtime.value == "interactive":
                return TunnelHealth(
                    tunnel_id=spec.id,
                    state=TunnelHealthState.HEALTHY,
                    detail="interactive human-in-the-loop tunnel is always available",
                    layers=layers,
                )
        except Exception as exc:
            return TunnelHealth(
                tunnel_id=spec.id,
                state=TunnelHealthState.UNAVAILABLE,
                detail=f"{type(exc).__name__}: {exc}",
                browser_connected=False,
                layers=layers,
            )
        return TunnelHealth(
            tunnel_id=spec.id,
            state=TunnelHealthState.UNKNOWN,
            detail="no dynamic probe implemented",
            layers=layers,
        )

    def inspect(self, spec: TunnelSpec) -> TunnelHealth:
        """Inspect the browser route without opening or assuming a model site."""
        return self.probe(spec)

    def select(
        self,
        *,
        tunnel_id: str | None = None,
        policy: TunnelPolicy = "auto",
        require_automatic: bool = True,
    ) -> TunnelSelection:
        return self.resolver.select(tunnel_id=tunnel_id, policy=policy, require_automatic=require_automatic)

    def provider(self, selection: TunnelSelection) -> ChatGPTWebAutomationProvider:
        spec = self.registry.get(selection.tunnel_id)
        if not spec.capabilities.automatic:
            raise RuntimeError(f"tunnel {spec.id} is interactive and cannot build an automatic provider")
        driver = self.driver_factory.build(spec)
        return ChatGPTWebAutomationProvider(driver, timeout_s=self.timeout_s, tunnel_id=spec.id)

    def health(self) -> list[TunnelHealth]:
        return [self.probe(spec) for spec in self.registry.effective()]
