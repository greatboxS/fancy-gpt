from __future__ import annotations

import time

from websockets.sync.client import connect

from .protocol import dumps, hello, loads


def probe_bridge_workers(endpoint: str, token: str, *, open_timeout_s: float = 1.0) -> list[dict]:
    """Return a bridge worker snapshot using one short-lived controller session."""
    connection = connect(endpoint, open_timeout=open_timeout_s, max_size=8 * 1024 * 1024)
    try:
        connection.send(dumps(hello(role="controller", token=token)))
        ack = loads(connection.recv(timeout=open_timeout_s))
        if ack.get("type") != "hello_ack":
            raise RuntimeError(f"bridge refused probe controller: {ack}")
        connection.send(dumps({"type": "workers"}))
        result = loads(connection.recv(timeout=open_timeout_s))
        if result.get("type") != "workers_result":
            raise RuntimeError(f"unexpected bridge workers response: {result}")
        workers = result.get("workers", [])
        if not isinstance(workers, list):
            raise RuntimeError("bridge workers response is malformed")
        return [item for item in workers if isinstance(item, dict)]
    finally:
        connection.close()


def probe_bridge_stats(endpoint: str, token: str, *, open_timeout_s: float = 1.0) -> dict:
    """Return the bridge hub's aggregate stats plus current worker snapshot."""
    connection = connect(endpoint, open_timeout=open_timeout_s, max_size=8 * 1024 * 1024)
    try:
        connection.send(dumps(hello(role="controller", token=token)))
        ack = loads(connection.recv(timeout=open_timeout_s))
        if ack.get("type") != "hello_ack":
            raise RuntimeError(f"bridge refused probe controller: {ack}")
        connection.send(dumps({"type": "stats"}))
        result = loads(connection.recv(timeout=open_timeout_s))
        if result.get("type") != "stats_result":
            raise RuntimeError(f"unexpected bridge stats response: {result}")
        stats = result.get("stats", {})
        workers = result.get("workers", [])
        if not isinstance(stats, dict) or not isinstance(workers, list):
            raise RuntimeError("bridge stats response is malformed")
        return {"stats": stats, "workers": [item for item in workers if isinstance(item, dict)]}
    finally:
        connection.close()


def send_job_cancel(
    endpoint: str,
    token: str,
    tunnel_id: str,
    job_id: str,
    *,
    generation_epoch: int = 0,
    reason: str = "cancelled",
    open_timeout_s: float = 2.0,
) -> bool:
    """Ask the browser to stop generating `job_id`.

    Uses its own short-lived connection so it can be sent while the controller
    that owns the turn is still blocked waiting for that turn's reply.
    Returns True when the bridge accepted and forwarded it to a worker.
    """
    with connect(endpoint, open_timeout=open_timeout_s, max_size=1 << 20) as connection:
        connection.send(dumps(hello(role="controller", token=token)))
        ack = loads(connection.recv(timeout=open_timeout_s))
        if ack.get("type") != "hello_ack":
            raise RuntimeError(f"bridge refused cancel controller: {ack}")
        connection.send(dumps({
            "type": "cancel",
            "tunnel_id": tunnel_id,
            "job_id": job_id,
            "generation_epoch": generation_epoch,
            "reason": reason,
        }))
        result = loads(connection.recv(timeout=open_timeout_s))
        if result.get("type") != "cancel_result":
            raise RuntimeError(f"unexpected bridge cancel response: {result}")
        return bool(result.get("accepted"))


def send_extension_reload(
    endpoint: str, token: str, tunnel_id: str, *, expected_build: str,
    open_timeout_s: float = 5.0, reconnect_timeout_s: float = 15.0,
) -> dict:
    """Reload, then prove a new worker with the expected build reconnected."""
    before = [
        worker for worker in probe_bridge_workers(endpoint, token, open_timeout_s=open_timeout_s)
        if tunnel_id in worker.get("tunnel_ids", []) and worker.get("alive", True)
    ]
    old_ids = {str(worker.get("worker_id")) for worker in before}
    with connect(endpoint, open_timeout=open_timeout_s, max_size=1 << 20) as connection:
        connection.send(dumps(hello(role="controller", token=token)))
        ack = loads(connection.recv(timeout=open_timeout_s))
        if ack.get("type") != "hello_ack":
            raise RuntimeError(f"bridge refused reload controller: {ack}")
        connection.send(dumps({"type": "extension.reload", "tunnel_id": tunnel_id}))
        result = loads(connection.recv(timeout=open_timeout_s))
        if result.get("type") != "reload_result":
            raise RuntimeError(f"unexpected extension reload response: {result}")
        if not result.get("accepted"):
            return result
    deadline = time.monotonic() + reconnect_timeout_s
    while time.monotonic() < deadline:
        try:
            workers = probe_bridge_workers(endpoint, token, open_timeout_s=1.0)
        except Exception:
            time.sleep(0.2)
            continue
        replacement = next((
            worker for worker in workers
            if tunnel_id in worker.get("tunnel_ids", [])
            and str(worker.get("worker_id")) not in old_ids
            and worker.get("build") == expected_build
        ), None)
        if replacement is not None:
            return {
                "type": "reload_result", "accepted": True, "verified": True,
                "worker_id": replacement.get("worker_id"), "build": replacement.get("build"),
            }
        time.sleep(0.2)
    return {
        "type": "reload_result", "accepted": False, "verified": False,
        "reason": f"no new worker reconnected with build {expected_build}",
    }


def fetch_job_progress(
    endpoint: str, token: str, tunnel_id: str, job_id: str, *, open_timeout_s: float = 1.0
) -> dict | None:
    """Return the latest in-flight text for `job_id`, or None if no progress recorded yet.

    Uses its own short-lived connection so it never contends with the
    long-running controller connection that is blocked waiting on the job's
    final result.
    """
    connection = connect(endpoint, open_timeout=open_timeout_s, max_size=8 * 1024 * 1024)
    try:
        connection.send(dumps(hello(role="controller", token=token)))
        ack = loads(connection.recv(timeout=open_timeout_s))
        if ack.get("type") != "hello_ack":
            raise RuntimeError(f"bridge refused progress controller: {ack}")
        connection.send(dumps({"type": "progress", "tunnel_id": tunnel_id, "job_id": job_id}))
        result = loads(connection.recv(timeout=open_timeout_s))
        if result.get("type") != "progress_result":
            raise RuntimeError(f"unexpected bridge progress response: {result}")
        progress = result.get("progress")
        return progress if isinstance(progress, dict) else None
    finally:
        connection.close()


def available_tunnels(workers: list[dict]) -> set[str]:
    result: set[str] = set()
    for worker in workers:
        # A snapshot still lists workers that went stale so operators can see
        # them; those must never count as an available tunnel.
        if not worker.get("alive", True):
            continue
        for item in worker.get("tunnel_ids", []):
            if item == "*":
                raise ValueError("bridge worker snapshot contains forbidden wildcard tunnel id")
            if isinstance(item, str) and item:
                result.add(item)
    return result
