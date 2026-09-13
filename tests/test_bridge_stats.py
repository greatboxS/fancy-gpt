from __future__ import annotations

from fancy_gpt.bridge.server import BridgeHub, BrowserWorker


class FakeConnection:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, payload: str) -> None:
        self.sent.append(payload)


def make_worker() -> BrowserWorker:
    return BrowserWorker(connection=FakeConnection(), tunnel_ids={"chrome-remote"}, browser="chrome")


def test_worker_counts_successful_job():
    worker = make_worker()

    def respond():
        worker.dispatch({"type": "job_result", "job_id": "j1", "text": "ok"})

    import threading
    timer = threading.Timer(0.01, respond)
    timer.start()
    response = worker.request({"job_id": "j1"}, timeout_s=1.0)
    timer.join()

    assert response["type"] == "job_result"
    assert worker.jobs_started == 1
    assert worker.jobs_succeeded == 1
    assert worker.jobs_failed == 0


def test_worker_counts_job_error_response():
    worker = make_worker()

    def respond():
        worker.dispatch({"type": "job_error", "job_id": "j2", "error": "boom"})

    import threading
    timer = threading.Timer(0.01, respond)
    timer.start()
    response = worker.request({"job_id": "j2"}, timeout_s=1.0)
    timer.join()

    assert response["type"] == "job_error"
    assert worker.jobs_started == 1
    assert worker.jobs_succeeded == 0
    assert worker.jobs_failed == 1


def test_worker_counts_timeout_as_failure():
    worker = make_worker()
    try:
        worker.request({"job_id": "j3"}, timeout_s=0.05)
    except TimeoutError:
        pass
    assert worker.jobs_started == 1
    assert worker.jobs_failed == 1
    assert worker.jobs_succeeded == 0


def test_hub_snapshot_reports_connection_and_job_fields():
    hub = BridgeHub(token="t")
    worker = make_worker()
    hub.register(worker)
    worker.jobs_started = 3
    worker.jobs_succeeded = 2
    worker.jobs_failed = 1

    [entry] = hub.snapshot()
    assert entry["browser"] == "chrome"
    assert entry["alive"] is True
    assert entry["jobs_started"] == 3
    assert entry["jobs_succeeded"] == 2
    assert entry["jobs_failed"] == 1
    assert entry["active_jobs"] == 0
    assert entry["queued_jobs"] == 0
    assert entry["max_turns"] == 1
    assert "connected_at" in entry
    assert entry["connected_seconds"] >= 0


def test_hub_snapshot_exposes_parallel_surface_capabilities():
    hub = BridgeHub(token="t")
    worker = BrowserWorker(
        connection=FakeConnection(), tunnel_ids={"chrome-remote"}, browser="chrome",
        max_turns=4, max_render_slots=4, sites=("chatgpt", "gemini"),
        surface_mode="dedicated-window",
    )
    hub.register(worker)

    [entry] = hub.snapshot()
    assert entry["max_turns"] == 4
    assert entry["max_render_slots"] == 4
    assert entry["sites"] == ["chatgpt", "gemini"]
    assert entry["surface_mode"] == "dedicated-window"


def test_hub_stats_aggregates_across_worker_lifecycle():
    hub = BridgeHub(token="t")
    worker = make_worker()
    hub.register(worker)
    hub.record_job_start()
    hub.record_job_result(True)
    hub.record_probe()
    hub.unregister(worker)

    stats = hub.stats()
    assert stats["total_connections_seen"] == 1
    assert stats["total_jobs_started"] == 1
    assert stats["total_jobs_succeeded"] == 1
    assert stats["total_probe_requests"] == 1
    assert stats["connected_workers"] == 0
    assert stats["uptime_seconds"] >= 0


def test_hub_progress_round_trip():
    hub = BridgeHub(token="t")
    assert hub.get_progress("chrome-remote", "job-1") is None

    hub.record_progress("job-1", "partial answer so far", "chrome-remote")
    progress = hub.get_progress("chrome-remote", "job-1")
    assert progress is not None
    assert progress["text"] == "partial answer so far"
    assert progress["job_id"] == "job-1"

    hub.record_progress("job-1", "partial answer so far, more", "chrome-remote")
    assert hub.get_progress("chrome-remote", "job-1")["text"] == "partial answer so far, more"

    hub.clear_progress("chrome-remote", "job-1")
    assert hub.get_progress("chrome-remote", "job-1") is None


def test_hub_progress_evicts_oldest_beyond_cap():
    hub = BridgeHub(token="t")
    hub._progress_cap = 3
    for i in range(5):
        hub.record_progress(f"job-{i}", f"text-{i}", "chrome-remote")
    assert len(hub._progress) == 3
    # the earliest jobs should have been evicted, most recent kept
    assert hub.get_progress("chrome-remote", "job-4") is not None
    assert hub.get_progress("chrome-remote", "job-0") is None
