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


def patch_defaults(text: str, browser: str) -> str:
    remote = f"{browser}-extension-ws-remote"
    return (
        text
        .replace('tunnelId: "chrome-extension-ws-remote"', f'tunnelId: "{remote}"')
        .replace('browserName: "chrome"', f'browserName: "{browser}"')
        .replace('value="chrome-extension-ws-remote"', f'value="{remote}"')
        .replace('value="chrome"', f'value="{browser}"')
    )


def expected_files(family: str) -> dict[str, bytes]:
    browser = "firefox" if family == "firefox" else "chrome"
    result: dict[str, bytes] = {"manifest.json": MANIFESTS[family].read_bytes()}
    for name in FILES:
        text = (COMMON / name).read_text(encoding="utf-8")
        if family == "firefox" and name in {"background.js", "popup.html"}:
            text = patch_defaults(text, browser)
        result[name] = text.encode("utf-8")
    return result


def targets(family: str) -> list[Path]:
    return [
        ROOT / "extension" / "build" / family,
        ROOT / "src" / "fancy_gpt" / "extension_assets" / family,
    ]


def build() -> None:
    for family in MANIFESTS:
        expected = expected_files(family)
        for target in targets(family):
            target.mkdir(parents=True, exist_ok=True)
            for name, content in expected.items():
                (target / name).write_bytes(content)


def check() -> list[str]:
    errors: list[str] = []
    for family in MANIFESTS:
        expected = expected_files(family)
        for target in targets(family):
            for name, content in expected.items():
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
        print(json.dumps({"ok": True, "families": sorted(MANIFESTS)}, indent=2))


if __name__ == "__main__":
    main()
