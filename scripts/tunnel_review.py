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

expected_extension = {
    f"{browser}-extension-{suffix}"
    for browser in ("chrome", "edge", "firefox")
    for suffix in ("native-local", "ws-remote")
}
assert expected_extension <= ids
assert {item.id for item in RuntimeRegistry().all()} >= {"extension", "playwright", "cdp", "interactive"}
assert {item.id for item in TransportRegistry().all()} >= {"native-messaging", "websocket", "local-process", "cdp", "human"}
assert {item.id for item in SiteRegistry().all()} == {"chatgpt"}

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

site_js = (root / "extension/common/site_chatgpt.js").read_text(encoding="utf-8")
runtime_js = (root / "extension/common/background.js").read_text(encoding="utf-8")
transport_js = (root / "extension/common/bridge_transport.js").read_text(encoding="utf-8")
assert "prompt-textarea" in site_js and "data-turn-id" in site_js
assert "prompt-textarea" not in runtime_js
assert "chatgpt.com" not in transport_js
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
    "extension_tunnels": len(expected_extension),
    "static_layer_failures": layer_failures,
}
print(json.dumps(result, indent=2))
