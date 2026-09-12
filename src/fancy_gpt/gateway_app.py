"""ASGI transport for the model gateway.

The gateway's scarce resource is not CPU and not threads: it is browser tabs.
One turn occupies one automation tab on one site for as long as the model takes
to answer, which is tens of seconds. So the transport is built around that:

* an ``anyio.CapacityLimiter`` bounds how many turns may hold a browser tab at
  once, and every turn runs on the worker thread pool through that limiter;
* a client that disconnects cancels its turn while the browser call is still in
  flight, instead of the gateway discovering it only when it tries to reply;
* SSE events are emitted one at a time with a disconnect checkpoint between
  them, so a client that goes away mid-stream stops the stream.

``GatewayService`` itself stays synchronous. The browser driver is blocking, so
running the turn in a worker thread is what it actually is, and keeping the core
sync means the state, compaction and capability layers are unchanged.
"""

from __future__ import annotations

import json
import secrets
from functools import partial
from typing import Any, AsyncIterator, Callable

import anyio
import anyio.to_thread
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .gateway import (
    GATEWAY_MODELS,
    IDEMPOTENCY_HEADERS,
    CancelToken,
    GatewayCancelled,
    GatewayResult,
    GatewayService,
    NormalizedTurn,
    _protocol_error,
    _validate_header,
    anthropic_response,
    anthropic_stream_events,
    capability_report,
    classify_error,
    codex_models,
    gemini_response,
    normalize_anthropic,
    normalize_gemini,
    normalize_openai,
    openai_response,
    openai_stream_events,
)

#: How often the transport checks whether the caller is still connected while a
#: browser turn is running. The turn takes tens of seconds, so this is cheap.
DISCONNECT_POLL_SECONDS = 0.5


class GatewayResources:
    """Bounds on the resources a gateway actually consumes.

    ``browser_slots`` is the important one. It is not a request-rate limit: it
    is the number of automation tabs that may be open at once, which is what the
    browser and the bridge worker really have to supply.
    """

    def __init__(self, *, browser_slots: int = 4) -> None:
        self.browser_slots = browser_slots
        self._limiter: anyio.CapacityLimiter | None = None

    @property
    def limiter(self) -> anyio.CapacityLimiter:
        # Created lazily so the limiter is bound to the running event loop.
        if self._limiter is None:
            self._limiter = anyio.CapacityLimiter(self.browser_slots)
        return self._limiter

    def snapshot(self) -> dict[str, Any]:
        limiter = self._limiter
        if limiter is None:
            return {"browser_slots": self.browser_slots, "in_use": 0, "waiting": 0}
        return {
            "browser_slots": self.browser_slots,
            "in_use": limiter.borrowed_tokens,
            "waiting": len(limiter.statistics().tasks_waiting),
        }


def _protocol_for(path: str) -> str | None:
    if path == "/v1/responses":
        return "openai"
    if path == "/v1/messages":
        return "anthropic"
    if ":generateContent" in path or ":streamGenerateContent" in path:
        return "gemini"
    return None


def _authorized(request: Request, token: str | None) -> bool:
    """Constant-time bearer/api-key check."""
    if not token:
        return True
    authorization = request.headers.get("authorization") or ""
    api_key = request.headers.get("x-api-key") or ""
    return secrets.compare_digest(authorization, f"Bearer {token}") or secrets.compare_digest(api_key, token)


def _idempotency_key(request: Request) -> str | None:
    for name in IDEMPOTENCY_HEADERS:
        value = request.headers.get(name)
        if value:
            candidate = value.strip()
            if len(candidate) > 255:
                raise ValueError("idempotency key must be 1-255 characters")
            return candidate
    return None


def _client_key(request: Request) -> str:
    declared = request.headers.get("x-fancy-client-id")
    if declared:
        return _validate_header(declared, "x-fancy-client-id") or "unknown"
    return request.client.host if request.client else "unknown"


