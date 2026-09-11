from __future__ import annotations

import json
from typing import Any


def parse_json_object(raw_text: str) -> dict[str, Any]:
    """Parse exactly one JSON object and reject prose/fences/arrays.

    The final model is explicitly instructed to return raw JSON. Accepting a
    heuristic substring would make response binding/validation ambiguous, so
    automation fails closed instead.
    """

    try:
        value = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError("model response must be exactly one valid JSON value") from exc
    if not isinstance(value, dict):
        raise ValueError("model response must be one JSON object")
    return value
