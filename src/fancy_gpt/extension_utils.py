from __future__ import annotations

import hashlib
from importlib.resources import files
from pathlib import Path


def _patch_browser_defaults(text: str, browser: str) -> str:
    remote_tunnel = f"{browser}-remote"
    return (
        text
        .replace('tunnelId: "chrome-remote"', f'tunnelId: "{remote_tunnel}"')
        .replace('browserName: "chrome"', f'browserName: "{browser}"')
        .replace('value="chrome-remote"', f'value="{remote_tunnel}"')
        .replace('value="chrome"', f'value="{browser}"')
    )


_BUILD_PLACEHOLDER = "__FANCYGPT_ADAPTER_BUILD__"


# The scripts that together decide how a page is driven. A change to any of them
# changes how the browser behaves, so all of them define the build.
ADAPTER_SOURCES = ("background.js", "bridge_transport.js", "site_kit.js", "content.js")


def _adapter_source_names(family: str = "chromium") -> list[str]:
    source = files("fancy_gpt").joinpath(f"extension_assets/{family}")
    sites = sorted(
        item.name for item in source.iterdir()
        if item.is_file() and item.name.startswith("site_") and item.name != "site_kit.js"
    )
    return [*ADAPTER_SOURCES, *sites]


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
        elif item.name == "site_kit.js":
            # Stamped once, in the one file every adapter loads, so each site
            # reports the same build instead of carrying its own copy.
            target.write_text(_stamp_adapter_build(item.read_text(encoding="utf-8"), family), encoding="utf-8")
        else:
            target.write_bytes(item.read_bytes())
    return destination


def adapter_build_id(family: str = "chromium") -> str:
    """Identify one exact build of the browser-side adapter surface.

    The browser often runs on a different machine than the runtime, so the only
    way to know whether a reload actually took effect is to have the adapter
    report which build answered. Every script that shapes how a page is driven
    contributes, so adding or changing any site adapter produces a new id.
    """
    digest = hashlib.sha256()
    root = files("fancy_gpt").joinpath(f"extension_assets/{family}")
    for name in _adapter_source_names(family):
        digest.update(name.encode("utf-8"))
        digest.update(root.joinpath(name).read_text(encoding="utf-8").replace(_BUILD_PLACEHOLDER, "").encode("utf-8"))
    return digest.hexdigest()[:12]


def _stamp_adapter_build(source: str, family: str = "chromium") -> str:
    return source.replace(_BUILD_PLACEHOLDER, adapter_build_id(family))
