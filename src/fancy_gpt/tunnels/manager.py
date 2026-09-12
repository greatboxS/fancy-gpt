from __future__ import annotations

import importlib.util
import time
import urllib.request
from pathlib import Path

from fancy_gpt.bridge import BridgeBrowserDriver, available_tunnels, load_or_create_token, probe_bridge_workers
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
        self._bridge_probe_cache: dict[tuple[str, str], tuple[float, set[str]]] = {}
        self.resolver = TunnelResolver(self.registry, self.probe)

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
                    workers = probe_bridge_workers(endpoint, token, open_timeout_s=0.75)
                    tunnel_ids = available_tunnels(workers)
                    self._bridge_probe_cache[cache_key] = (now, tunnel_ids)
                else:
                    tunnel_ids = cached[1]
                if spec.id not in tunnel_ids and "*" not in tunnel_ids:
                    return TunnelHealth(
                        tunnel_id=spec.id,
                        state=TunnelHealthState.UNAVAILABLE,
                        detail="bridge is reachable but no worker registered for this tunnel",
                        latency_ms=(time.monotonic() - started) * 1000,
                        browser_connected=False,
                        layers=layers,
                    )
                return TunnelHealth(
                    tunnel_id=spec.id,
                    state=TunnelHealthState.HEALTHY,
                    detail="matching browser extension worker is connected to the bridge",
                    latency_ms=(time.monotonic() - started) * 1000,
                    browser_connected=True,
                    layers=layers,
                )

            if spec.runtime.value == "playwright":
                if importlib.util.find_spec("playwright") is None:
                    return TunnelHealth(
                        tunnel_id=spec.id,
                        state=TunnelHealthState.UNAVAILABLE,
                        detail="Playwright Python package is not installed",
                        layers=layers,
                    )
                from playwright.sync_api import sync_playwright
                runtime = sync_playwright().start()
                try:
                    browser_type = runtime.firefox if spec.browser == "firefox" else runtime.chromium
                    executable = Path(browser_type.executable_path)
                finally:
                    runtime.stop()
                if not executable.is_file():
                    return TunnelHealth(
                        tunnel_id=spec.id,
                        state=TunnelHealthState.UNAVAILABLE,
                        detail=f"Playwright browser executable is not installed: {executable}",
                        layers=layers,
                    )
                return TunnelHealth(
                    tunnel_id=spec.id,
                    state=TunnelHealthState.DEGRADED,
                    detail=f"Playwright browser executable is installed at {executable}; ChatGPT session is verified when opened",
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
        """Deep diagnostic health including the live site adapter when possible."""
        health = self.probe(spec)
        if health.state == TunnelHealthState.UNAVAILABLE or spec.runtime.value != "extension":
            return health
        driver = self.driver_factory.build(spec)
        site_health = getattr(driver, "site_health", None)
        if not callable(site_health):
            return health
        try:
            driver.start()
            driver.health_check()
            payload = site_health(timeout_s=min(20.0, self.timeout_s))
            health.metadata["site_health"] = payload
            health.detail = "browser worker connected and ChatGPT site adapter is ready"
            health.state = TunnelHealthState.HEALTHY
            return health
        except Exception as exc:
            health.state = TunnelHealthState.UNAVAILABLE
            health.detail = f"browser worker connected but site is not ready: {type(exc).__name__}: {exc}"
            health.metadata["site_ready"] = False
            return health
        finally:
            try:
                driver.stop()
            except Exception:
                pass

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
