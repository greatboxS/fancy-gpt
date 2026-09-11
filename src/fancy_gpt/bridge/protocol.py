from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = "fancy-browser/1"


def dumps(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False)


def loads(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("bridge message must be a JSON object")
    return value


def hello(*, role: str, token: str, tunnel_ids: list[str] | None = None, browser: str | None = None) -> dict[str, Any]:
    return {
        "type": "hello",
        "protocol": PROTOCOL_VERSION,
        "role": role,
        "token": token,
        "tunnel_ids": tunnel_ids or [],
        "browser": browser,
    }
