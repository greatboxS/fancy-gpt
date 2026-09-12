from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "extension" / "common"
MANIFESTS = {
    "chromium": ROOT / "extension" / "chromium" / "manifest.json",
    "firefox": ROOT / "extension" / "firefox" / "manifest.json",
}
FILES = ["background.js", "bridge_transport.js", "site_chatgpt.js", "content.js", "popup.html", "popup.js"]

# Every generated bundle, and the browser each one is configured for. Keeping
# them in one table is the point: these copies used to be synced by hand, so one
# of them was reliably stale and the browser ran code nobody was looking at.
#   family, browser, destination, stamp_build
PROFILES = [
    ("chromium", "chrome", ROOT / "src" / "fancy_gpt" / "extension_assets" / "chromium", False),
    ("firefox", "firefox", ROOT / "src" / "fancy_gpt" / "extension_assets" / "firefox", False),
    ("chromium", "chrome", ROOT / "extension" / "pre-build" / "chromium", True),
    ("chromium", "edge", ROOT / "extension" / "pre-build" / "edge", True),
    ("firefox", "firefox", ROOT / "extension" / "pre-build" / "firefox", True),
]


def patch_defaults(text: str, browser: str) -> str:
    remote = f"{browser}-extension-ws-remote"
    return (
        text
        .replace('tunnelId: "chrome-extension-ws-remote"', f'tunnelId: "{remote}"')
        .replace('browserName: "chrome"', f'browserName: "{browser}"')
        .replace('value="chrome-extension-ws-remote"', f'value="{remote}"')
        .replace('value="chrome"', f'value="{browser}"')
    )


def adapter_build_id(source: str) -> str:
    import hashlib

    return hashlib.sha256(source.replace(PLACEHOLDER, "").encode("utf-8")).hexdigest()[:12]


PLACEHOLDER = "__FANCYGPT_ADAPTER_BUILD__"


def expected_files(family: str, browser: str, stamp_build: bool) -> dict[str, bytes]:
    result: dict[str, bytes] = {"manifest.json": MANIFESTS[family].read_bytes()}
    for name in FILES:
        text = (COMMON / name).read_text(encoding="utf-8")
        if browser != "chrome" and name in {"background.js", "popup.html"}:
            text = patch_defaults(text, browser)
        if name == "site_chatgpt.js" and stamp_build:
            # A pre-built bundle is loaded straight into a browser, so it has to
            # carry a real build id; the packaged assets keep the placeholder
            # because `extension export` stamps them on the way out.
            text = text.replace(PLACEHOLDER, adapter_build_id(text))
        result[name] = text.encode("utf-8")
    return result


def build() -> None:
    for family, browser, target, stamp in PROFILES:
        target.mkdir(parents=True, exist_ok=True)
        for name, content in expected_files(family, browser, stamp).items():
            (target / name).write_bytes(content)


def check() -> list[str]:
    errors: list[str] = []
    for family, browser, target, stamp in PROFILES:
        for name, content in expected_files(family, browser, stamp).items():
            path = target / name
            if not path.is_file():
                errors.append(f"missing {path.relative_to(ROOT)}")
            elif path.read_bytes() != content:
                errors.append(f"stale {path.relative_to(ROOT)}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        errors = check()
        print(json.dumps({"ok": not errors, "errors": errors}, indent=2))
        if errors:
            raise SystemExit(1)
    else:
        build()
        print(json.dumps({
            "ok": True,
            "generated": [str(target.relative_to(ROOT)) for _, _, target, _ in PROFILES],
        }, indent=2))


if __name__ == "__main__":
    main()