async def _read_body(request: Request, limit: int) -> dict[str, Any]:
    """Read and parse the body, refusing anything oversized before parsing."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            announced = int(declared)
        except ValueError:
            raise ValueError("Content-Length header is not an integer") from None
        if announced > limit:
            raise _TooLarge(limit)
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        # A chunked request can lie about or omit Content-Length, so the real
        # bound is enforced while reading rather than trusting the header.
        if len(raw) > limit:
            raise _TooLarge(limit)
    if not raw:
        raise ValueError("request body is required")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


class _TooLarge(ValueError):
    def __init__(self, limit: int) -> None:
        super().__init__(f"request body exceeds {limit} bytes")
        self.limit = limit


async def _run_turn(
    service: GatewayService,
    resources: GatewayResources,
    request: Request,
    turn: NormalizedTurn,
    *,
    tunnel_id: str | None,
    idempotency_key: str | None,
    client_key: str,
) -> GatewayResult:
    """Run one blocking browser turn, cancelling it if the caller goes away."""
    token = CancelToken()
    done = anyio.Event()
    result: list[GatewayResult] = []
    failure: list[BaseException] = []

    async def execute() -> None:
        try:
            result.append(
                await anyio.to_thread.run_sync(
                    partial(
                        service.execute,
                        turn,
                        tunnel_id=tunnel_id,
                        idempotency_key=idempotency_key,
                        cancel_token=token,
                        client_key=client_key,
                    ),
                    # The browser call blocks in a socket read that cannot be
                    # interrupted, so a cancelled turn stops being awaited
                    # rather than pretending the thread was killed. The turn
                    # itself observes the CancelToken at its own checkpoints.
                    abandon_on_cancel=True,
                    limiter=resources.limiter,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - re-raised to the caller
            failure.append(exc)
        finally:
            done.set()

    async def watch_disconnect() -> None:
        while True:
            with anyio.move_on_after(DISCONNECT_POLL_SECONDS):
                await done.wait()
            if done.is_set():
                return
            if await request.is_disconnected():
                # Signal the running turn; the service releases the
                # conversation lock at its next checkpoint.
                token.cancel("client disconnected")
                return

    async with anyio.create_task_group() as group:
        group.start_soon(execute)
        group.start_soon(watch_disconnect)
        await done.wait()
        group.cancel_scope.cancel()

    if failure:
        raise failure[0]
    return result[0]


def _error_response(exc: Exception, protocol: str) -> JSONResponse:
    status, body, retry_after = classify_error(exc, protocol)
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return JSONResponse(body, status_code=status, headers=headers)


async def _stream(
    events: list[tuple[str | None, dict[str, Any]]],
    request: Request,
) -> AsyncIterator[ServerSentEvent]:
    """Emit SSE events one at a time, stopping if the client goes away.

    The browser backend returns a whole answer rather than a token stream, so
    these events are the protocol's framing rather than generation progress.
    What is genuinely incremental here is the transport: the connection is
    checked between events, so a client that disconnects mid-stream ends the
    response instead of being written to a dead socket.
    """
    for name, payload in events:
        if await request.is_disconnected():
            return
        yield ServerSentEvent(data=json.dumps(payload, ensure_ascii=False), event=name)


def create_app(service: GatewayService, *, token: str | None = None, browser_slots: int = 4) -> Starlette:
    resources = GatewayResources(browser_slots=browser_slots)

    async def health(request: Request) -> Response:
        return JSONResponse({
            "ok": True,
            "service": "fancy-gpt-gateway",
            "metrics": service.metrics.snapshot(),
            "resources": resources.snapshot(),
            "active_conversations": len(service.locks.active_keys()),
            "capabilities": capability_report(GATEWAY_MODELS, GatewayService.resolve_site),
            "limits": dict(service.limits.__dict__),
        })

    async def metrics(request: Request) -> Response:
        return JSONResponse({
            "metrics": service.metrics.snapshot(),
            "resources": resources.snapshot(),
            "active_conversations": service.locks.active_keys(),
        })

    async def capabilities(request: Request) -> Response:
        return JSONResponse({"capabilities": capability_report(GATEWAY_MODELS, GatewayService.resolve_site)})

    async def models(request: Request) -> Response:
        return JSONResponse(codex_models())

    async def turn_endpoint(request: Request) -> Response:
        path = request.url.path
        protocol = _protocol_for(path)
        if protocol is None or request.method != "POST":
            return JSONResponse({"error": {"message": "not found", "type": "invalid_request_error"}}, status_code=404)
        if not _authorized(request, token):
            return JSONResponse(
                _protocol_error(protocol, 401, "invalid API key", "authentication_error"), status_code=401
            )
        try:
            payload = await _read_body(request, service.limits.max_body_bytes)
            session = _validate_header(request.headers.get("x-fancy-session-id"), "x-fancy-session-id")
            tunnel = _validate_header(request.headers.get("x-fancy-tunnel-id"), "x-fancy-tunnel-id")
            key = _idempotency_key(request)
            client = _client_key(request)

            if protocol == "openai":
                turn = normalize_openai(payload, session)
            elif protocol == "anthropic":
                turn = normalize_anthropic(payload, session)
            else:
                model = path.split("/models/", 1)[1].split(":", 1)[0]
                turn = normalize_gemini(payload, model, session)

            result = await _run_turn(
                service, resources, request, turn,
                tunnel_id=tunnel, idempotency_key=key, client_key=client,
            )
        except _TooLarge as exc:
            return JSONResponse(_protocol_error(protocol, 413, str(exc), "invalid_request_error"), status_code=413)
        except Exception as exc:  # noqa: BLE001 - mapped to the client's envelope
            return _error_response(exc, protocol)

        if protocol == "openai":
            body = openai_response(result)
            if turn.stream:
                return EventSourceResponse(_stream(openai_stream_events(body, result), request))
            return JSONResponse(body)
        if protocol == "anthropic":
            body = anthropic_response(result)
            if turn.stream:
                return EventSourceResponse(_stream(anthropic_stream_events(body, result), request))
            return JSONResponse(body)
        body = gemini_response(result)
        if ":streamGenerateContent" in path:
            return EventSourceResponse(_stream([(None, body)], request))
        return JSONResponse(body)

    return Starlette(routes=[
        Route("/health", health, methods=["GET", "HEAD"]),
        Route("/metrics", metrics, methods=["GET"]),
        Route("/v1/capabilities", capabilities, methods=["GET"]),
        Route("/v1/models", models, methods=["GET"]),
        Route("/v1/responses", turn_endpoint, methods=["POST"]),
        Route("/v1/messages", turn_endpoint, methods=["POST"]),
        # Catch-all: an unknown path is 404, not 405. The Gemini endpoint
        # carries the model in its path, so it cannot be a fixed route.
        Route("/{path:path}", turn_endpoint, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]),
    ])
