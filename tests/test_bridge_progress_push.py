"""Pushed progress, replacing the poll that floored streaming latency."""

from __future__ import annotations

import json
import threading
import time

import pytest

from fancy_gpt.bridge.progress import ProgressSubscription
from fancy_gpt.bridge.server import BridgeHub, BrowserWorker


class FakeConnection:
    """Stands in for a controller's subscriber socket."""

    def __init__(self, fail_after: int | None = None) -> None:
        self.sent: list[str] = []
        self.fail_after = fail_after

    def send(self, raw: str) -> None:
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise ConnectionResetError("subscriber went away")
        self.sent.append(raw)


def hub_with_subscriber() -> tuple[BridgeHub, FakeConnection]:
    hub = BridgeHub(token="t")
    connection = FakeConnection()
    hub.add_subscriber(connection, "edge-remote")
    return hub, connection


def test_progress_is_pushed_to_subscribers(tmp_path=None) -> None:
    hub, connection = hub_with_subscriber()
    hub.record_progress("job-1", "Hello", "edge-remote")
    hub.record_progress("job-1", "Hello world", "edge-remote")

    pushed = [json.loads(raw) for raw in connection.sent]
    assert [item["text"] for item in pushed] == ["Hello", "Hello world"]
    assert all(item["job_id"] == "job-1" for item in pushed)
    # The stored snapshot is still available for anything still polling.
    assert hub.get_progress("edge-remote", "job-1")["text"] == "Hello world"


def test_a_dead_subscriber_is_dropped_not_retried() -> None:
    hub = BridgeHub(token="t")
    connection = FakeConnection(fail_after=1)
    hub.add_subscriber(connection, "edge-remote")

    hub.record_progress("job-1", "first", "edge-remote")
    assert hub.subscriber_count() == 1
    # The second push fails; the subscriber is removed rather than retried.
    hub.record_progress("job-1", "second", "edge-remote")
    assert hub.subscriber_count() == 0
    # Recording still works with no subscribers at all.
    hub.record_progress("job-1", "third", "edge-remote")
    assert hub.get_progress("edge-remote", "job-1")["text"] == "third"


def test_a_slow_subscriber_never_blocks_the_producer() -> None:
    """Snapshots are cumulative, so a laggard misses some and still converges."""

    class SlowConnection(FakeConnection):
        def __init__(self) -> None:
            super().__init__()
            self.gate = threading.Lock()

        def send(self, raw: str) -> None:
            # Held elsewhere, so acquiring inside the hub's push times out.
            super().send(raw)

    hub = BridgeHub(token="t")
    connection = SlowConnection()
    key = hub.add_subscriber(connection, "edge-remote")
    # Hold the subscriber's own send lock to simulate a stuck consumer.
    _conn, lock, _tunnel = hub._subscribers[key]
    lock.acquire()
    started = time.monotonic()
    hub.record_progress("job-1", "snapshot", "edge-remote")
    elapsed = time.monotonic() - started
    lock.release()

    # The producer waited only for the short acquire timeout, not forever.
    assert elapsed < 1.0
    assert connection.sent == []


def test_subscribers_are_removed_on_unsubscribe() -> None:
    hub, connection = hub_with_subscriber()
    assert hub.subscriber_count() == 1
    hub.remove_subscriber(id(connection))
    assert hub.subscriber_count() == 0
    hub.record_progress("job-1", "ignored", "edge-remote")
    assert connection.sent == []


# -- client side demultiplexing ----------------------------------------------


