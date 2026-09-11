from __future__ import annotations

import json
from pathlib import Path

import yaml


def load_mapping(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        payload = yaml.safe_load(text)
    else:
        payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("input must be a mapping/object")
    return payload
