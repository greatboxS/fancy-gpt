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


def available_tunnels(workers: list[dict]) -> set[str]:
    result: set[str] = set()
    for worker in workers:
        for item in worker.get("tunnel_ids", []):
            if item == "*":
                raise ValueError("bridge worker snapshot contains forbidden wildcard tunnel id")
            if isinstance(item, str) and item:
                result.add(item)
    return result
