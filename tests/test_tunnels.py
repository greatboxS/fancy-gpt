from __future__ import annotations

import json
import threading
import time

import pytest
from websockets.sync.client import connect

from fancy_gpt.bridge import BridgeBrowserDriver, BridgeServer
from fancy_gpt.bridge.protocol import dumps, hello, loads
from fancy_gpt.tunnels import (
    TunnelHealth,
    TunnelHealthState,
    TunnelRegistry,
    TunnelResolver,
)


def test_tunnel_catalog_has_expected_compositions() -> None:
    registry = TunnelRegistry()
    ids = {item.id for item in registry.all()}
    assert ids == {"chrome-remote", "edge-remote", "firefox-remote"}
    assert all("site" not in spec.model_dump() for spec in registry.all())


def test_tunnel_resolver_policy_and_explicit_selection() -> None:
    registry = TunnelRegistry()

    def probe(spec):
        state = TunnelHealthState.HEALTHY if spec.id in {"chrome-remote", "edge-remote"} else TunnelHealthState.UNAVAILABLE
        return TunnelHealth(tunnel_id=spec.id, state=state, detail=state.value)

    resolver = TunnelResolver(registry, probe)
    selected = resolver.select(policy="prefer-remote")
    assert selected.tunnel_id == "chrome-remote"
    explicit = resolver.select(tunnel_id="edge-remote")
    assert explicit.explicit is True
    assert explicit.tunnel_id == "edge-remote"


