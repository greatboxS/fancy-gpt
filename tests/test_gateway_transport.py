"""ASGI transport behaviour: resource bounds, disconnects, and body limits.

These cover what the transport is responsible for, as opposed to what the
gateway core decides. The browser-slot limiter is the important one: it bounds
the resource that is actually scarce.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import anyio
import pytest

from fancy_gpt.gateway import GatewayLimits, GatewayService
from fancy_gpt.gateway_app import create_app
from fancy_gpt.models import AutomatedModelResponse
from fancy_gpt.tunnels.models import TunnelHealth, TunnelHealthState, TunnelSelection

from .asgi_harness import call_app, request


class TimedProvider:
    """Records exactly when each turn occupied the browser."""

    name = "fake-web"

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.windows: list[tuple[float, float]] = []
        self.requests: list = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def execute(self, request):
        started = time.monotonic()
        self.requests.append(request)
        if self.delay:
            time.sleep(self.delay)
        self.windows.append((started, time.monotonic()))
        return AutomatedModelResponse(
            request_id=request.request_id,
            stage="agent",
            provider=self.name,
            raw_text=json.dumps({"type": "message", "text": "gateway-ok"}),
            response_identity="response-1",
            conversation_id=f"conversation-{len(self.requests)}",
        )


class StreamingProvider(TimedProvider):
    """Reports the growing reply the way the browser adapter does."""

    def __init__(self, chunks: list[str] | None = None, gap: float = 0.05) -> None:
        super().__init__()
        self.chunks = chunks or ["Hello", "Hello world", "Hello world!"]
        self.gap = gap

    def execute(self, request, *, on_progress=None):
        self.requests.append(request)
        final = self.chunks[-1]
        for chunk in self.chunks:
            if on_progress is not None:
                # Cumulative JSON snapshots, exactly like the scraped page.
                on_progress(json.dumps({"type": "message", "text": chunk}))
            time.sleep(self.gap)
        self.windows.append((0.0, 0.0))
        return AutomatedModelResponse(
            request_id=request.request_id, stage="agent", provider=self.name,
            raw_text=json.dumps({"type": "message", "text": final}),
            response_identity="response-1", conversation_id="conversation-1",
        )




class DisconnectAwareProvider(TimedProvider):
    """Blocks until the gateway forwards a client disconnect to cancel()."""

    def __init__(self) -> None:
        super().__init__()
        self.active_turn_id: str | None = None
        self.cancelled = threading.Event()
        self.cancel_reasons: list[str] = []

    def cancel(self, turn_id: str, *, generation_epoch: int = 0, reason: str = "cancelled") -> bool:
        self.cancel_reasons.append(reason)
        self.cancelled.set()
        return True

    def execute(self, request, *, on_progress=None):
        self.requests.append(request)
        self.active_turn_id = f"browser-{request.request_id}"
        if on_progress is not None:
            on_progress(json.dumps({"type": "message", "text": "partial"}))
        if not self.cancelled.wait(timeout=5.0):
            raise RuntimeError("disconnect was not forwarded to browser provider")
        raise RuntimeError("generation stopped by client disconnect")


class Manager:
    def __init__(self, provider: TimedProvider) -> None:
        self.model = provider

    def select(self, **_kwargs):
        return TunnelSelection(
            tunnel_id="edge-remote",
            reason="test",
            explicit=True,
            health=TunnelHealth(tunnel_id="edge-remote", state=TunnelHealthState.HEALTHY, detail="ok"),
        )

    def provider(self, _selection):
        return self.model


def build(tmp_path: Path, *, delay: float = 0.0, slots: int = 4, limits: GatewayLimits | None = None):
    provider = TimedProvider(delay)
    service = GatewayService(tmp_path, manager=Manager(provider), limits=limits)
    return create_app(service, browser_slots=slots), service, provider


def _overlap(windows: list[tuple[float, float]]) -> float:
    (a_start, a_end), (b_start, b_end) = windows[0], windows[1]
    return min(a_end, b_end) - max(a_start, b_start)


# -- browser slots are the real resource bound -------------------------------


def test_one_browser_slot_serializes_two_callers(tmp_path: Path) -> None:
    app, _service, provider = build(tmp_path, delay=0.25, slots=1)

    async def scenario() -> None:
        async with anyio.create_task_group() as group:
            for index in range(2):
                group.start_soon(
                    lambda i=index: call_app(
                        app, "POST", "/v1/responses",
                        body={"model": "fancy-gemini", "input": f"turn {i}"},
                        headers={"x-fancy-session-id": f"gw_slot_{i}", "x-fancy-client-id": f"c{i}"},
                    )
                )

    anyio.run(scenario)
    assert len(provider.windows) == 2
    # One tab means the two turns cannot have been in the browser together.
    assert _overlap(provider.windows) <= 0


def test_two_browser_slots_allow_genuine_overlap(tmp_path: Path) -> None:
    app, _service, provider = build(tmp_path, delay=0.25, slots=2)

    async def scenario() -> None:
        async with anyio.create_task_group() as group:
            for index in range(2):
                group.start_soon(
                    lambda i=index: call_app(
                        app, "POST", "/v1/responses",
                        body={"model": "fancy-gemini", "input": f"turn {i}"},
                        headers={"x-fancy-session-id": f"gw_par_{i}", "x-fancy-client-id": f"c{i}"},
                    )
                )

    anyio.run(scenario)
    assert len(provider.windows) == 2
    # Two tabs, two different sessions: they really did run at the same time.
    assert _overlap(provider.windows) > 0


def test_health_reports_browser_slot_usage(tmp_path: Path) -> None:
    app, _service, _provider = build(tmp_path, slots=3)
    body = request(app, "GET", "/health").json()
    assert body["resources"]["browser_slots"] == 3
    assert body["resources"]["in_use"] == 0
    assert body["ok"] is True
    assert body["capabilities"]
    metrics = request(app, "GET", "/metrics").json()
    assert set(metrics["metrics"]) == {"queued", "running", "completed", "rejected", "cancelled", "failed"}


# -- disconnects -------------------------------------------------------------


def test_client_disconnect_mid_stream_stops_the_stream(tmp_path: Path) -> None:
    """The gap the old one-pass SSE writer could not cover."""
    provider = StreamingProvider(["a" * 40, "a" * 80, "a" * 120, "a" * 160], gap=0.15)
    service = GatewayService(tmp_path, manager=Manager(provider))
    app = create_app(service)

    full = request(
        app, "POST", "/v1/responses",
        body={"model": "fancy-gemini", "input": "hello", "stream": True},
        headers={"x-fancy-session-id": "gw_stream_full"},
    )
    complete = len(full.sse_events())
    assert complete >= 6, "expected a multi-event stream to truncate"

    provider2 = StreamingProvider(["a" * 40, "a" * 80, "a" * 120, "a" * 160], gap=0.15)
    app2 = create_app(GatewayService(tmp_path / "cut", manager=Manager(provider2)))
    cut = anyio.run(
        lambda: call_app(
            app2, "POST", "/v1/responses",
            body={"model": "fancy-gemini", "input": "hello", "stream": True},
            headers={"x-fancy-session-id": "gw_stream_cut"},
            disconnect_after_chunks=2,
        )
    )
    # The stream stopped early instead of running to completion for a dead socket.
    assert len(cut.sse_events()) < complete


def test_stream_generator_teardown_cancels_blocking_browser_turn(tmp_path: Path) -> None:
    """EventSourceResponse may consume http.disconnect before Request sees it."""
    provider = DisconnectAwareProvider()
    app = create_app(GatewayService(tmp_path, manager=Manager(provider)))

    result = anyio.run(
        lambda: call_app(
            app,
            "POST",
            "/v1/responses",
            body={"model": "fancy-chatgpt", "input": "hello", "stream": True},
            headers={"x-fancy-session-id": "gw_disconnect_cancel"},
            disconnect_after_chunks=2,
        )
    )

    deadline = time.monotonic() + 2
    while not provider.cancel_reasons and time.monotonic() < deadline:
        time.sleep(0.01)
    assert result.sse_events(), "stream should begin before disconnect"
    assert provider.cancel_reasons == ["client disconnected"]


# -- real incremental streaming ----------------------------------------------


def test_text_arrives_as_separate_deltas_not_one_block(tmp_path: Path) -> None:
    provider = StreamingProvider(["Hello", "Hello world", "Hello world!"], gap=0.02)
    app = create_app(GatewayService(tmp_path, manager=Manager(provider)))

    result = request(
        app, "POST", "/v1/responses",
        body={"model": "fancy-gemini", "input": "hi", "stream": True},
        headers={"x-fancy-session-id": "gw_incremental"},
    )
    events = result.sse_events()
    names = [event.get("event") for event in events]
    deltas = [json.loads(event["data"])["delta"] for event in events if event.get("event") == "response.output_text.delta"]

    assert names[0] == "response.created"
    assert names[1] == "response.in_progress"
    assert names[-1] == "response.completed"
    assert len(deltas) >= 2, f"expected several deltas, got {deltas}"
    # The deltas concatenate to exactly the final text, with nothing repeated.
    assert "".join(deltas) == "Hello world!"
    # Ordering: the item is opened before any delta and closed after them.
    assert names.index("response.output_item.added") < names.index("response.output_text.delta")
    assert names.index("response.output_text.done") > names.index("response.output_text.delta")
    # sequence_number is monotonic across the whole stream.
    numbers = [json.loads(event["data"]).get("sequence_number") for event in events]
    assert numbers == sorted(n for n in numbers if n is not None)


def test_anthropic_streams_text_delta_blocks(tmp_path: Path) -> None:
    provider = StreamingProvider(["par", "part one", "part one done"], gap=0.02)
    app = create_app(GatewayService(tmp_path, manager=Manager(provider)))

    result = request(
        app, "POST", "/v1/messages",
        body={"model": "fancy-chatgpt", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers={"x-fancy-session-id": "gw_anthropic_stream"},
    )
    events = result.sse_events()
    names = [event.get("event") for event in events]
    assert names[0] == "message_start"
    assert names[-2:] == ["message_delta", "message_stop"]
    assert names.count("content_block_start") == names.count("content_block_stop")
    deltas = [
        json.loads(event["data"])["delta"]["text"]
        for event in events
        if event.get("event") == "content_block_delta"
    ]
    assert "".join(deltas) == "part one done"


def test_gemini_streams_candidate_chunks(tmp_path: Path) -> None:
    provider = StreamingProvider(["one", "one two", "one two three"], gap=0.02)
    app = create_app(GatewayService(tmp_path, manager=Manager(provider)))

    result = request(
        app, "POST", "/v1beta/models/fancy-gemini:streamGenerateContent",
        body={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        headers={"x-fancy-session-id": "gw_gemini_stream"},
    )
    payloads = [json.loads(event["data"]) for event in result.sse_events()]
    texts = [
        part["text"]
        for payload in payloads
        for candidate in payload.get("candidates", [])
        for part in candidate.get("content", {}).get("parts", [])
        if "text" in part
    ]
    assert "".join(texts[:-1]) == "one two three"
    assert payloads[-1]["candidates"][0]["finishReason"] == "STOP"


def test_a_tool_call_turn_opens_no_text_block(tmp_path: Path) -> None:
    """A turn that ends in a tool call has no message item to open."""
    provider = TimedProvider()
    provider.execute = lambda request, on_progress=None: AutomatedModelResponse(  # type: ignore[assignment]
        request_id=request.request_id, stage="agent", provider="fake-web",
        raw_text=json.dumps({"type": "tool_calls", "calls": [{"id": "c1", "name": "read_file", "arguments": {}}]}),
        response_identity="r", conversation_id="conversation-1",
    )
    app = create_app(GatewayService(tmp_path, manager=Manager(provider)))
    result = request(
        app, "POST", "/v1/responses",
        body={"model": "fancy-gemini", "input": "use it", "stream": True,
              "tools": [{"type": "function", "name": "read_file", "parameters": {}}]},
        headers={"x-fancy-session-id": "gw_toolstream"},
    )
    names = [event.get("event") for event in result.sse_events()]
    assert "response.output_text.delta" not in names
    assert "response.function_call_arguments.delta" in names
    assert names[-1] == "response.completed"


# -- body limits are enforced before parsing ---------------------------------


def test_oversized_body_is_refused(tmp_path: Path) -> None:
    app, _service, _provider = build(tmp_path, limits=GatewayLimits(max_body_bytes=256))
    result = request(
        app, "POST", "/v1/responses",
        body={"model": "fancy-gemini", "input": "x" * 2000},
    )
    assert result.status == 413
    assert "exceeds" in json.dumps(result.json())


def test_understated_content_length_is_still_refused(tmp_path: Path) -> None:
    """A caller that lies about Content-Length must not slip past the limit."""
    app, _service, provider = build(tmp_path, limits=GatewayLimits(max_body_bytes=256))
    result = request(
        app, "POST", "/v1/responses",
        body={"model": "fancy-gemini", "input": "x" * 2000},
        content_length=10,
    )
    assert result.status == 413
    assert provider.requests == []


def test_malformed_json_maps_to_the_protocol_envelope(tmp_path: Path) -> None:
    app, _service, _provider = build(tmp_path)
    for path, key in (
        ("/v1/responses", "type"),
        ("/v1/messages", "type"),
    ):
        result = request(app, "POST", path, body=[1, 2, 3])
        assert result.status == 400
        assert key in result.json()["error"]


def test_unsupported_modality_is_rejected_over_http(tmp_path: Path) -> None:
    app, _service, provider = build(tmp_path)
    result = request(
        app, "POST", "/v1/responses",
        body={
            "model": "fancy-chatgpt",
            "input": [{"role": "user", "content": [{"type": "input_image", "image_url": "data:..."}]}],
        },
    )
    assert result.status == 400
    assert "not supported" in result.json()["error"]["message"]
    # Rejected before anything reached the browser.
    assert provider.requests == []


def test_auth_failure_uses_each_protocol_envelope(tmp_path: Path) -> None:
    provider = TimedProvider()
    service = GatewayService(tmp_path, manager=Manager(provider))
    app = create_app(service, token="secret")

    openai = request(app, "POST", "/v1/responses", body={"model": "fancy-gemini", "input": "x"})
    assert openai.status == 401 and openai.json()["error"]["type"] == "authentication_error"

    gemini = request(
        app, "POST", "/v1beta/models/fancy-gemini:generateContent",
        body={"contents": [{"role": "user", "parts": [{"text": "x"}]}]},
    )
    assert gemini.status == 401 and gemini.json()["error"]["status"] == "UNAUTHENTICATED"

    ok = request(
        app, "POST", "/v1/responses",
        body={"model": "fancy-gemini", "input": "x"},
        headers={"Authorization": "Bearer secret"},
    )
    assert ok.status == 200
