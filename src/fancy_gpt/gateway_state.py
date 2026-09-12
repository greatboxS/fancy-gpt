"""Durable state for the model gateway.

Three concerns live here, deliberately separated from the HTTP/protocol layer:

* ``SessionBinding`` - a logical gateway session is bound to exactly one
  provider conversation. Rebinding is an explicit, recorded event.
* ``TurnRecord`` - the lifecycle of a single turn, including whether it was
  actually submitted to the browser. An ``uncertain-submit`` turn must never be
  blind re-submitted.
* ``IdempotencyRecord`` - replay of a completed turn for the same key/payload.

Everything is file-backed so a gateway restart recovers the same view.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

_ID_ALPHABET = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_name(value: str, *, label: str) -> str:
    if not value or len(value) > 128 or any(char not in _ID_ALPHABET for char in value):
        raise ValueError(f"invalid {label}")
    return value


class TurnState(str, Enum):
    """Lifecycle of one gateway turn.

    The split between ``SUBMITTING`` and ``UNCERTAIN`` is the important one: a
    turn that failed while the browser may already have accepted the prompt is
    not safe to retry automatically.
    """

    QUEUED = "queued"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    OBSERVING = "observing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain-submit"


TERMINAL_STATES = {TurnState.COMPLETED, TurnState.FAILED, TurnState.CANCELLED}

#: States in which the prompt may already be sitting in the provider chat.
SUBMIT_MAY_HAVE_LANDED = {TurnState.SUBMITTING, TurnState.SUBMITTED, TurnState.OBSERVING, TurnState.UNCERTAIN}


class RebindReason(str, Enum):
    NO_BINDING = "no-binding"
    UNTRUSTED = "binding-untrusted"
    INCOMPATIBLE = "site-or-model-changed"
    UNSAFE_CONTEXT = "context-cannot-continue"


class StateTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: TurnState
    at: str
    detail: str = ""


class SessionBinding(BaseModel):
    """Binds one logical gateway session to one provider chat."""

    model_config = ConfigDict(extra="forbid")
    session_id: str
    site: str
    model: str
    protocol: str
    conversation_id: str | None = None
    generation: int = 0
    rebind_reason: RebindReason | None = None
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class TurnRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response_id: str
    session_id: str
    protocol: str
    model: str
    site: str
    state: TurnState = TurnState.QUEUED
    payload_digest: str = ""
    idempotency_key: str | None = None
    attempt: int = 1
    retry_of: str | None = None
    predecessor_response_id: str | None = None
    conversation_id: str | None = None
    cancellation_reason: str | None = None
    error: str | None = None
    transitions: list[StateTransition] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)

    @property
    def resumable(self) -> bool:
        """True when the prompt may already be in the chat, so re-submit is unsafe."""
        return self.state in SUBMIT_MAY_HAVE_LANDED


class IdempotencyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    payload_digest: str
    response_id: str
    state: TurnState = TurnState.QUEUED
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class IdempotencyConflict(ValueError):
    """Same idempotency key replayed with a different payload."""


class UncertainSubmitError(RuntimeError):
    """A prior attempt may already have reached the provider chat."""

    def __init__(self, response_id: str, state: TurnState) -> None:
        super().__init__(
            f"turn {response_id} is in state {state.value} and may already have been submitted; "
            "resume or cancel it explicitly instead of retrying"
        )
        self.response_id = response_id
        self.state = state


class ConversationLocks:
    """Per-provider-conversation mutexes.

    Turns on the same provider chat serialize absolutely; turns on different
    chats proceed in parallel. Locks are reference counted so the registry does
    not grow without bound, and release is exception/cancellation safe.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, tuple[threading.Lock, int]] = {}

    @contextmanager
    def acquire(self, key: str) -> Iterator[None]:
        with self._guard:
            lock, count = self._locks.get(key, (threading.Lock(), 0))
            self._locks[key] = (lock, count + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._guard:
                held_lock, held_count = self._locks[key]
                if held_count <= 1:
                    del self._locks[key]
                else:
                    self._locks[key] = (held_lock, held_count - 1)

    def active_keys(self) -> list[str]:
        with self._guard:
            return sorted(self._locks)


class GatewayStateStore:
    """File-backed session bindings, turn records, and idempotency claims."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve() / "gateway"
        self.bindings_dir = self.root / "bindings"
        self.turns_dir = self.root / "state-turns"
        self.idempotency_dir = self.root / "idempotency"
        for path in (self.bindings_dir, self.turns_dir, self.idempotency_dir):
            path.mkdir(parents=True, exist_ok=True)
        self._write_guard = threading.Lock()

    # -- persistence helpers -------------------------------------------------

    def _write(self, path: Path, model: BaseModel) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with self._write_guard:
            tmp.write_text(model.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp, path)

    def _binding_path(self, session_id: str) -> Path:
        return self.bindings_dir / f"{_safe_name(session_id, label='session id')}.json"

    def _turn_path(self, response_id: str) -> Path:
        return self.turns_dir / f"{_safe_name(response_id, label='response id')}.json"

    def _idempotency_path(self, key: str) -> Path:
        # The key is client supplied, so it is hashed rather than used as a filename.
        return self.idempotency_dir / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"

    # -- session bindings ----------------------------------------------------

    def load_binding(self, session_id: str) -> SessionBinding | None:
        path = self._binding_path(session_id)
        if not path.exists():
            return None
        return SessionBinding.model_validate_json(path.read_text(encoding="utf-8"))

    def save_binding(self, binding: SessionBinding) -> SessionBinding:
        binding.updated_at = _now()
        self._write(self._binding_path(binding.session_id), binding)
        return binding

    def bind(self, *, session_id: str, site: str, model: str, protocol: str) -> SessionBinding:
        """Return the session's binding, creating or rebinding it when required.

        A binding is only reconstructed when it is missing or the site changed;
        a model alias change within the same site keeps the provider chat.
        """
        existing = self.load_binding(session_id)
        if existing is None:
            return self.save_binding(
                SessionBinding(
                    session_id=session_id,
                    site=site,
                    model=model,
                    protocol=protocol,
                    rebind_reason=RebindReason.NO_BINDING,
                )
            )
        if existing.site != site:
            existing.site = site
            existing.model = model
            existing.protocol = protocol
            existing.conversation_id = None
            existing.generation += 1
            existing.rebind_reason = RebindReason.INCOMPATIBLE
            return self.save_binding(existing)
        if existing.model != model or existing.protocol != protocol:
            existing.model = model
            existing.protocol = protocol
            return self.save_binding(existing)
        return existing

    def rebind(self, session_id: str, reason: RebindReason) -> SessionBinding | None:
        """Drop the provider conversation so the next turn starts a fresh chat."""
        binding = self.load_binding(session_id)
        if binding is None:
            return None
        binding.conversation_id = None
        binding.generation += 1
        binding.rebind_reason = reason
        return self.save_binding(binding)

    def attach_conversation(self, session_id: str, conversation_id: str | None) -> SessionBinding | None:
        """Record the provider conversation the session actually landed on.

        The first observed conversation wins; a provider that reports a
        different chat for an already-bound session is a correlation failure and
        is reported rather than silently accepted.
        """
        binding = self.load_binding(session_id)
        if binding is None or not conversation_id:
            return binding
        if binding.conversation_id and binding.conversation_id != conversation_id:
            raise RuntimeError(
                f"session {session_id} is bound to provider conversation "
                f"{binding.conversation_id} but the provider answered on {conversation_id}"
            )
        if not binding.conversation_id:
            binding.conversation_id = conversation_id
            return self.save_binding(binding)
        return binding

    # -- turn records --------------------------------------------------------

    def load_turn(self, response_id: str) -> TurnRecord | None:
        path = self._turn_path(response_id)
        if not path.exists():
            return None
        return TurnRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def save_turn(self, turn: TurnRecord) -> TurnRecord:
        turn.updated_at = _now()
        self._write(self._turn_path(turn.response_id), turn)
        return turn

    def transition(self, turn: TurnRecord, state: TurnState, detail: str = "") -> TurnRecord:
        turn.state = state
        turn.transitions.append(StateTransition(state=state, at=_now(), detail=detail))
        if state is TurnState.CANCELLED and detail:
            turn.cancellation_reason = detail
        if state is TurnState.FAILED and detail:
            turn.error = detail
        return self.save_turn(turn)

    def session_turns(self, session_id: str) -> list[TurnRecord]:
        turns: list[TurnRecord] = []
        for path in self.turns_dir.glob("*.json"):
            try:
                turn = TurnRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if turn.session_id == session_id:
                turns.append(turn)
        return sorted(turns, key=lambda item: item.created_at)

    def unresolved_turns(self, session_id: str) -> list[TurnRecord]:
        """Turns that may already have reached the provider chat."""
        return [turn for turn in self.session_turns(session_id) if turn.resumable]

    # -- idempotency ---------------------------------------------------------

    def claim_idempotency(self, key: str, payload_digest: str, response_id: str) -> tuple[str, IdempotencyRecord]:
        """Claim an idempotency key.

        Returns ``("claimed", record)`` for a fresh key, or ``("replay", record)``
        when the same key/payload was already seen. Raises
        :class:`IdempotencyConflict` when the payload differs.
        """
        if not key or len(key) > 255:
            raise ValueError("idempotency key must be 1-255 characters")
        path = self._idempotency_path(key)
        record = IdempotencyRecord(key=key, payload_digest=payload_digest, response_id=response_id)
        # The claim is published by hard-linking a fully written temporary file
        # into place. os.link fails if the target exists, so exactly one
        # concurrent caller wins, and a loser never observes a half-written
        # claim the way it could with a create-then-write sequence.
        staging = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.claim")
        staging.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        try:
            os.link(staging, path)
            return "claimed", record
        except FileExistsError:
            existing = IdempotencyRecord.model_validate_json(path.read_text(encoding="utf-8"))
            if existing.payload_digest != payload_digest:
                raise IdempotencyConflict(
                    "idempotency key was already used with a different request payload"
                ) from None
            return "replay", existing
        finally:
            staging.unlink(missing_ok=True)

    def update_idempotency(self, key: str, state: TurnState) -> None:
        path = self._idempotency_path(key)
        if not path.exists():
            return
        record = IdempotencyRecord.model_validate_json(path.read_text(encoding="utf-8"))
        record.state = state
        record.updated_at = _now()
        self._write(path, record)

    def release_idempotency(self, key: str) -> None:
        """Drop a claim so a failed turn can be retried with the same key."""
        path = self._idempotency_path(key)
        if path.exists():
            path.unlink()
