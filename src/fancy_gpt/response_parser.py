from __future__ import annotations

import json
from typing import Any


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_json_object(raw_text: str) -> dict[str, Any]:
    """Parse exactly one JSON object and reject prose/fences/arrays.

    The final model is explicitly instructed to return raw JSON, but some web
    UIs wrap the reply in a ``` code fence; that wrapper is stripped before
    parsing. The browser DOM scrape can also carry a literal newline inside a
    string value (e.g. a long URL that the ChatGPT UI line-wraps, preserved
    verbatim by `innerText`), which strict JSON rejects as an invalid control
    character even though the document is otherwise well-formed; a second,
    lenient parse pass (`strict=False`, which only affects control-character
    handling inside strings) recovers that case. Anything else (prose,
    multiple values, truncated output) still fails closed, since accepting a
    heuristic substring would make response binding/validation ambiguous.
    """

    text = _strip_code_fence(raw_text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            value = json.loads(text, strict=False)
        except json.JSONDecodeError as exc:
            raise ValueError("model response must be exactly one valid JSON value") from exc
    if not isinstance(value, dict):
        raise ValueError("model response must be one JSON object")
    return value
