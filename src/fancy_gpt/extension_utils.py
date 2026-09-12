from __future__ import annotations

import hashlib
from importlib.resources import files
from pathlib import Path


def _patch_browser_defaults(text: str, browser: str) -> str:
    remote_tunnel = f"{browser}-extension-ws-remote"
    return (
        text
        .replace('tunnelId: "chrome-extension-ws-remote"', f'tunnelId: "{remote_tunnel}"')
        .replace('browserName: "chrome"', f'browserName: "{browser}"')
        .replace('value="chrome-extension-ws-remote"', f'value="{remote_tunnel}"')
        .replace('value="chrome"', f'value="{browser}"')
    )


_BUILD_PLACEHOLDER = "__FANCYGPT_ADAPTER_BUILD__"


def export_extension(browser: str, destination: Path) -> Path:
    family = "firefox" if browser == "firefox" else "chromium"
    if browser not in {"chrome", "edge", "firefox"}:
        raise ValueError("browser must be chrome, edge, or firefox")
    source = files("fancy_gpt").joinpath(f"extension_assets/{family}")
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if not item.is_file():
            continue
        target = destination / item.name
        if item.name in {"background.js", "popup.html"}:
            target.write_text(_patch_browser_defaults(item.read_text(encoding="utf-8"), browser), encoding="utf-8")
        elif item.name == "site_chatgpt.js":
            target.write_text(_stamp_adapter_build(item.read_text(encoding="utf-8")), encoding="utf-8")
        else:
            target.write_bytes(item.read_bytes())
    return destination


def adapter_build_id(source: str | None = None) -> str:
    """Identify one exact build of the ChatGPT site adapter.

    The browser often runs on a different machine than the runtime, so the only
    way to know whether a reload actually took effect is to have the adapter
    report which build answered.
    """
    if source is None:
        source = files("fancy_gpt").joinpath("extension_assets/chromium/site_chatgpt.js").read_text(encoding="utf-8")
    return hashlib.sha256(source.replace(_BUILD_PLACEHOLDER, "").encode("utf-8")).hexdigest()[:12]


def _stamp_adapter_build(source: str) -> str:
    return source.replace(_BUILD_PLACEHOLDER, adapter_build_id(source))
