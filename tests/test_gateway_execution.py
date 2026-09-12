"""Ownership, fencing and single-terminal guarantees for a live turn."""

from __future__ import annotations

import threading

import pytest

from fancy_gpt.gateway_execution import (
    ExecutionRegistry,
    LeaseState,
    TabLease,
    Terminal,
    TurnExecution,
    TurnIdentity,
)


def make(epoch: int = 0, reclaim_after_s: float = 60.0) -> TurnExecution:
    identity = TurnIdentity.new("turn-1", epoch=epoch)
    return TurnExecution(identity, TabLease(identity=identity, reclaim_after_s=reclaim_after_s))


# -- exactly one terminal ----------------------------------------------------


def test_only_the_first_terminal_wins() -> None:
    execution = make()
    assert execution.set_terminal(Terminal.COMPLETED, "answered") is True
    assert execution.set_terminal(Terminal.CANCELLED, "too late") is False
    assert execution.terminal is Terminal.COMPLETED
    assert execution.terminal_detail == "answered"


def test_completion_racing_cancellation_keeps_completed() -> None:
    """If the answer landed before Stop took effect, the turn completed."""
    execution = make()
    execution.request_cancel("client disconnected")
    assert execution.set_terminal(Terminal.COMPLETED, "answer arrived first") is True
    # Cancellation must not rewrite an outcome that already happened.
    assert execution.set_terminal(Terminal.CANCELLED) is False
    assert execution.terminal is Terminal.COMPLETED


def test_cancel_after_terminal_is_refused() -> None:
    execution = make()
    execution.set_terminal(Terminal.COMPLETED)
    assert execution.request_cancel("too late") is False
    assert execution.cancel_requested is False


def test_second_cancel_is_a_noop_not_an_error() -> None:
    execution = make()
    assert execution.request_cancel("first") is True
    assert execution.request_cancel("second") is False
    assert execution.cancel_reason == "first"


def test_concurrent_terminals_yield_exactly_one_winner() -> None:
    execution = make()
    winners: list[bool] = []
    barrier = threading.Barrier(8)

    def attempt(state: Terminal) -> None:
        barrier.wait()
        winners.append(execution.set_terminal(state))

    threads = [threading.Thread(target=attempt, args=(Terminal.COMPLETED if i % 2 else Terminal.FAILED,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert winners.count(True) == 1


# -- the lease outlives the request ------------------------------------------


def test_cancel_does_not_release_the_permit_immediately() -> None:
    """A disconnecting caller must not free a tab that is still generating."""
    execution = make()
    execution.request_cancel("client disconnected")
    assert execution.lease.state is LeaseState.RELEASING
    assert execution.lease.released is False


def test_permit_is_released_once_the_browser_confirms() -> None:
    execution = make()
    execution.request_cancel("client disconnected")
    assert execution.lease.confirm_closed() is True
    assert execution.lease.state is LeaseState.RELEASED
    assert execution.lease.tab_confirmed_closed is True
    # Idempotent: a duplicate confirmation does not double-release.
    assert execution.lease.confirm_closed() is False


def test_release_callback_fires_exactly_once_under_races() -> None:
    identity = TurnIdentity.new("turn-1")
    calls: list[str] = []
    lease = TabLease(identity=identity)
    lease._on_release = lambda _lease: calls.append("released")
    barrier = threading.Barrier(6)

    def attempt() -> None:
        barrier.wait()
        lease.confirm_closed()

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == ["released"]


def test_a_dead_browser_cannot_starve_capacity_forever() -> None:
    """Gemini's starvation case: confirmation never arrives."""
    execution = make(reclaim_after_s=0.0)
    execution.request_cancel("client disconnected")
    assert execution.lease.reclaim_due() is True
    assert execution.lease.reclaim() is True
    assert execution.lease.state is LeaseState.RECLAIMED
    # Reclaimed without confirmation, which is recorded rather than assumed.
    assert execution.lease.tab_confirmed_closed is False


def test_a_held_lease_is_never_reclaim_due() -> None:
    execution = make(reclaim_after_s=0.0)
    assert execution.lease.state is LeaseState.HELD
    assert execution.lease.reclaim_due() is False


# -- fencing against recycled tabs -------------------------------------------


def test_stale_epoch_is_not_attributed_to_the_current_turn() -> None:
    registry = ExecutionRegistry()
    current = make(epoch=3)
    registry.register(current)

    job = current.identity.job_id
    assert registry.get(job, epoch=3) is current
    # A late message from the previous occupant of this tab.
    assert registry.get(job, epoch=2) is None
    assert registry.get("job-unknown", epoch=3) is None


def test_identity_matching_rejects_foreign_jobs() -> None:
    identity = TurnIdentity.new("turn-1", epoch=5)
    assert identity.matches(identity.job_id, 5) is True
    assert identity.matches(identity.job_id, 4) is False
    assert identity.matches("job-other", 5) is False
    # Epoch omitted means "any epoch of this job".
    assert identity.matches(identity.job_id) is True


def test_registry_reports_leases_past_their_deadline() -> None:
    registry = ExecutionRegistry()
    stale = make(reclaim_after_s=0.0)
    fresh = make(reclaim_after_s=999.0)
    stale.request_cancel("gone")
    fresh.request_cancel("gone")
    registry.register(stale)
    registry.register(fresh)
    assert registry.reclaimable() == [stale]


def test_cleanup_runs_once() -> None:
    execution = make()
    assert execution.mark_cleaned() is True
    assert execution.mark_cleaned() is False
