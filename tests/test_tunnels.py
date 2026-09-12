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
    assert len(ids) == 10
    assert {
        "chrome-extension-native-local",
        "edge-extension-native-local",
        "firefox-extension-native-local",
        "chrome-extension-ws-remote",
        "edge-extension-ws-remote",
        "firefox-extension-ws-remote",
        "chrome-cdp-local",
        "playwright-chromium-local",
        "playwright-firefox-local",
        "interactive-manual",
    } == ids


def test_tunnel_resolver_policy_and_explicit_selection() -> None:
    registry = TunnelRegistry()

    def probe(spec):
        state = TunnelHealthState.HEALTHY if spec.id in {
            "chrome-extension-ws-remote", "playwright-chromium-local"
        } else TunnelHealthState.UNAVAILABLE
        return TunnelHealth(tunnel_id=spec.id, state=state, detail=state.value)

    resolver = TunnelResolver(registry, probe)
    selected = resolver.select(policy="prefer-remote")
    assert selected.tunnel_id == "chrome-extension-ws-remote"
    explicit = resolver.select(tunnel_id="playwright-chromium-local")
    assert explicit.explicit is True
    assert explicit.tunnel_id == "playwright-chromium-local"


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
            tunnel_ids=["chrome-extension-ws-remote"], browser="chrome"
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
    while server.hub.worker_for("chrome-extension-ws-remote") is None and time.monotonic() < deadline:
        time.sleep(0.01)

    driver = BridgeBrowserDriver(endpoint, token, "chrome-extension-ws-remote")
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


def test_bridge_driver_rejects_unregistered_tunnel() -> None:
    token = "test-token"
    server = BridgeServer("127.0.0.1", 0, token)
    server.start_background()
    driver = BridgeBrowserDriver(f"ws://127.0.0.1:{server.port}", token, "chrome-extension-ws-remote")
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
  - id: chrome-extension-ws-remote-alt
    description: alternate forwarded Chrome tunnel
    site: chatgpt
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
    assert registry.get("chrome-extension-ws-remote-alt").endpoint == "ws://127.0.0.1:9876"


def test_explicit_disabled_tunnel_is_rejected(monkeypatch) -> None:
    registry = TunnelRegistry()
    monkeypatch.setenv("FANCY_GPT_DISABLED_TUNNELS", "playwright-chromium-local")

    def probe(spec):
        return TunnelHealth(tunnel_id=spec.id, state=TunnelHealthState.HEALTHY, detail="ok")

    resolver = TunnelResolver(registry, probe)
    with pytest.raises(RuntimeError, match="disabled"):
        resolver.select(tunnel_id="playwright-chromium-local")


def test_bridge_refuses_non_loopback_by_default() -> None:
    with pytest.raises(ValueError, match="non-loopback"):
        BridgeServer("0.0.0.0", 8765, "token")
    server = BridgeServer("0.0.0.0", 0, "token", allow_non_loopback=True)
    assert server.host == "0.0.0.0"


def test_bridge_job_propagates_configured_timeout() -> None:
    from fancy_gpt.bridge.client import BridgeBrowserDriver
    driver = BridgeBrowserDriver("ws://127.0.0.1:1", "token", "x", job_timeout_s=777)
    assert driver.job_timeout_s == 777


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
            tunnel_ids=["chrome-extension-ws-remote", "edge-extension-ws-remote"],
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
    while server.hub.worker_for("chrome-extension-ws-remote") is None and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        workers = probe_bridge_workers(endpoint, token, open_timeout_s=1)
        ids = available_tunnels(workers)
        assert {"chrome-extension-ws-remote", "edge-extension-ws-remote"} <= ids
    finally:
        stop.set()
        server.shutdown()


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
            tunnel_ids=["edge-extension-ws-remote"], browser="edge"
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
    while server.hub.worker_for("edge-extension-ws-remote") is None and time.monotonic() < deadline:
        time.sleep(0.01)
    driver = BridgeBrowserDriver(endpoint, token, "edge-extension-ws-remote")
    try:
        driver.start()
        driver.health_check()
        assert driver.site_health(timeout_s=5)["reason"] == "ready"
    finally:
        driver.stop()
        stop.set()
        server.shutdown()
