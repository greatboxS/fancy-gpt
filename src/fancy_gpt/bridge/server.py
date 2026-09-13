from __future__ import annotations

import ipaddress
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from websockets.sync.server import ServerConnection, serve

from .protocol import PROTOCOL_VERSION, dumps, loads


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class BrowserWorker:
    connection: ServerConnection
    tunnel_ids: set[str]
    browser: str
    worker_id: str = field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:10]}")
    connected_at: float = field(default_factory=time.monotonic)
    connected_at_wall: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.monotonic)
    send_lock: threading.Lock = field(default_factory=threading.Lock)
    request_lock: threading.Lock = field(default_factory=threading.Lock)
    pending: dict[str, queue.Queue[dict[str, Any]]] = field(default_factory=dict)
    alive: bool = True
    jobs_started: int = 0
    jobs_succeeded: int = 0
    jobs_failed: int = 0

    def send(self, message: dict[str, Any]) -> None:
        with self.send_lock:
            self.connection.send(dumps(message))

    def request(self, message: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        """Send a job and wait for its reply.

        The lock is held only to register the pending queue and touch counters,
        never across the wait. Holding it for the whole job would make one
        worker strictly single-job: a second caller could not even register its
        queue, so a reply arriving for it would find no destination and be
        dropped, and it would then wait out its full timeout for a response that
        had already come and gone.
        """
        job_id = str(message["job_id"])
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self.request_lock:
            if job_id in self.pending:
                raise ValueError(f"job {job_id} is already in flight")
            self.pending[job_id] = response_queue
            self.jobs_started += 1
        try:
            self.send(message)
            response = response_queue.get(timeout=timeout_s)
        except queue.Empty as exc:
            with self.request_lock:
                self.jobs_failed += 1
            raise TimeoutError(f"browser worker timed out for job {job_id}") from exc
        except Exception:
            with self.request_lock:
                self.jobs_failed += 1
            raise
        else:
            with self.request_lock:
                if response.get("type") == "job_error":
                    self.jobs_failed += 1
                else:
                    self.jobs_succeeded += 1
            return response
        finally:
            with self.request_lock:
                self.pending.pop(job_id, None)

    def send_control(self, message: dict[str, Any]) -> None:
        """Fire-and-forget message to the worker, outside any job's reply path.

        Cancellation uses this: the turn's own reply still returns on the
        request that is waiting for it, so the control message must not consume
        or register a pending slot.
        """
        self.send(message)

    def dispatch(self, message: dict[str, Any]) -> None:
        job_id = str(message.get("job_id", ""))
        with self.request_lock:
            target = self.pending.get(job_id)
        if target is not None:
            try:
                target.put_nowait(message)
            except queue.Full:
                pass


class BridgeHub:
    def __init__(self, token: str, *, stale_after_s: float = 45.0) -> None:
        if stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive")
        self.token = token
        self.stale_after_s = stale_after_s
        self.started_at = time.time()
        self._workers: list[BrowserWorker] = []
        self._lock = threading.RLock()
        self._total_connections = 0
        self._total_jobs_started = 0
        self._total_jobs_succeeded = 0
        self._total_jobs_failed = 0
        self._total_probes = 0
        self._progress: dict[str, tuple[float, str]] = {}
        self._progress_cap = 200
        # Controllers that asked to be pushed progress instead of polling
        # for it. Each carries its own send lock, because a push happens on
        # the worker's thread while the controller's own thread is blocked
        # in recv().
        self._subscribers: dict[int, tuple[Any, threading.Lock, str]] = {}

    def _fresh(self, worker: BrowserWorker) -> bool:
        return worker.alive and (time.monotonic() - worker.last_seen) <= self.stale_after_s

    def _expire_stale_unlocked(self) -> None:
        """Demote workers that stopped sending anything; callers hold the lock.

        A stale worker stays in the snapshot as `state: stale` so operators can
        see it went quiet, but never wins routing again.
        """
        now = time.monotonic()
        for worker in self._workers:
            if worker.alive and now - worker.last_seen > self.stale_after_s:
                worker.alive = False

    def register(self, worker: BrowserWorker) -> None:
        if not worker.tunnel_ids:
            raise ValueError("browser worker must register at least one exact tunnel id")
        if "*" in worker.tunnel_ids:
            raise ValueError("wildcard tunnel registration is forbidden")
        with self._lock:
            self._workers.append(worker)
            self._total_connections += 1

    def record_probe(self) -> None:
        with self._lock:
            self._total_probes += 1

    def record_job_start(self) -> None:
        with self._lock:
            self._total_jobs_started += 1

    def record_job_result(self, success: bool) -> None:
        with self._lock:
            if success:
                self._total_jobs_succeeded += 1
            else:
                self._total_jobs_failed += 1

    def add_subscriber(self, connection: Any, tunnel_id: str) -> int:
        key = id(connection)
        with self._lock:
            self._subscribers[key] = (connection, threading.Lock(), tunnel_id)
        return key

    def remove_subscriber(self, key: int) -> None:
        with self._lock:
            self._subscribers.pop(key, None)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def record_progress(self, job_id: str, text: str) -> None:
        with self._lock:
            self._progress[job_id] = (time.time(), text)
            if len(self._progress) > self._progress_cap:
                oldest = min(self._progress, key=lambda key: self._progress[key][0])
                self._progress.pop(oldest, None)
            subscribers = list(self._subscribers.items())
        if not subscribers:
            return
        # Snapshots are cumulative, so a subscriber that cannot keep up simply
        # misses intermediate ones and still converges. The producer is never
        # blocked and a dead subscriber is dropped rather than retried.
        message = dumps({"type": "job_progress", "job_id": job_id, "text": text})
        for key, (connection, lock, _tunnel) in subscribers:
            try:
                if lock.acquire(timeout=0.05):
                    try:
                        connection.send(message)
                    finally:
                        lock.release()
            except Exception:
                self.remove_subscriber(key)

    def get_progress(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._progress.get(job_id)
        if entry is None:
            return None
        updated_at, text = entry
        return {"job_id": job_id, "text": text, "updated_at": updated_at}

    def clear_progress(self, job_id: str) -> None:
        with self._lock:
            self._progress.pop(job_id, None)

    def unregister(self, worker: BrowserWorker) -> None:
        worker.alive = False
        with self._lock:
            if worker in self._workers:
                self._workers.remove(worker)

    def worker_for(self, tunnel_id: str) -> BrowserWorker | None:
        with self._lock:
            self._expire_stale_unlocked()
            candidates = [worker for worker in self._workers if self._fresh(worker) and tunnel_id in worker.tunnel_ids]
            if not candidates:
                return None
            return max(candidates, key=lambda worker: worker.last_seen)
        return None

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            self._expire_stale_unlocked()
            return [
                {
                    "worker_id": worker.worker_id,
                    "browser": worker.browser,
                    "tunnel_ids": sorted(worker.tunnel_ids),
                    "alive": worker.alive,
                    "state": "connected" if worker.alive else "stale",
                    "fresh": self._fresh(worker),
                    "connected_at": worker.connected_at_wall,
                    "connected_seconds": round(now - worker.connected_at, 1),
                    "last_seen_seconds_ago": round(now - worker.last_seen, 1),
                    "jobs_started": worker.jobs_started,
                    "jobs_succeeded": worker.jobs_succeeded,
                    "jobs_failed": worker.jobs_failed,
                }
                for worker in self._workers
            ]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._expire_stale_unlocked()
            for worker in self._workers:
                if worker.alive and now - worker.last_seen > self.stale_after_s:
                    worker.alive = False
            return {
                "started_at": self.started_at,
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "connected_workers": sum(1 for worker in self._workers if worker.alive),
                "total_connections_seen": self._total_connections,
                "total_probe_requests": self._total_probes,
                "total_jobs_started": self._total_jobs_started,
                "total_jobs_succeeded": self._total_jobs_succeeded,
                "total_jobs_failed": self._total_jobs_failed,
            }


class BridgeServer:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        # Slightly above the controller's own backstop, so a turn that does
        # end is reported by the component that knows why rather than being
        # cut off here with a generic message.
        job_timeout_s: float = 1860.0,
        worker_stale_after_s: float = 45.0,
        allow_non_loopback: bool = False,
    ) -> None:
        if not allow_non_loopback and not _is_loopback_host(host):
            raise ValueError(
                "bridge refuses non-loopback bind by default; use SSH forwarding or explicitly allow non-loopback exposure"
            )
        self.host = host
        self.port = port
        if job_timeout_s <= 0:
            raise ValueError("job_timeout_s must be positive")
        self.hub = BridgeHub(token, stale_after_s=worker_stale_after_s)
        self.job_timeout_s = job_timeout_s
        self._server: Any = None

    def _authenticate(self, connection: ServerConnection) -> dict[str, Any]:
        first = loads(connection.recv(timeout=10))
        if first.get("type") != "hello" or first.get("protocol") != PROTOCOL_VERSION:
            raise PermissionError("invalid bridge hello")
        if first.get("token") != self.hub.token:
            raise PermissionError("invalid bridge token")
        return first

    def _browser_session(self, connection: ServerConnection, hello: dict[str, Any]) -> None:
        worker = BrowserWorker(
            connection=connection,
            tunnel_ids=set(str(item) for item in hello.get("tunnel_ids", [])),
            browser=str(hello.get("browser") or "unknown"),
        )
        if "*" in worker.tunnel_ids:
            raise ValueError("browser worker must register exact tunnel ids; wildcard is forbidden")
        self.hub.register(worker)
        connection.send(dumps({"type": "hello_ack", "protocol": PROTOCOL_VERSION, "role": "browser"}))
        try:
            while True:
                message = loads(connection.recv())
                worker.last_seen = time.monotonic()
                if message.get("type") == "heartbeat":
                    worker.send({"type": "heartbeat_ack", "ts": message.get("ts")})
                    continue
                # job_cancelled is terminal too. Leaving it out meant a
                # stopped turn produced no reply at all, so the controller
                # waited out its whole timeout for a turn that had already
                # ended in the browser.
                if message.get("type") in {"job_result", "job_error", "job_cancelled"}:
                    worker.dispatch(message)
                elif message.get("type") == "job_progress":
                    job_id = str(message.get("job_id", ""))
                    if job_id:
                        self.hub.record_progress(job_id, str(message.get("text", "")))
        except Exception:
            pass
        finally:
            self.hub.unregister(worker)

    def _controller_session(self, connection: ServerConnection) -> None:
        connection.send(dumps({"type": "hello_ack", "protocol": PROTOCOL_VERSION, "role": "controller"}))
        subscriber_key: int | None = None
        try:
            self._controller_loop(connection)
        finally:
            if subscriber_key is not None:
                self.hub.remove_subscriber(subscriber_key)
            # A controller that subscribed is identified by its connection, so
            # dropping it by identity is enough even if the loop never saw the
            # key assignment.
            self.hub.remove_subscriber(id(connection))

    def _controller_loop(self, connection: ServerConnection) -> None:
        subscriber_key: int | None = None
        while True:
            message = loads(connection.recv())
            msg_type = message.get("type")
            if msg_type == "probe":
                self.hub.record_probe()
                tunnel_id = str(message.get("tunnel_id", ""))
                worker = self.hub.worker_for(tunnel_id)
                connection.send(dumps({
                    "type": "probe_result",
                    "tunnel_id": tunnel_id,
                    "available": worker is not None,
                    "workers": self.hub.snapshot(),
                }))
            elif msg_type == "job":
                tunnel_id = str(message.get("tunnel_id", ""))
                worker = self.hub.worker_for(tunnel_id)
                if worker is None:
                    self.hub.record_job_result(False)
                    connection.send(dumps({
                        "type": "job_error",
                        "job_id": message.get("job_id"),
                        "error": f"no browser worker connected for tunnel {tunnel_id}",
                    }))
                    continue
                self.hub.record_job_start()
                try:
                    requested_timeout = float(message.get("timeout_s", self.job_timeout_s))
                    if requested_timeout <= 0:
                        raise ValueError("job timeout must be positive")
                    effective_timeout = max(1.0, min(requested_timeout, self.job_timeout_s))
                    response = worker.request(message, timeout_s=effective_timeout)
                    self.hub.record_job_result(response.get("type") != "job_error")
                except Exception as exc:
                    response = {"type": "job_error", "job_id": message.get("job_id"), "error": str(exc)}
                    self.hub.record_job_result(False)
                finally:
                    self.hub.clear_progress(str(message.get("job_id", "")))
                connection.send(dumps(response))
            elif msg_type == "workers":
                connection.send(dumps({"type": "workers_result", "workers": self.hub.snapshot()}))
            elif msg_type == "stats":
                connection.send(dumps({"type": "stats_result", "stats": self.hub.stats(), "workers": self.hub.snapshot()}))
            elif msg_type == "subscribe":
                # This connection becomes a progress feed. Its recv() below keeps
                # blocking; pushes are written from the worker's thread under
                # this subscriber's own lock.
                tunnel_id = str(message.get("tunnel_id", ""))
                subscriber_key = self.hub.add_subscriber(connection, tunnel_id)
                connection.send(dumps({"type": "subscribe_result", "accepted": True}))
            elif msg_type == "cancel":
                # Cancellation is out of band on purpose: the turn's own reply
                # still returns on whichever request is waiting for it, so this
                # must not consume that pending slot.
                tunnel_id = str(message.get("tunnel_id", ""))
                job_id = str(message.get("job_id", ""))
                worker = self.hub.worker_for(tunnel_id)
                if worker is None or not job_id:
                    connection.send(dumps({
                        "type": "cancel_result", "job_id": job_id, "accepted": False,
                        "reason": "no browser worker connected" if worker is None else "job_id is required",
                    }))
                    continue
                worker.send_control({
                    "type": "cancel",
                    "job_id": job_id,
                    "generation_epoch": message.get("generation_epoch", 0),
                    "reason": str(message.get("reason", "cancelled")),
                })
                connection.send(dumps({"type": "cancel_result", "job_id": job_id, "accepted": True}))
            elif msg_type == "progress":
                job_id = str(message.get("job_id", ""))
                progress = self.hub.get_progress(job_id)
                connection.send(dumps({"type": "progress_result", "job_id": job_id, "progress": progress}))
            else:
                connection.send(dumps({"type": "error", "error": f"unsupported controller message: {msg_type}"}))

    def handler(self, connection: ServerConnection) -> None:
        try:
            hello = self._authenticate(connection)
            role = hello.get("role")
            if role == "browser":
                self._browser_session(connection, hello)
            elif role == "controller":
                self._controller_session(connection)
            else:
                raise PermissionError(f"unsupported bridge role: {role}")
        except Exception as exc:
            try:
                connection.send(dumps({"type": "error", "error": str(exc)}))
            except Exception:
                pass

    def serve_forever(self) -> None:
        with serve(self.handler, self.host, self.port, max_size=8 * 1024 * 1024) as server:
            self._server = server
            # Preserve an ephemeral port selected with port=0 for tests/embedding.
            self.port = int(server.socket.getsockname()[1])
            server.serve_forever()

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="fancy-gpt-bridge", daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        while (self._server is None or self.port == 0) and time.monotonic() < deadline:
            time.sleep(0.01)
        if self._server is None or self.port == 0:
            raise RuntimeError("bridge server did not start")
        return thread

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
