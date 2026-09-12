from __future__ import annotations

import json
from pathlib import Path

from fancy_gpt.bridge.native_manifest import native_host_manifest
from fancy_gpt.tunnels import TunnelCompositionValidator, TunnelLayerInspector, TunnelRegistry
from fancy_gpt.tunnels.models import TunnelRuntime, TunnelTransport
from fancy_gpt.web.runtime import RuntimeRegistry
from fancy_gpt.web.sites import SiteRegistry
from fancy_gpt.web.transport import TransportRegistry

root = Path(__file__).resolve().parents[1]
registry = TunnelRegistry()
inspector = TunnelLayerInspector()
specs = registry.all()
ids = {item.id for item in specs}

expected_tunnels = {f"{browser}-remote" for browser in ("chrome", "edge", "firefox")}
assert ids == expected_tunnels
assert {item.id for item in RuntimeRegistry().all()} >= {"extension", "playwright", "cdp", "interactive"}
assert {item.id for item in TransportRegistry().all()} >= {"native-messaging", "websocket", "local-process", "cdp", "human"}
sites = {item.id for item in SiteRegistry().all()}
assert sites == {"chatgpt", "gemini"}
assert all("site" not in spec.model_dump() for spec in specs)

layer_failures = {}
for spec in specs:
    failed = [item.model_dump(mode="json") for item in inspector.inspect(spec) if item.state != "healthy"]
    if failed:
        layer_failures[spec.id] = failed
assert not layer_failures, layer_failures

# Prove that inspection is not merely accepting the built-in catalog.
invalid = specs[0].model_copy(update={
    "id": "invalid-playwright-native",
    "runtime": TunnelRuntime.PLAYWRIGHT,
    "transport": TunnelTransport.NATIVE_MESSAGING,
})
try:
    TunnelCompositionValidator().validate(invalid)
except ValueError:
    pass
else:
    raise AssertionError("invalid Playwright + Native Messaging composition was accepted")

runtime_js = (root / "extension/common/background.js").read_text(encoding="utf-8")
transport_js = (root / "extension/common/bridge_transport.js").read_text(encoding="utf-8")
kit_js = (root / "extension/common/site_kit.js").read_text(encoding="utf-8")

# Each site owns its own DOM assumptions; the layers below must stay ignorant of them.
site_sources = {
    site: (root / f"extension/common/site_{site}.js").read_text(encoding="utf-8") for site in sorted(sites)
}
assert "prompt-textarea" in site_sources["chatgpt"] and "data-turn-id" in site_sources["chatgpt"]
assert "gemini.google.com" in site_sources["gemini"]
assert "prompt-textarea" not in runtime_js
assert "prompt-textarea" not in kit_js and "gemini.google.com" not in kit_js
for site, source in site_sources.items():
    other = {name for name in sites if name != site}
    assert not any(f"FancyGPTSites.{name}" in source for name in other), f"{site} adapter references another site"
    assert "FancyGPTSiteKit" in source, f"{site} adapter must reuse the shared kit"
assert "chatgpt.com" not in transport_js and "gemini.google.com" not in transport_js
assert "WebSocket" in transport_js and "connectNative" in transport_js

fake_executable = root / "fancy-gpt-native-host"
chrome_native = native_host_manifest("chrome", "abc", fake_executable)
firefox_native = native_host_manifest("firefox", "fancy-gpt@local", fake_executable)
assert "allowed_origins" in chrome_native and "allowed_extensions" not in chrome_native
assert "allowed_extensions" in firefox_native and "allowed_origins" not in firefox_native

result = {
    "pass": True,
    "tunnels": len(specs),
    "sites": len(SiteRegistry().all()),
    "runtimes": len(RuntimeRegistry().all()),
    "transports": len(TransportRegistry().all()),
    "browser_tunnels": len(expected_tunnels),
    "static_layer_failures": layer_failures,
}
print(json.dumps(result, indent=2))
