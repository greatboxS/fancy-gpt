from __future__ import annotations

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
        else:
            target.write_bytes(item.read_bytes())
    return destination
