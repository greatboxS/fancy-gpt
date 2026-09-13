"""Push-based progress, replacing a 1.5s poll.

The controller that owns a turn is blocked in ``recv()`` waiting for that turn's
terminal reply, so progress cannot arrive on the same connection. It used to be
polled instead, which put a 1.5 second floor under streaming latency.

Instead one long-lived connection per endpoint carries progress for *every*
in-flight turn, demultiplexed by job id. One connection rather than one per turn
avoids a websocket handshake on the critical path of each turn; a separate
connection rather than the primary one avoids racing the blocking read that the
turn itself depends on.

Snapshots are cumulative, so a subscriber that falls behind simply misses
intermediate ones and still converges on the same text. Nothing is queued and the
producer is never blocked.
"""

from __future__ import annotations

import threading
from typing import Callable

from websockets.sync.client import connect

from .protocol import dumps, hello, loads

#: Progress for an unknown job is normal - other turns share this connection -
#: so unroutable messages are dropped silently rather than logged.
Callback = Callable[[str], None]


class ProgressSubscription:
    """One shared push feed, fanned out to the turns that care."""

    def __init__(self, endpoint: str, token: str, tunnel_id: str, *, open_timeout_s: float = 5.0) -> None:
        self.endpoint = endpoint
        self.token = token
        self.tunnel_id = tunnel_id
        self.open_timeout_s = open_timeout_s
        self._guard = threading.Lock()
        self._callbacks: dict[str, Callback] = {}
        self._connection = None
        self._reader: threading.Thread | None = None
        self._closing = False
        self.connected = False

    # -- lifecycle -----------------------------------------------------------

    def _ensure_connected(self) -> bool:
        with self._guard:
            if self.connected and self._reader is not None and self._reader.is_alive():
                return True
            if self._closing:
                return False
        try:
            connection = connect(self.endpoint, open_timeout=self.open_timeout_s, max_size=8 * 1024 * 1024)
            connection.send(dumps(hello(role="controller", token=self.token)))
            ack = loads(connection.recv(timeout=self.open_timeout_s))
            if ack.get("type") != "hello_ack":
                connection.close()
                return False
            connection.send(dumps({"type": "subscribe", "tunnel_id": self.tunnel_id}))
            result = loads(connection.recv(timeout=self.open_timeout_s))
            if result.get("type") != "subscribe_result" or not result.get("accepted"):
                connection.close()
                return False
        except Exception:
            return False

        with self._guard:
            self._connection = connection
            self.connected = True
            self._reader = threading.Thread(target=self._read_loop, name="bridge-progress", daemon=True)
            self._reader.start()
        return True

    def _read_loop(self) -> None:
        connection = self._connection
        try:
            while connection is not None:
                message = loads(connection.recv())
                if message.get("type") != "job_progress":
                    continue
                job_id = str(message.get("job_id", ""))
                text = str(message.get("text", ""))
                with self._guard:
                    callback = self._callbacks.get(job_id)
                if callback is None:
                    continue  # another turn's progress, or one already finished
                try:
                    callback(text)
                except Exception:
                    # Progress is a monitoring aid and must never break a turn.
                    pass
        except Exception:
            pass
        finally:
            with self._guard:
                self.connected = False
                self._connection = None

    def close(self) -> None:
        with self._guard:
            self._closing = True
            connection = self._connection
            self._connection = None
            self.connected = False
            self._callbacks.clear()
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    # -- per-turn registration ----------------------------------------------

    def subscribe(self, job_id: str, callback: Callback) -> bool:
        """Route this job's progress to ``callback`` until it is released."""
        if not self._ensure_connected():
            return False
        with self._guard:
            self._callbacks[job_id] = callback
        return True

    def unsubscribe(self, job_id: str) -> None:
        """Stop routing. Called at the terminal reply, so a late snapshot for a
        finished turn cannot be delivered after its completion."""
        with self._guard:
            self._callbacks.pop(job_id, None)

    def active_jobs(self) -> list[str]:
        with self._guard:
            return sorted(self._callbacks)
