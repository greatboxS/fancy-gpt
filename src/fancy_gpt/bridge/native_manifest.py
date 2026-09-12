from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from fancy_gpt.runtime_paths import user_data_dir

HOST_NAME = "com.fancygpt.bridge"


def native_host_manifest(browser: str, extension_id: str, executable: Path) -> dict:
    common = {
        "name": HOST_NAME,
        "description": "FancyGPT local browser bridge",
        "path": str(executable.resolve()),
        "type": "stdio",
    }
    if browser == "firefox":
        common["allowed_extensions"] = [extension_id]
    elif browser in {"chrome", "edge"}:
        common["allowed_origins"] = [f"chrome-extension://{extension_id}/"]
    else:
        raise ValueError("browser must be chrome, edge, or firefox")
    return common


def windows_registry_key(browser: str) -> str:
    keys = {
        "chrome": rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}",
        "edge": rf"Software\Microsoft\Edge\NativeMessagingHosts\{HOST_NAME}",
        "firefox": rf"Software\Mozilla\NativeMessagingHosts\{HOST_NAME}",
    }
    try:
        return keys[browser]
    except KeyError as exc:
        raise ValueError("browser must be chrome, edge, or firefox") from exc


def default_manifest_dir(browser: str) -> Path:
    home = Path.home()
    if sys.platform.startswith("linux"):
        if browser == "chrome":
            return home / ".config/google-chrome/NativeMessagingHosts"
        if browser == "edge":
            return home / ".config/microsoft-edge/NativeMessagingHosts"
        if browser == "firefox":
            return home / ".mozilla/native-messaging-hosts"
    if sys.platform == "darwin":
        if browser == "chrome":
            return home / "Library/Application Support/Google/Chrome/NativeMessagingHosts"
        if browser == "edge":
            return home / "Library/Application Support/Microsoft Edge/NativeMessagingHosts"
        if browser == "firefox":
            return home / "Library/Application Support/Mozilla/NativeMessagingHosts"
    if os.name == "nt":
        return user_data_dir() / "native-messaging-hosts" / browser
    raise RuntimeError(f"unsupported platform for native manifest path: {sys.platform}")


def _register_windows_manifest(browser: str, manifest_path: Path) -> None:
    try:
        import winreg
    except ImportError as exc:  # pragma: no cover - Windows only
        raise RuntimeError("winreg is unavailable on this Python runtime") from exc
    key_path = windows_registry_key(browser)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:  # pragma: no cover - Windows only
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, str(manifest_path.resolve()))


def install_manifest(browser: str, extension_id: str, executable: Path, destination: Path | None = None) -> Path:
    target_dir = destination or default_manifest_dir(browser)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{HOST_NAME}.json"
    target.write_text(json.dumps(native_host_manifest(browser, extension_id, executable), indent=2) + "\n", encoding="utf-8")
    if os.name == "nt" and destination is None:
        _register_windows_manifest(browser, target)
    return target
