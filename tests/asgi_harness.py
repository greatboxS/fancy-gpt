"""Drive an ASGI app directly, with no HTTP client dependency.

Calling the app through its own protocol (rather than a real socket) is what
makes a mid-stream client disconnect testable: the harness decides exactly when
``http.disconnect`` is delivered.
"""

from __future__ import annotations

import json
from typing import Any

import anyio


class Result:
    def __init__(self) -> None:
        self.status: int = 0
        self.headers: dict[str, str] = {}
        self.chunks: list[bytes] = []

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)

    def json(self) -> Any:
        return json.loads(self.body)

    def sse_events(self) -> list[dict[str, str]]:
        """Parse the response body as a Server-Sent Events stream.

        Line endings are normalized first: sse-starlette separates fields with
        CRLF, so splitting on bare LF would see the whole stream as one block.
        """
        text = self.body.decode("utf-8").replace("\r\n", "\n")
        events: list[dict[str, str]] = []
        for block in text.split("\n\n"):
            if not block.strip():
                continue
            event: dict[str, str] = {}
            for line in block.splitlines():
                if line.startswith("data:"):
                    event["data"] = line[5:].strip()
                elif line.startswith("event:"):
                    event["event"] = line[6:].strip()
            if event:
                events.append(event)
        return events


async def call_app(
    app: Any,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    disconnect_after_chunks: int | None = None,
    content_length: int | None = None,
) -> Result:
    """Invoke an ASGI app once.

    ``disconnect_after_chunks`` simulates the caller going away after that many
    response body chunks have been written.
    """
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    header_pairs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if raw and not any(k == b"content-type" for k, _ in header_pairs):
        header_pairs.append((b"content-type", b"application/json"))
    if raw:
        declared = content_length if content_length is not None else len(raw)
        header_pairs.append((b"content-length", str(declared).encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": header_pairs,
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8787),
    }

    result = Result()
    disconnected = anyio.Event()
    body_sent = False

    async def receive() -> dict:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        # Block until the test decides the client has gone away. Starlette's
        # is_disconnected() polls receive with a tiny timeout, so blocking here
        # correctly reads as "still connected".
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            result.status = message["status"]
            result.headers = {k.decode(): v.decode() for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            if chunk:
                result.chunks.append(chunk)
            if (
                disconnect_after_chunks is not None
                and len(result.chunks) >= disconnect_after_chunks
                and not disconnected.is_set()
            ):
                disconnected.set()

    with anyio.move_on_after(30):
        await app(scope, receive, send)
    disconnected.set()
    return result


def request(app: Any, method: str, path: str, **kwargs: Any) -> Result:
    """Synchronous wrapper for use in ordinary tests."""
    return anyio.run(lambda: call_app(app, method, path, **kwargs))