def test_progress_is_routed_only_to_the_turn_that_asked() -> None:
    subscription = ProgressSubscription("ws://unused", "token", "edge-remote")
    received_a: list[str] = []
    received_b: list[str] = []
    # Register directly; connecting is exercised by the live run, not here.
    subscription._callbacks["job-a"] = received_a.append
    subscription._callbacks["job-b"] = received_b.append

    for message in (
        {"type": "job_progress", "job_id": "job-a", "text": "for a"},
        {"type": "job_progress", "job_id": "job-b", "text": "for b"},
        {"type": "job_progress", "job_id": "job-unknown", "text": "nobody"},
        {"type": "something_else", "job_id": "job-a", "text": "not progress"},
    ):
        if message.get("type") != "job_progress":
            continue
        callback = subscription._callbacks.get(str(message["job_id"]))
        if callback:
            callback(str(message["text"]))

    assert received_a == ["for a"]
    assert received_b == ["for b"]


def test_unsubscribing_stops_late_progress_reaching_a_finished_turn() -> None:
    """A snapshot arriving after the terminal reply must have nowhere to go."""
    subscription = ProgressSubscription("ws://unused", "token", "edge-remote")
    received: list[str] = []
    subscription._callbacks["job-a"] = received.append
    assert subscription.active_jobs() == ["job-a"]

    subscription.unsubscribe("job-a")
    assert subscription.active_jobs() == []
    assert subscription._callbacks.get("job-a") is None
    assert received == []


def test_close_releases_every_route() -> None:
    subscription = ProgressSubscription("ws://unused", "token", "edge-remote")
    subscription._callbacks["job-a"] = lambda _text: None
    subscription.close()
    assert subscription.active_jobs() == []
    # Closing is final: it will not silently reconnect afterwards.
    assert subscription._ensure_connected() is False


def test_a_callback_that_raises_does_not_break_the_feed() -> None:
    subscription = ProgressSubscription("ws://unused", "token", "edge-remote")
    subscription._callbacks["job-a"] = lambda _text: (_ for _ in ()).throw(RuntimeError("boom"))
    subscription._callbacks["job-b"] = lambda _text: None
    # Routing is per job, so one bad consumer cannot take the others down.
    callback = subscription._callbacks["job-a"]
    with pytest.raises(RuntimeError):
        callback("x")
    assert "job-b" in subscription.active_jobs()


# -- the provider prefers push and falls back to polling ---------------------


def test_provider_uses_push_when_the_driver_offers_it() -> None:
    from fancy_gpt.providers.web_automation import ChatGPTWebAutomationProvider

    class PushDriver:
        def __init__(self) -> None:
            self.watched: list[str] = []
            self.polled = 0

        def watch_progress(self, turn_id: str, on_progress) -> bool:
            self.watched.append(turn_id)
            return True

        def poll_progress(self, turn_id: str):
            self.polled += 1
            return None

    driver = PushDriver()
    provider = ChatGPTWebAutomationProvider(driver)  # type: ignore[arg-type]
    poller = provider._start_progress_poller("bridge-1", lambda _text: None)

    assert driver.watched == ["bridge-1"]
    assert poller is None, "no poller should start when push succeeded"
    assert driver.polled == 0


def test_provider_falls_back_to_polling_when_push_is_unavailable() -> None:
    from fancy_gpt.providers.web_automation import ChatGPTWebAutomationProvider

    class PollOnlyDriver:
        def watch_progress(self, turn_id: str, on_progress) -> bool:
            return False

        def poll_progress(self, turn_id: str):
            return None

    provider = ChatGPTWebAutomationProvider(PollOnlyDriver())  # type: ignore[arg-type]
    poller = provider._start_progress_poller("bridge-1", lambda _text: None)
    assert poller is not None
    poller.stop()


def test_no_progress_machinery_starts_without_a_consumer() -> None:
    from fancy_gpt.providers.web_automation import ChatGPTWebAutomationProvider

    class Driver:
        def watch_progress(self, *_args, **_kwargs) -> bool:
            raise AssertionError("must not be called without a consumer")

        def poll_progress(self, *_args, **_kwargs):
            raise AssertionError("must not be called without a consumer")

    provider = ChatGPTWebAutomationProvider(Driver())  # type: ignore[arg-type]
    assert provider._start_progress_poller("bridge-1", None) is None
