"""One browser worker must be able to carry several jobs at once.

The worker's lock previously spanned the whole job, which made a single worker
strictly one-job-at-a-time and silently dropped replies for any caller that had
not managed to register yet.
"""

from __future__ import annotations

import threading
import time

import pytest

from fancy_gpt.bridge.server import BrowserWorker


class FakeConnection:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, raw: str) -> None:
        self.sent.append(raw)


def make_worker() -> BrowserWorker:
    return BrowserWorker(connection=FakeConnection(), tunnel_ids={"edge-remote"}, browser="edge")


def _answer_when_registered(worker: BrowserWorker, work_s: float, stop: threading.Event) -> None:
    """Stand in for the browser: reply `work_s` after a job actually appears."""
    seen: set[str] = set()
    while not stop.wait(0.01):
        for job_id in list(worker.pending):
            if job_id not in seen:
                seen.add(job_id)
                threading.Timer(
                    work_s, worker.dispatch, args=({"type": "job_result", "job_id": job_id, "text": "ok"},)
                ).start()


def test_one_worker_runs_several_jobs_concurrently() -> None:
    worker = make_worker()
    stop = threading.Event()
    threading.Thread(target=_answer_when_registered, args=(worker, 0.3, stop), daemon=True).start()
    finished: dict[str, float] = {}

    def run(job_id: str) -> None:
        worker.request({"type": "job", "job_id": job_id}, timeout_s=10)
        finished[job_id] = time.monotonic() - started

    started = time.monotonic()
    threads = [threading.Thread(target=run, args=(f"job-{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    total = time.monotonic() - started

    assert len(finished) == 4
    # Serialized would be ~1.2s for four 0.3s jobs; concurrent is ~0.3s.
    assert total < 0.7, f"jobs appear serialized: {total:.2f}s for 4 x 0.3s"
    stop.set()


def test_a_reply_is_not_dropped_while_another_job_is_waiting() -> None:
    """The dropped-dispatch bug: a reply arriving for an unregistered job."""
    worker = make_worker()
    slow_done = threading.Event()

    def slow() -> None:
        try:
            worker.request({"type": "job", "job_id": "slow"}, timeout_s=5)
        finally:
            slow_done.set()

    threading.Thread(target=slow, daemon=True).start()
    # Let the slow job register and start waiting.
    deadline = time.monotonic() + 2
    while "slow" not in worker.pending and time.monotonic() < deadline:
        time.sleep(0.01)

    result: list[dict] = []

    def fast() -> None:
        result.append(worker.request({"type": "job", "job_id": "fast"}, timeout_s=5))

    fast_thread = threading.Thread(target=fast)
    fast_thread.start()
    while "fast" not in worker.pending and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.dispatch({"type": "job_result", "job_id": "fast", "text": "answered"})
    fast_thread.join(timeout=5)

    assert result and result[0]["text"] == "answered"
    worker.dispatch({"type": "job_result", "job_id": "slow", "text": "later"})
    assert slow_done.wait(timeout=5)


def test_counters_stay_consistent_under_concurrency() -> None:
    worker = make_worker()
    stop = threading.Event()
    threading.Thread(target=_answer_when_registered, args=(worker, 0.05, stop), daemon=True).start()

    def run(job_id: str) -> None:
        worker.request({"type": "job", "job_id": job_id}, timeout_s=10)

    threads = [threading.Thread(target=run, args=(f"job-{i}",)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stop.set()

    assert worker.jobs_started == 10
    assert worker.jobs_succeeded == 10
    assert worker.jobs_failed == 0
    # Every pending slot is cleaned up.
    assert worker.pending == {}


def test_duplicate_job_id_in_flight_is_refused() -> None:
    worker = make_worker()

    def hold() -> None:
        try:
            worker.request({"type": "job", "job_id": "dup"}, timeout_s=1)
        except TimeoutError:
            pass

    thread = threading.Thread(target=hold)
    thread.start()
    deadline = time.monotonic() + 2
    while "dup" not in worker.pending and time.monotonic() < deadline:
        time.sleep(0.01)

    with pytest.raises(ValueError, match="already in flight"):
        worker.request({"type": "job", "job_id": "dup"}, timeout_s=1)
    thread.join(timeout=3)


def test_control_messages_do_not_consume_a_pending_slot() -> None:
    """Cancellation must not steal the reply the turn itself is waiting for."""
    worker = make_worker()
    worker.send_control({"type": "cancel", "job_id": "j1"})
    assert worker.pending == {}
    assert worker.jobs_started == 0
    assert '"cancel"' in worker.connection.sent[0]


# -- cancel routing ----------------------------------------------------------


def test_cancel_reaches_the_worker_without_touching_the_pending_reply() -> None:
    """The turn's own reply must still land on whoever is waiting for it."""
    import json

    from fancy_gpt.bridge.server import BridgeHub, BridgeServer

    worker = make_worker()
    hub = BridgeHub(token="t")
    hub.register(worker)

    waiting: list[dict] = []

    def hold() -> None:
        waiting.append(worker.request({"type": "job", "job_id": "j1"}, timeout_s=5))

    thread = threading.Thread(target=hold)
    thread.start()
    deadline = time.monotonic() + 2
    while "j1" not in worker.pending and time.monotonic() < deadline:
        time.sleep(0.01)

    # A cancel arrives out of band while the job is still waiting.
    worker.send_control({"type": "cancel", "job_id": "j1", "generation_epoch": 0, "reason": "client gone"})
    sent = [json.loads(raw) for raw in worker.connection.sent]
    assert any(item.get("type") == "cancel" and item.get("job_id") == "j1" for item in sent)
    # The pending slot is untouched, so the real reply still has a destination.
    assert "j1" in worker.pending

    worker.dispatch({"type": "job_result", "job_id": "j1", "text": "partial"})
    thread.join(timeout=5)
    assert waiting and waiting[0]["text"] == "partial"
