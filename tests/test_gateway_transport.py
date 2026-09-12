"""ASGI transport behaviour: resource bounds, disconnects, and body limits.

These cover what the transport is responsible for, as opposed to what the
gateway core decides. The browser-slot limiter is the important one: it bounds
the resource that is actually scarce.
"""

from __future__ import annotations

import json
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
                        body={"model": "gemini-web", "input": f"turn {i}"},
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
                        body={"model": "gemini-web", "input": f"turn {i}"},
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
    app, _service, _provider = build(tmp_path)

    full = request(
        app, "POST", "/v1/responses",
        body={"model": "gemini-web", "input": "hello", "stream": True},
        headers={"x-fancy-session-id": "gw_stream_full"},
    )
    complete = len(full.sse_events())
    assert complete >= 6, "expected a multi-event stream to truncate"

    cut = anyio.run(
        lambda: call_app(
            app, "POST", "/v1/responses",
            body={"model": "gemini-web", "input": "hello", "stream": True},
            headers={"x-fancy-session-id": "gw_stream_cut"},
            disconnect_after_chunks=2,
        )
    )
    # The stream stopped early instead of writing every event to a dead socket.
    assert len(cut.sse_events()) < complete


# -- body limits are enforced before parsing ---------------------------------


def test_oversized_body_is_refused(tmp_path: Path) -> None:
    app, _service, _provider = build(tmp_path, limits=GatewayLimits(max_body_bytes=256))
    result = request(
        app, "POST", "/v1/responses",
        body={"model": "gemini-web", "input": "x" * 2000},
    )
    assert result.status == 413
    assert "exceeds" in json.dumps(result.json())


def test_understated_content_length_is_still_refused(tmp_path: Path) -> None:
    """A caller that lies about Content-Length must not slip past the limit."""
    app, _service, provider = build(tmp_path, limits=GatewayLimits(max_body_bytes=256))
    result = request(
        app, "POST", "/v1/responses",
        body={"model": "gemini-web", "input": "x" * 2000},
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
            "model": "chatgpt-web",
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

    openai = request(app, "POST", "/v1/responses", body={"model": "gemini-web", "input": "x"})
    assert openai.status == 401 and openai.json()["error"]["type"] == "authentication_error"

    gemini = request(
        app, "POST", "/v1beta/models/gemini-web:generateContent",
        body={"contents": [{"role": "user", "parts": [{"text": "x"}]}]},
    )
    assert gemini.status == 401 and gemini.json()["error"]["status"] == "UNAUTHENTICATED"

    ok = request(
        app, "POST", "/v1/responses",
        body={"model": "gemini-web", "input": "x"},
        headers={"Authorization": "Bearer secret"},
    )
    assert ok.status == 200
