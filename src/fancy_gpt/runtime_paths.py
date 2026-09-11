from __future__ import annotations

import os
import sys
from pathlib import Path


def user_data_dir() -> Path:
    """Return a platform-native per-user persistent data directory."""
    override = os.getenv("FANCY_GPT_DATA_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
        if base:
            return Path(base) / "FancyGPT"
        return Path.home() / "AppData" / "Local" / "FancyGPT"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FancyGPT"
    xdg = os.getenv("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "fancy-gpt"
    return Path.home() / ".local" / "share" / "fancy-gpt"


def default_browser_profile() -> Path:
    override = os.getenv("FANCY_GPT_BROWSER_PROFILE")
    if override:
        return Path(override).expanduser()
    return user_data_dir() / "browser-profile"


def default_bridge_token_file() -> Path:
    override = os.getenv("FANCY_GPT_BRIDGE_TOKEN_FILE")
    if override:
        return Path(override).expanduser()
    return user_data_dir() / "bridge-token"


def default_bridge_native_config() -> Path:
    return user_data_dir() / "native-host.json"
