from __future__ import annotations

import json
import re
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


_FENCE = re.compile(
    r"^[ \t]*```[ \t]*(?P<info>[^\n`]*)\n(?P<body>.*?)^[ \t]*```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
_VERBATIM_INFO = re.compile(r"^fancygpt[:\s]+(?P<id>[A-Za-z0-9._\-]+)$", re.IGNORECASE)


def split_verbatim_blocks(raw_text: str) -> tuple[str, dict[str, str]]:
    """Separate fenced verbatim blocks from the JSON document.

    Source code cannot travel inside a JSON string. The model would have to
    escape every quote, backslash and newline in the file it is editing, and it
    reliably gets that wrong: one unescaped `"` from the edited code invalidates
    the entire response.

    Code therefore travels beside the JSON in its own fenced block, tagged with
    an id the JSON refers to:

        ```fancygpt:E1-OLD
        def alpha():
            return 1
        ```

    A fence is used rather than a custom sentinel because the response is read
    back out of the rendered ChatGPT DOM. Markdown renders a fence as
    `<pre><code>`, whose `innerText` preserves leading whitespace exactly, while
    an inline sentinel is rewrapped by the renderer and arrives mangled --
    losing precisely the indentation that makes source code correct.
    """
    fences = list(_FENCE.finditer(raw_text))
    if not fences:
        return _split_scraped_blocks(raw_text)

    blocks: dict[str, str] = {}
    document_fences: list[str] = []
    outside: list[str] = []
    cursor = 0
    for match in fences:
        info = match.group("info").strip()
        named = _VERBATIM_INFO.match(info)
        outside.append(raw_text[cursor:match.start()])
        if named:
            block_id = named.group("id")
            if block_id in blocks:
                raise ValueError(f"duplicate verbatim block id: {block_id}")
            body = match.group("body")
            blocks[block_id] = body[:-1] if body.endswith("\n") else body
        else:
            # A plain or ```json fence carries the document itself.
            document_fences.append(match.group("body"))
        cursor = match.end()
    outside.append(raw_text[cursor:])

    if document_fences:
        # Prose around a fenced answer is commentary, not part of the document.
        return "\n".join(document_fences), blocks
    return "".join(outside), blocks


_SCRAPED_LABEL = re.compile(r"^[ \t]*fancygpt[:\s]+(?P<id>[A-Za-z0-9._\-]+)[ \t]*$", re.IGNORECASE)


def _split_scraped_blocks(raw_text: str) -> tuple[str, dict[str, str]]:
    """Recover fenced blocks after the browser has already rendered them.

    The response is read from the rendered page, where a fence is a `<pre>`
    element: `innerText` returns the block's contents with indentation intact
    but without the surrounding backticks, leaving the info string behind as a
    bare line. So the delimiters we wrote are gone and the labels are all that
    survive -- a block simply runs until the next label or the end of the text.
    """
    lines = raw_text.splitlines()
    marks = [(i, m.group("id")) for i, line in enumerate(lines) if (m := _SCRAPED_LABEL.match(line))]
    if not marks:
        return raw_text, {}

    blocks: dict[str, str] = {}
    for position, (index, block_id) in enumerate(marks):
        end = marks[position + 1][0] if position + 1 < len(marks) else len(lines)
        if block_id in blocks:
            raise ValueError(f"duplicate verbatim block id: {block_id}")
        blocks[block_id] = "\n".join(lines[index + 1:end])
    document = "\n".join(lines[:marks[0][0]])
    # A rendered ```json fence leaves its language label as a bare first line.
    document = re.sub(r"^[ \t]*json[ \t]*\n", "", document.strip(), count=1, flags=re.IGNORECASE)
    return document, blocks


def parse_json_object_with_blocks(raw_text: str) -> tuple[dict[str, Any], dict[str, str]]:
    document, blocks = split_verbatim_blocks(raw_text)
    return parse_json_object(document), blocks