def test_bridge_driver_routes_to_exact_registered_tunnel() -> None:
    token = "test-token"
    server = BridgeServer("127.0.0.1", 0, token)
    server.start_background()
    endpoint = f"ws://127.0.0.1:{server.port}"
    stop = threading.Event()

    def browser_worker() -> None:
        connection = connect(endpoint)
        connection.send(dumps(hello(
            role="browser", token=token,
            tunnel_ids=["chrome-remote"], browser="chrome"
        )))
        assert loads(connection.recv(timeout=5))["type"] == "hello_ack"
        try:
            while not stop.is_set():
                message = loads(connection.recv(timeout=5))
                if message.get("type") == "job":
                    connection.send(dumps({
                        "type": "job_result",
                        "job_id": message["job_id"],
                        "text": json.dumps({"echo": message["prompt"]}),
                        "response_identity": "assistant-turn-1",
                    }))
        except Exception:
            pass
        finally:
            connection.close()

    thread = threading.Thread(target=browser_worker, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while server.hub.worker_for("chrome-remote") is None and time.monotonic() < deadline:
        time.sleep(0.01)

    driver = BridgeBrowserDriver(endpoint, token, "chrome-remote")
    try:
        driver.start()
        driver.health_check()
        turn = driver.begin_turn(request_id="r1", stage="planner")
        driver.submit(turn, "hello")
        response = driver.wait_for_response(turn, timeout_s=5)
        assert json.loads(response.text) == {"echo": "hello"}
        assert response.response_identity == "assistant-turn-1"
        driver.close_turn(turn)
    finally:
        driver.stop()
        stop.set()
        server.shutdown()
        thread.join(timeout=2)


def test_bridge_driver_poll_progress_reads_in_flight_text() -> None:
    token = "test-token"
    server = BridgeServer("127.0.0.1", 0, token)
    server.start_background()
    endpoint = f"ws://127.0.0.1:{server.port}"
    stop = threading.Event()
    release_final = threading.Event()

    def browser_worker() -> None:
        connection = connect(endpoint)
        connection.send(dumps(hello(
            role="browser", token=token,
            tunnel_ids=["chrome-remote"], browser="chrome"
        )))
        assert loads(connection.recv(timeout=5))["type"] == "hello_ack"
        try:
            while not stop.is_set():
                message = loads(connection.recv(timeout=5))
                if message.get("type") == "job":
                    connection.send(dumps({"type": "job_progress", "job_id": message["job_id"], "text": "typing…"}))
                    release_final.wait(timeout=5)
                    connection.send(dumps({
                        "type": "job_result",
                        "job_id": message["job_id"],
                        "text": "done",
                        "response_identity": "assistant-turn-1",
                    }))
        except Exception:
            pass
        finally:
            connection.close()

    thread = threading.Thread(target=browser_worker, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while server.hub.worker_for("chrome-remote") is None and time.monotonic() < deadline:
        time.sleep(0.01)

    driver = BridgeBrowserDriver(endpoint, token, "chrome-remote")
    try:
        driver.start()
        driver.health_check()
        turn = driver.begin_turn(request_id="r1", stage="planner")
        driver.submit(turn, "hello")

        # give the worker time to publish progress before it sends job_result
        progress_deadline = time.monotonic() + 3
        seen = None
        while time.monotonic() < progress_deadline:
            seen = driver.poll_progress(turn.turn_id)
            if seen == "typing…":
                break
            time.sleep(0.05)
        assert seen == "typing…"

        release_final.set()
        response = driver.wait_for_response(turn, timeout_s=5)
        assert response.text == "done"

        # progress is cleared once the job completes
        assert driver.poll_progress(turn.turn_id) is None
        driver.close_turn(turn)
    finally:
        driver.stop()
        stop.set()
        server.shutdown()
        thread.join(timeout=2)


def test_bridge_driver_rejects_unregistered_tunnel() -> None:
    token = "test-token"
    server = BridgeServer("127.0.0.1", 0, token)
    server.start_background()
    driver = BridgeBrowserDriver(f"ws://127.0.0.1:{server.port}", token, "chrome-remote")
    try:
        driver.start()
        with pytest.raises(RuntimeError, match="unavailable"):
            driver.health_check()
    finally:
        driver.stop()
        server.shutdown()


def test_custom_tunnel_catalog_is_explicit_and_validated(tmp_path, monkeypatch) -> None:
    custom = tmp_path / "tunnels.yaml"
    custom.write_text("""
tunnels:
  - id: chrome-remote-alt
    description: alternate forwarded Chrome tunnel
    runtime: extension
    transport: websocket
    scope: remote
    browser: chrome
    priority: 25
    endpoint: ws://127.0.0.1:9876
    capabilities:
      automatic: true
      remote_capable: true
      existing_session: true
      persistent_session: true
      requires_extension: true
      requires_bridge: true
      supports_ssh_forward: true
      browser_families: [chrome]
""", encoding="utf-8")
    monkeypatch.setenv("FANCY_GPT_TUNNELS_FILE", str(custom))
    registry = TunnelRegistry()
    assert registry.get("chrome-remote-alt").endpoint == "ws://127.0.0.1:9876"


def test_explicit_disabled_tunnel_is_rejected(monkeypatch) -> None:
    registry = TunnelRegistry()
    monkeypatch.setenv("FANCY_GPT_DISABLED_TUNNELS", "firefox-remote")

    def probe(spec):
        return TunnelHealth(tunnel_id=spec.id, state=TunnelHealthState.HEALTHY, detail="ok")

    resolver = TunnelResolver(registry, probe)
    with pytest.raises(RuntimeError, match="disabled"):
        resolver.select(tunnel_id="firefox-remote")


def test_bridge_refuses_non_loopback_by_default() -> None:
    with pytest.raises(ValueError, match="non-loopback"):
        BridgeServer("0.0.0.0", 8765, "token")
    server = BridgeServer("0.0.0.0", 0, "token", allow_non_loopback=True)
    assert server.host == "0.0.0.0"


def test_bridge_job_propagates_configured_timeout() -> None:
    from fancy_gpt.bridge.client import BridgeBrowserDriver
    driver = BridgeBrowserDriver("ws://127.0.0.1:1", "token", "x", job_timeout_s=777)
    assert driver.job_timeout_s == 777


def test_bridge_rejects_wildcard_worker_and_marks_stale_workers() -> None:
    from fancy_gpt.bridge.server import BridgeHub, BrowserWorker

    class Connection:
        def send(self, _message):
            pass

    hub = BridgeHub("token", stale_after_s=0.01)
    worker = BrowserWorker(Connection(), {"chrome-remote"}, "chrome")
    hub.register(worker)
    worker.last_seen -= 1
    assert hub.worker_for("chrome-remote") is None
    assert hub.snapshot()[0]["state"] == "stale"
    assert not hub.snapshot()[0]["alive"]

    # A wildcard worker is now refused at registration rather than merely
    # losing every routing decision, so it can never appear in a snapshot.
    with pytest.raises(ValueError, match="wildcard"):
        hub.register(BrowserWorker(Connection(), {"*"}, "chrome"))
    assert hub.worker_for("unregistered-tunnel") is None


def test_bridge_server_rejects_unbounded_or_invalid_timeouts() -> None:
    with pytest.raises(ValueError, match="job_timeout_s"):
        BridgeServer("127.0.0.1", 0, "token", job_timeout_s=0)


def test_bridge_hub_routes_concurrent_workers_by_exact_tunnel() -> None:
    from fancy_gpt.bridge.server import BridgeHub, BrowserWorker

    class Connection:
        def send(self, _message):
            pass

    hub = BridgeHub("token")
    chrome = BrowserWorker(Connection(), {"chrome-remote"}, "chrome")
    firefox = BrowserWorker(Connection(), {"firefox-remote"}, "firefox")
    hub.register(chrome)
    hub.register(firefox)
    assert hub.worker_for("chrome-remote") is chrome
    assert hub.worker_for("firefox-remote") is firefox
    assert len(hub.snapshot()) == 2


def test_bridge_prefers_the_worker_with_more_spare_capacity() -> None:
    from fancy_gpt.bridge.server import BridgeHub, BrowserWorker

    class Connection:
        def send(self, _message):
            pass

    hub = BridgeHub("token")
    busy = BrowserWorker(Connection(), {"edge-remote"}, "edge", max_turns=2)
    idle = BrowserWorker(Connection(), {"edge-remote"}, "edge", max_turns=2)
    busy.pending["running"] = __import__("queue").Queue(maxsize=1)
    hub.register(busy)
    hub.register(idle)

    assert hub.worker_for("edge-remote") is idle


def test_bridge_rejects_wrong_pairing_token() -> None:
    server = BridgeServer("127.0.0.1", 0, "correct-token")
    server.start_background()
    connection = connect(f"ws://127.0.0.1:{server.port}")
    try:
        connection.send(dumps(hello(role="controller", token="wrong-token")))
        result = loads(connection.recv(timeout=2))
        assert result["type"] == "error"
        assert "token" in result["error"]
    finally:
        connection.close()
        server.shutdown()


def test_bridge_snapshot_probe_lists_registered_tunnels() -> None:
    from fancy_gpt.bridge import probe_bridge_workers, available_tunnels
    token = "probe-token"
    server = BridgeServer("127.0.0.1", 0, token)
    server.start_background()
    endpoint = f"ws://127.0.0.1:{server.port}"
    stop = threading.Event()

    def browser_worker() -> None:
        connection = connect(endpoint)
        connection.send(dumps(hello(
            role="browser", token=token,
            tunnel_ids=["chrome-remote", "edge-remote"],
            browser="chrome",
        )))
        assert loads(connection.recv(timeout=5))["type"] == "hello_ack"
        try:
            while not stop.is_set():
                msg = loads(connection.recv(timeout=1))
                if msg.get("type") == "heartbeat":
                    connection.send(dumps({"type": "heartbeat", "ts": msg.get("ts")}))
        except Exception:
            pass
        finally:
            connection.close()

    thread = threading.Thread(target=browser_worker, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while server.hub.worker_for("chrome-remote") is None and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        workers = probe_bridge_workers(endpoint, token, open_timeout_s=1)
        ids = available_tunnels(workers)
        assert {"chrome-remote", "edge-remote"} <= ids
    finally:
        stop.set()
        server.shutdown()
        thread.join(timeout=2)


def test_bridge_site_health_round_trip() -> None:
    token = "health-token"
    server = BridgeServer("127.0.0.1", 0, token)
    server.start_background()
    endpoint = f"ws://127.0.0.1:{server.port}"
    stop = threading.Event()

    def browser_worker() -> None:
        connection = connect(endpoint)
        connection.send(dumps(hello(
            role="browser", token=token,
            tunnel_ids=["edge-remote"], browser="edge"
        )))
        assert loads(connection.recv(timeout=5))["type"] == "hello_ack"
        try:
            while not stop.is_set():
                message = loads(connection.recv(timeout=5))
                if message.get("type") == "job" and message.get("operation") == "site.health":
                    connection.send(dumps({
                        "type": "job_result", "job_id": message["job_id"],
                        "text": json.dumps({"ok": True, "reason": "ready"}),
                        "response_identity": "site-health",
                    }))
        except Exception:
            pass
        finally:
            connection.close()

    threading.Thread(target=browser_worker, daemon=True).start()
    deadline = time.monotonic() + 2
    while server.hub.worker_for("edge-remote") is None and time.monotonic() < deadline:
        time.sleep(0.01)
    driver = BridgeBrowserDriver(endpoint, token, "edge-remote")
    try:
        driver.start()
        driver.health_check()
        assert driver.site_health("gemini", timeout_s=5)["reason"] == "ready"
    finally:
        driver.stop()
        stop.set()
        server.shutdown()


def test_available_tunnels_rejects_wildcard_snapshot() -> None:
    from fancy_gpt.bridge.probe import available_tunnels
    import pytest

    with pytest.raises(ValueError, match="forbidden wildcard"):
        available_tunnels([{"worker_id": "legacy", "tunnel_ids": ["*"]}])
