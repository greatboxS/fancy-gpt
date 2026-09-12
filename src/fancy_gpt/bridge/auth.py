from __future__ import annotations

import secrets
from pathlib import Path


def load_or_create_token(path: Path) -> str:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


def load_token(path: Path) -> str:
    token = path.expanduser().read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"empty bridge token file: {path}")
    return token
