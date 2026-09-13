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
    return BrowserWorker(connection=FakeConnection(), tunnel_ids={"edge-remote"}, browser="edge", max_turns=16)


def test_worker_capacity_queues_without_rejecting_or_overcommitting() -> None:
    worker = BrowserWorker(
        connection=FakeConnection(), tunnel_ids={"edge-remote"}, browser="edge", max_turns=2,
    )
    peak = 0
    stop = threading.Event()

    def answer() -> None:
        nonlocal peak
        seen: set[str] = set()
        while not stop.wait(0.005):
            pending = list(worker.pending)
            peak = max(peak, len(pending))
            for job_id in pending:
                if job_id not in seen:
                    seen.add(job_id)
                    threading.Timer(
                        0.08, worker.dispatch,
                        args=({"type": "job_result", "job_id": job_id, "text": "ok"},),
                    ).start()

    threading.Thread(target=answer, daemon=True).start()
    results: list[str] = []
    threads = [threading.Thread(
        target=lambda job_id=f"bounded-{i}": results.append(
            worker.request({"type": "job", "job_id": job_id}, timeout_s=2)["text"]
        )
    ) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    stop.set()

    assert results == ["ok"] * 5
    assert peak == 2


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


def test_job_cancelled_is_a_terminal_reply_not_a_dropped_message() -> None:
    """The live bug: a stopped turn produced no reply, so the caller hung.

    Cancellation reached the browser and generation really stopped, but the
    bridge only forwarded job_result and job_error, so job_cancelled was
    dropped and the controller waited out its entire timeout for a turn that
    had already ended.
    """
    from fancy_gpt.bridge.server import BridgeServer

    import inspect

    source = inspect.getsource(BridgeServer)
    assert '"job_cancelled"' in source, "job_cancelled must be forwarded to the waiting caller"

    worker = make_worker()
    answered: list[dict] = []

    def wait() -> None:
        answered.append(worker.request({"type": "job", "job_id": "j-cancel"}, timeout_s=5))

    thread = threading.Thread(target=wait)
    thread.start()
    deadline = time.monotonic() + 2
    while "j-cancel" not in worker.pending and time.monotonic() < deadline:
        time.sleep(0.01)

    worker.dispatch({"type": "job_cancelled", "job_id": "j-cancel", "text": "half an answer"})
    thread.join(timeout=5)

    assert answered, "a cancelled turn must still resolve the waiting request"
    assert answered[0]["type"] == "job_cancelled"
    assert answered[0]["text"] == "half an answer"


# -- findings from the independent Codex review ------------------------------


def test_stats_works_while_a_worker_is_alive() -> None:
    """The endpoint used to raise exactly when it mattered."""
    from fancy_gpt.bridge.server import BridgeHub

    hub = BridgeHub(token="t")
    worker = make_worker()
    hub.register(worker)
    assert worker.alive is True

    stats = hub.stats()
    assert stats["connected_workers"] >= 1
    assert "total_jobs_started" in stats


def test_progress_never_crosses_tunnels() -> None:
    """Relying on the client to discard other jobs is not confidentiality."""
    from fancy_gpt.bridge.server import BridgeHub

    hub = BridgeHub(token="t")
    edge, chrome = FakeConnection(), FakeConnection()
    hub.add_subscriber(edge, "edge-remote")
    hub.add_subscriber(chrome, "chrome-remote")

    hub.record_progress("job-1", "partial answer", "edge-remote")

    assert len(edge.sent) == 1, "the owning tunnel's subscriber receives it"
    assert chrome.sent == [], "another tunnel's subscriber must never see it"
    # It is still stored for whoever polls for it.
    assert hub.get_progress("edge-remote", "job-1")["text"] == "partial answer"
    assert hub.get_progress("chrome-remote", "job-1") is None


def test_empty_tunnel_cannot_subscribe_to_every_progress_feed() -> None:
    from fancy_gpt.bridge.server import BridgeHub

    hub = BridgeHub(token="t")
    with pytest.raises(ValueError, match="exact tunnel id"):
        hub.add_subscriber(FakeConnection(), "")


def test_same_job_id_is_isolated_in_polled_progress() -> None:
    from fancy_gpt.bridge.server import BridgeHub

    hub = BridgeHub(token="t")
    hub.record_progress("same-job", "edge secret", "edge-remote")
    hub.record_progress("same-job", "chrome secret", "chrome-remote")
    assert hub.get_progress("edge-remote", "same-job")["text"] == "edge secret"
    assert hub.get_progress("chrome-remote", "same-job")["text"] == "chrome secret"


def test_a_cancel_is_answered_by_the_browser_not_acknowledged_blind() -> None:
    """A cancel can legitimately do nothing; saying "accepted" then is a lie."""
    worker = make_worker()
    answers: list[dict] = []

    def responder() -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if worker.control_pending:
                control_id = next(iter(worker.control_pending))
                worker.dispatch_control(
                    {"type": "cancel_result", "job_id": "job-x", "control_id": control_id, "accepted": False,
                     "reason": "no such job is running"}
                )
                return
            time.sleep(0.01)

    threading.Thread(target=responder, daemon=True).start()
    answer = worker.request_control({"type": "cancel", "job_id": "job-x"}, timeout_s=3)
    assert answer is not None
    assert answer["accepted"] is False
    assert "no such job" in answer["reason"]


def test_a_control_round_trip_never_consumes_a_job_reply() -> None:
    worker = make_worker()
    waiting: list[dict] = []

    def hold() -> None:
        waiting.append(worker.request({"type": "job", "job_id": "job-y"}, timeout_s=5))

    thread = threading.Thread(target=hold)
    thread.start()
    deadline = time.monotonic() + 2
    while "job-y" not in worker.pending and time.monotonic() < deadline:
        time.sleep(0.01)

    # A control message for the same id must not steal the job's reply slot.
    threading.Thread(
        target=lambda: worker.request_control({"type": "cancel", "job_id": "job-y"}, timeout_s=0.3),
        daemon=True,
    ).start()
    time.sleep(0.5)
    assert "job-y" in worker.pending, "the job is still waiting for its own reply"

    worker.dispatch({"type": "job_result", "job_id": "job-y", "text": "answered"})
    thread.join(timeout=5)
    assert waiting and waiting[0]["text"] == "answered"


def test_an_unanswered_cancel_is_reported_as_not_accepted() -> None:
    worker = make_worker()
    assert worker.request_control({"type": "cancel", "job_id": "job-z"}, timeout_s=0.2) is None
