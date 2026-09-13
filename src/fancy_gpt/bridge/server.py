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
    max_turns: int = 1
    max_render_slots: int = 1
    sites: tuple[str, ...] = ()
    surface_mode: str = "legacy"
    build: str | None = None
    worker_id: str = field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:10]}")
    connected_at: float = field(default_factory=time.monotonic)
    connected_at_wall: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.monotonic)
    send_lock: threading.Lock = field(default_factory=threading.Lock)
    request_lock: threading.Lock = field(default_factory=threading.Lock)
    pending: dict[str, queue.Queue[dict[str, Any]]] = field(default_factory=dict)
    job_tunnels: dict[str, str] = field(default_factory=dict)
    #: Replies to control messages (cancel), kept apart from job replies so
    #: a control round trip can never consume a turn's own response.
    control_pending: dict[str, queue.Queue[dict[str, Any]]] = field(default_factory=dict)
    alive: bool = True
    jobs_started: int = 0
    jobs_succeeded: int = 0
    jobs_failed: int = 0
    capacity_slots: threading.BoundedSemaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_turns < 1:
            raise ValueError("browser worker max_turns must be positive")
        self.capacity_slots = threading.BoundedSemaphore(self.max_turns)

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
        started_waiting = time.monotonic()
        if not self.capacity_slots.acquire(timeout=timeout_s):
            raise TimeoutError(f"browser worker capacity wait timed out for job {job_id}")
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        try:
            with self.request_lock:
                if job_id in self.pending:
                    raise ValueError(f"job {job_id} is already in flight")
                self.pending[job_id] = response_queue
                self.job_tunnels[job_id] = str(message.get("tunnel_id", ""))
                self.jobs_started += 1
            self.send(message)
            remaining = max(0.0, timeout_s - (time.monotonic() - started_waiting))
            response = response_queue.get(timeout=remaining)
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
                self.job_tunnels.pop(job_id, None)
            self.capacity_slots.release()

    def send_control(self, message: dict[str, Any]) -> None:
        """Fire-and-forget message to the worker, outside any job's reply path."""
        self.send(message)

    def request_control(self, message: dict[str, Any], timeout_s: float = 5.0) -> dict[str, Any] | None:
        """Send a control message and wait for the worker's own answer.

        Acknowledging a cancel the moment it is forwarded tells the caller it
        worked when the extension may have ignored it - for an unknown job, or
        a stale generation. The answer has to come from the side that acted.

        Returns None if the worker does not answer in time.
        """
        control_id = str(message.get("control_id") or uuid.uuid4().hex)
        message = {**message, "control_id": control_id}
        reply: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self.request_lock:
            if control_id in self.control_pending:
                raise ValueError(f"control request {control_id} is already in flight")
            self.control_pending[control_id] = reply
        try:
            self.send(message)
            return reply.get(timeout=timeout_s)
        except queue.Empty:
            return None
        finally:
            with self.request_lock:
                self.control_pending.pop(control_id, None)

    def dispatch_control(self, message: dict[str, Any]) -> None:
        control_id = str(message.get("control_id", ""))
        with self.request_lock:
            target = self.control_pending.get(control_id)
        if target is not None:
            try:
                target.put_nowait(message)
            except queue.Full:
                pass

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
        self._progress: dict[tuple[str, str], tuple[float, str]] = {}
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
        if not worker.tunnel_ids or any(not item for item in worker.tunnel_ids):
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
        if not tunnel_id:
            raise ValueError("progress subscription requires an exact tunnel id")
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

    def record_progress(self, job_id: str, text: str, tunnel_id: str) -> None:
        if not tunnel_id:
            return
        progress_key = (tunnel_id, job_id)
        with self._lock:
            self._progress[progress_key] = (time.time(), text)
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
        for key, (connection, lock, subscriber_tunnel) in subscribers:
            # A subscriber only ever sees its own tunnel's progress. Relying on
            # the client to discard messages for other jobs is not
            # confidentiality: it leaves the server handing one caller another
            # caller's partial output.
            if subscriber_tunnel != tunnel_id:
                continue
            try:
                if lock.acquire(timeout=0.05):
                    try:
                        connection.send(message)
                    finally:
                        lock.release()
            except Exception:
                self.remove_subscriber(key)

    def get_progress(self, tunnel_id: str, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._progress.get((tunnel_id, job_id))
        if entry is None:
            return None
        updated_at, text = entry
        return {"job_id": job_id, "text": text, "updated_at": updated_at}

    def clear_progress(self, tunnel_id: str, job_id: str) -> None:
        with self._lock:
            self._progress.pop((tunnel_id, job_id), None)

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
            return min(
                candidates,
                key=lambda worker: (len(worker.pending) / worker.max_turns, -worker.last_seen),
            )
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
                    "active_jobs": len(worker.pending),
                    "max_turns": worker.max_turns,
                    "max_render_slots": worker.max_render_slots,
                    "sites": list(worker.sites),
                    "surface_mode": worker.surface_mode,
                    "build": worker.build,
                }
                for worker in self._workers
            ]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            # _expire_stale_unlocked already ages workers out. The duplicate
            # loop that used to be here referenced an undefined `now`, so this
            # raised as soon as any worker was actually alive - the one state in
            # which the endpoint matters.
            self._expire_stale_unlocked()
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
        capabilities = hello.get("capabilities") if isinstance(hello.get("capabilities"), dict) else {}
        max_turns = max(1, min(16, int(capabilities.get("max_turns", 1))))
        max_render_slots = max(1, min(max_turns, int(capabilities.get("max_render_slots", max_turns))))
        worker = BrowserWorker(
            connection=connection,
            tunnel_ids=set(str(item) for item in hello.get("tunnel_ids", [])),
            browser=str(hello.get("browser") or "unknown"),
            max_turns=max_turns,
            max_render_slots=max_render_slots,
            sites=tuple(sorted(str(item) for item in capabilities.get("sites", []) if item)),
            surface_mode=str(capabilities.get("surface_mode") or "legacy"),
            build=str(capabilities.get("build")) if capabilities.get("build") else None,
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
                if message.get("type") in {"cancel_result", "reload_result"}:
                    worker.dispatch_control(message)
                elif message.get("type") in {"job_result", "job_error", "job_cancelled"}:
                    worker.dispatch(message)
                elif message.get("type") == "job_progress":
                    job_id = str(message.get("job_id", ""))
                    if job_id:
                        # Attribute progress to the tunnel it came from, so it
                        # is only ever delivered to subscribers of that tunnel.
                        with worker.request_lock:
                            origin = worker.job_tunnels.get(job_id, "")
                        self.hub.record_progress(job_id, str(message.get("text", "")), origin)
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
                    self.hub.clear_progress(tunnel_id, str(message.get("job_id", "")))
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
                if not tunnel_id:
                    connection.send(dumps({"type": "subscribe_result", "accepted": False, "reason": "tunnel_id is required"}))
                    continue
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
                answer = worker.request_control({
                    "type": "cancel",
                    "job_id": job_id,
                    "control_id": uuid.uuid4().hex,
                    "generation_epoch": message.get("generation_epoch", 0),
                    "reason": str(message.get("reason", "cancelled")),
                })
                if answer is None:
                    connection.send(dumps({
                        "type": "cancel_result", "job_id": job_id, "accepted": False,
                        "reason": "the browser did not answer the cancel",
                    }))
                else:
                    connection.send(dumps({
                        "type": "cancel_result", "job_id": job_id,
                        "accepted": bool(answer.get("accepted")),
                        "reason": str(answer.get("reason", "")),
                    }))
            elif msg_type == "extension.reload":
                tunnel_id = str(message.get("tunnel_id", ""))
                worker = self.hub.worker_for(tunnel_id)
                if worker is None:
                    connection.send(dumps({
                        "type": "reload_result", "accepted": False,
                        "reason": "no browser worker connected",
                    }))
                    continue
                answer = worker.request_control({
                    "type": "extension.reload", "control_id": uuid.uuid4().hex,
                })
                connection.send(dumps(answer or {
                    "type": "reload_result", "accepted": False,
                    "reason": "the browser did not acknowledge reload",
                }))
            elif msg_type == "progress":
                tunnel_id = str(message.get("tunnel_id", ""))
                job_id = str(message.get("job_id", ""))
                progress = self.hub.get_progress(tunnel_id, job_id) if tunnel_id else None
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
