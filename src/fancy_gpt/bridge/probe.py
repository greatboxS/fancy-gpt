from __future__ import annotations

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


def fetch_job_progress(endpoint: str, token: str, job_id: str, *, open_timeout_s: float = 1.0) -> dict | None:
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
        connection.send(dumps({"type": "progress", "job_id": job_id}))
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
        if not worker.get("alive", True):
            continue
        for item in worker.get("tunnel_ids", []):
            if isinstance(item, str) and item != "*":
                result.add(item)
    return result
