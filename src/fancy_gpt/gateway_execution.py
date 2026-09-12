"""Runtime ownership of one gateway turn.

The durable side of a turn lives in :mod:`gateway_state`. This module owns the
*live* side: the browser tab it occupies, the capacity permit that tab consumes,
its cancellation signal, and the single terminal outcome.

The rule that motivates the whole module: **the HTTP request does not own the
browser.** A caller that disconnects only *requests* cancellation. The tab keeps
existing until the browser confirms it is gone, so the capacity permit must
outlive the request that started it. Releasing the permit when the awaiting task
goes away is what lets an abandoned turn keep generating in a tab while the
gateway believes the slot is free.

Identity is fenced. Every message about a turn carries
``(job_id, turn_id, generation_epoch)``, so a late message from a previous
generation on a recycled tab is discarded rather than attributed to the turn
currently using it.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class Terminal(str, Enum):
    """How a turn ended. Exactly one is ever recorded."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    #: The browser may or may not have produced an answer we can still recover.
    UNCERTAIN = "uncertain-submit"


@dataclass(frozen=True)
class TurnIdentity:
    """Fencing token for every message about one generation.

    ``generation_epoch`` increments when a tab is reused, so messages from the
    previous occupant of that tab can be told apart from the current one.
    """

    job_id: str
    turn_id: str
    generation_epoch: int = 0

    @classmethod
    def new(cls, turn_id: str, *, epoch: int = 0) -> "TurnIdentity":
        return cls(job_id=f"job-{uuid.uuid4().hex}", turn_id=turn_id, generation_epoch=epoch)

    def matches(self, job_id: str | None, epoch: int | None = None) -> bool:
        if job_id != self.job_id:
            return False
        return epoch is None or epoch == self.generation_epoch


class LeaseState(str, Enum):
    HELD = "held"
    #: Cancellation asked for, browser not yet confirmed quiet.
    RELEASING = "releasing"
    RELEASED = "released"
    #: The browser never confirmed; the permit was reclaimed on a deadline.
    RECLAIMED = "reclaimed"


@dataclass
class TabLease:
    """A permit to occupy one automation tab, owned by one turn.

    The permit is surrendered only once the browser side is known to be done.
    A ``reclaim_deadline`` bounds that wait: a browser that dies mid-turn must
    not starve every later turn of capacity forever.
    """

    identity: TurnIdentity
    acquired_at: float = field(default_factory=time.monotonic)
    reclaim_after_s: float = 60.0
    state: LeaseState = LeaseState.HELD
    tab_confirmed_closed: bool = False
    _guard: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _on_release: Callable[["TabLease"], None] | None = field(default=None, repr=False)

    def begin_release(self) -> None:
        """Cancellation requested; the browser has not confirmed yet."""
        with self._guard:
            if self.state is LeaseState.HELD:
                self.state = LeaseState.RELEASING
                self.releasing_since = time.monotonic()

    def confirm_closed(self) -> bool:
        """The browser confirmed the tab is gone. Returns True if we released now."""
        return self._finish(LeaseState.RELEASED, confirmed=True)

    def reclaim(self) -> bool:
        """Take the permit back without confirmation, after the deadline."""
        return self._finish(LeaseState.RECLAIMED, confirmed=False)

    def _finish(self, state: LeaseState, *, confirmed: bool) -> bool:
        with self._guard:
            if self.state in {LeaseState.RELEASED, LeaseState.RECLAIMED}:
                return False  # idempotent: release exactly once
            self.state = state
            self.tab_confirmed_closed = confirmed
            callback = self._on_release
        if callback is not None:
            callback(self)
        return True

    @property
    def released(self) -> bool:
        return self.state in {LeaseState.RELEASED, LeaseState.RECLAIMED}

    def reclaim_due(self, now: float | None = None) -> bool:
        """True when a releasing lease has waited past its deadline."""
        if self.state is not LeaseState.RELEASING:
            return False
        started = getattr(self, "releasing_since", self.acquired_at)
        return (now or time.monotonic()) - started >= self.reclaim_after_s


class TurnExecution:
    """The single owner of one in-flight turn.

    Cancellation, terminal state and resource release all funnel through here,
    so they cannot disagree with each other.
    """

    def __init__(self, identity: TurnIdentity, lease: TabLease) -> None:
        self.identity = identity
        self.lease = lease
        self._guard = threading.Lock()
        self._terminal: Terminal | None = None
        self._terminal_detail: str = ""
        self._cancel_requested = False
        self._cancel_reason = ""
        self._cleaned = False

    # -- cancellation --------------------------------------------------------

    def request_cancel(self, reason: str) -> bool:
        """Ask for cancellation. Returns False if the turn already ended.

        This only *requests*: the browser must still be told to stop, and the
        lease is not surrendered until it confirms.
        """
        with self._guard:
            if self._terminal is not None:
                return False
            if self._cancel_requested:
                return False  # a second cancel is a no-op, not an error
            self._cancel_requested = True
            self._cancel_reason = reason
        self.lease.begin_release()
        return True

    @property
    def cancel_requested(self) -> bool:
        with self._guard:
            return self._cancel_requested

    @property
    def cancel_reason(self) -> str:
        with self._guard:
            return self._cancel_reason

    # -- terminal outcome ----------------------------------------------------

    def set_terminal(self, state: Terminal, detail: str = "") -> bool:
        """Record the outcome. Only the first call wins.

        Completion racing cancellation is the common case: if the answer landed
        before Stop took effect, the turn is COMPLETED and cancellation must not
        overwrite that.
        """
        with self._guard:
            if self._terminal is not None:
                return False
            self._terminal = state
            self._terminal_detail = detail
            return True

    @property
    def terminal(self) -> Terminal | None:
        with self._guard:
            return self._terminal

    @property
    def terminal_detail(self) -> str:
        with self._guard:
            return self._terminal_detail

    @property
    def finished(self) -> bool:
        return self.terminal is not None

    # -- cleanup -------------------------------------------------------------

    def mark_cleaned(self) -> bool:
        """Idempotent: returns True only for the caller that cleans up."""
        with self._guard:
            if self._cleaned:
                return False
            self._cleaned = True
            return True

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"TurnExecution(job={self.identity.job_id}, epoch={self.identity.generation_epoch}, "
            f"terminal={self.terminal}, lease={self.lease.state.value})"
        )


class ExecutionRegistry:
    """Live turns, addressable by job id so out-of-band messages can find them."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._by_job: dict[str, TurnExecution] = {}

    def register(self, execution: TurnExecution) -> None:
        with self._guard:
            self._by_job[execution.identity.job_id] = execution

    def get(self, job_id: str, epoch: int | None = None) -> TurnExecution | None:
        """Look up a live turn, rejecting a stale epoch from a recycled tab."""
        with self._guard:
            execution = self._by_job.get(job_id)
        if execution is None:
            return None
        if epoch is not None and execution.identity.generation_epoch != epoch:
            return None
        return execution

    def discard(self, job_id: str) -> None:
        with self._guard:
            self._by_job.pop(job_id, None)

    def active(self) -> list[TurnExecution]:
        with self._guard:
            return list(self._by_job.values())

    def reclaimable(self, now: float | None = None) -> list[TurnExecution]:
        """Turns whose lease has waited past its reclaim deadline."""
        return [item for item in self.active() if item.lease.reclaim_due(now)]
