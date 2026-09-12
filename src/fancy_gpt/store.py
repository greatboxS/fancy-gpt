from __future__ import annotations

import json
import os
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TypeVar

from pydantic import BaseModel

from .errors import InvalidStateError
from .file_lock import exclusive_file_lock
from contextlib import contextmanager
from .models import ChatKind, ChatPolicy, ChatRecord, RequestMode, RequestState, RequestStatus, SessionRecord

T = TypeVar("T", bound=BaseModel)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RequestStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.requests_dir = self.root / "requests"
        self.requests_dir.mkdir(parents=True, exist_ok=True)

    def request_dir(self, request_id: str) -> Path:
        if not request_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in request_id):
            raise ValueError("invalid request id")
        path = self.requests_dir / request_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _lock_path(self, request_id: str) -> Path:
        return self.request_dir(request_id) / ".state.lock"

    @contextmanager
    def operation_lock(self, request_id: str) -> Iterator[None]:
        with exclusive_file_lock(self.request_dir(request_id) / ".operation.lock"):
            yield

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def write_model(self, request_id: str, filename: str, model: BaseModel) -> Path:
        path = self.request_dir(request_id) / filename
        with exclusive_file_lock(self._lock_path(request_id)):
            self._atomic_write(path, model.model_dump_json(indent=2))
        return path

    def read_model(self, request_id: str, filename: str, cls: type[T]) -> T:
        path = self.request_dir(request_id) / filename
        with exclusive_file_lock(self._lock_path(request_id)):
            return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def write_json(self, request_id: str, filename: str, payload: dict[str, Any]) -> Path:
        path = self.request_dir(request_id) / filename
        with exclusive_file_lock(self._lock_path(request_id)):
            self._atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False))
        return path

    def write_text(self, request_id: str, filename: str, text: str) -> Path:
        path = self.request_dir(request_id) / filename
        with exclusive_file_lock(self._lock_path(request_id)):
            self._atomic_write(path, text)
        return path

    def create_status(
        self,
        request_id: str,
        *,
        route_kind: str,
        route_name: str,
        skill: str,
        mode: RequestMode,
        objective: str,
        session_id: str | None = None,
        chat_id: str | None = None,
        chat_policy: ChatPolicy | None = None,
    ) -> RequestStatus:
        now = _now()
        status = RequestStatus(
            request_id=request_id,
            state=RequestState.NEW,
            route_kind=route_kind,
            route_name=route_name,
            skill=skill,
            mode=mode,
            objective=objective,
            created_at=now,
            updated_at=now,
            session_id=session_id,
            chat_id=chat_id,
            chat_policy=chat_policy,
        )
        path = self.request_dir(request_id) / "status.json"
        with exclusive_file_lock(self._lock_path(request_id)):
            if path.exists():
                raise InvalidStateError(f"request already exists: {request_id}")
            self._atomic_write(path, status.model_dump_json(indent=2))
        return status

    def load_status(self, request_id: str) -> RequestStatus:
        path = self.request_dir(request_id) / "status.json"
        with exclusive_file_lock(self._lock_path(request_id)):
            return RequestStatus.model_validate_json(path.read_text(encoding="utf-8"))

    def list_statuses(self, request_ids: list[str] | None = None) -> list[RequestStatus]:
        if request_ids is None:
            paths = self.requests_dir.glob("*/status.json")
        else:
            paths = (self.requests_dir / request_id / "status.json" for request_id in request_ids)
        statuses = [RequestStatus.model_validate_json(path.read_text(encoding="utf-8")) for path in paths if path.exists()]
        return sorted(statuses, key=lambda item: item.created_at, reverse=True)

    def transition(self, request_id: str, expected: RequestState, new: RequestState, **updates: Any) -> RequestStatus:
        path = self.request_dir(request_id) / "status.json"
        with exclusive_file_lock(self._lock_path(request_id)):
            current = RequestStatus.model_validate_json(path.read_text(encoding="utf-8"))
            if current.state != expected:
                raise InvalidStateError(
                    f"state transition {expected.value}->{new.value} rejected; current={current.state.value}"
                )
            payload = current.model_dump()
            payload.update(updates)
            payload["state"] = new
            payload["updated_at"] = _now()
            updated = RequestStatus.model_validate(payload)
            self._atomic_write(path, updated.model_dump_json(indent=2))
            return updated

    def update_progress(self, request_id: str, text: str) -> None:
        """Best-effort snapshot of in-flight model output for live monitoring.

        Never raises and never touches `state`/`updated_at`: this is called
        from a background poller thread while the main thread is still
        blocked waiting on the provider, so it must not interfere with the
        authoritative state transitions happening on request completion.
        """
        path = self.request_dir(request_id) / "status.json"
        try:
            with exclusive_file_lock(self._lock_path(request_id)):
                current = RequestStatus.model_validate_json(path.read_text(encoding="utf-8"))
                if current.state not in (RequestState.RUNNING_PLANNER, RequestState.RUNNING_FINAL):
                    return
                payload = current.model_dump()
                payload["partial_text"] = text
                payload["partial_text_updated_at"] = _now()
                updated = RequestStatus.model_validate(payload)
                self._atomic_write(path, updated.model_dump_json(indent=2))
        except Exception:
            pass

    def update_status(self, request_id: str, **updates: Any) -> RequestStatus:
        path = self.request_dir(request_id) / "status.json"
        with exclusive_file_lock(self._lock_path(request_id)):
            current = RequestStatus.model_validate_json(path.read_text(encoding="utf-8"))
            payload = current.model_dump()
            payload.update(updates)
            payload["updated_at"] = _now()
            updated = RequestStatus.model_validate(payload)
            self._atomic_write(path, updated.model_dump_json(indent=2))
            return updated

    def fail(self, request_id: str, error: str) -> RequestStatus:
        path = self.request_dir(request_id) / "status.json"
        with exclusive_file_lock(self._lock_path(request_id)):
            current = RequestStatus.model_validate_json(path.read_text(encoding="utf-8"))
            if current.state == RequestState.COMPLETE:
                return current
            payload = current.model_dump()
            payload["state"] = RequestState.FAILED
            payload["updated_at"] = _now()
            payload["error"] = error
            updated = RequestStatus.model_validate(payload)
            self._atomic_write(path, updated.model_dump_json(indent=2))
            return updated


def _validate_id(value: str, label: str) -> str:
    if not value or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in value):
        raise ValueError(f"invalid {label}")
    return value


class SessionStore:
    """Persistent session/chat registry independent from individual request runs."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _session_dir(self, session_id: str) -> Path:
        path = self.sessions_dir / _validate_id(session_id, "session id")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _session_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "session.json"

    def _chat_path(self, session_id: str, chat_id: str) -> Path:
        path = self._session_dir(session_id) / "chats"
        path.mkdir(parents=True, exist_ok=True)
        return path / f"{_validate_id(chat_id, 'chat id')}.json"

    def _lock_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / ".session.lock"

    @contextmanager
    def chat_operation_lock(self, session_id: str, chat_id: str) -> Iterator[None]:
        _validate_id(chat_id, "chat id")
        with exclusive_file_lock(self._session_dir(session_id) / f".{chat_id}.operation.lock"):
            yield

    def create_session(self, *, repo_root: str, title: str | None = None, session_id: str | None = None) -> SessionRecord:
        resolved_id = session_id or f"ses-{uuid.uuid4().hex[:12]}"
        now = _now()
        record = SessionRecord(
            session_id=resolved_id,
            title=title or Path(repo_root).resolve().name or "FancyGPT session",
            repo_root=str(Path(repo_root).expanduser().resolve()),
            created_at=now,
            updated_at=now,
        )
        path = self._session_path(resolved_id)
        with exclusive_file_lock(self._lock_path(resolved_id)):
            if path.exists():
                raise InvalidStateError(f"session already exists: {resolved_id}")
            RequestStore._atomic_write(path, record.model_dump_json(indent=2))
        return record

    def default_session_id(self, repo_root: str) -> str:
        normalized = str(Path(repo_root).expanduser().resolve())
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
        return f"ses-default-{digest}"

    def ensure_session(self, *, repo_root: str, session_id: str | None = None, title: str | None = None) -> SessionRecord:
        resolved_id = session_id or self.default_session_id(repo_root)
        path = self._session_path(resolved_id)
        if not path.exists():
            try:
                return self.create_session(repo_root=repo_root, title=title, session_id=resolved_id)
            except InvalidStateError:
                pass
        session = self.get_session(resolved_id)
        expected_root = str(Path(repo_root).expanduser().resolve())
        if session.repo_root != expected_root:
            raise ValueError(f"session {resolved_id} belongs to a different repo_root")
        if session.closed_at:
            raise InvalidStateError(f"session is closed: {resolved_id}")
        return session

    def get_session(self, session_id: str) -> SessionRecord:
        path = self._session_path(session_id)
        with exclusive_file_lock(self._lock_path(session_id)):
            if not path.exists():
                raise KeyError(f"unknown session: {session_id}")
            return SessionRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list_sessions(self) -> list[SessionRecord]:
        records: list[SessionRecord] = []
        for path in self.sessions_dir.glob("*/session.json"):
            records.append(SessionRecord.model_validate_json(path.read_text(encoding="utf-8")))
        return sorted(records, key=lambda item: item.updated_at, reverse=True)

    def close_session(self, session_id: str) -> SessionRecord:
        with exclusive_file_lock(self._lock_path(session_id)):
            path = self._session_path(session_id)
            current = SessionRecord.model_validate_json(path.read_text(encoding="utf-8"))
            payload = current.model_dump()
            payload.update(closed_at=_now(), updated_at=_now())
            updated = SessionRecord.model_validate(payload)
            RequestStore._atomic_write(path, updated.model_dump_json(indent=2))
            return updated

    def create_chat(
        self,
        session_id: str,
        *,
        title: str,
        kind: ChatKind = ChatKind.STANDARD,
        make_active: bool = False,
        conversation_id: str | None = None,
    ) -> ChatRecord:
        chat_id = f"chat-{uuid.uuid4().hex[:12]}"
        now = _now()
        chat = ChatRecord(
            chat_id=chat_id,
            session_id=session_id,
            title=title,
            kind=kind,
            conversation_id=conversation_id,
            created_at=now,
            updated_at=now,
        )
        with exclusive_file_lock(self._lock_path(session_id)):
            session_path = self._session_path(session_id)
            if not session_path.exists():
                raise KeyError(f"unknown session: {session_id}")
            session = SessionRecord.model_validate_json(session_path.read_text(encoding="utf-8"))
            if session.closed_at:
                raise InvalidStateError(f"session is closed: {session_id}")
            RequestStore._atomic_write(self._chat_path(session_id, chat_id), chat.model_dump_json(indent=2))
            payload = session.model_dump()
            payload["chat_ids"] = [*session.chat_ids, chat_id]
            if make_active:
                payload["active_chat_id"] = chat_id
            payload["updated_at"] = now
            RequestStore._atomic_write(session_path, SessionRecord.model_validate(payload).model_dump_json(indent=2))
        return chat

    def get_or_create_main_chat(self, session_id: str) -> ChatRecord:
        """Atomically resolve the active chat, creating the main chat once."""
        with exclusive_file_lock(self._lock_path(session_id)):
            session_path = self._session_path(session_id)
            session = SessionRecord.model_validate_json(session_path.read_text(encoding="utf-8"))
            if session.closed_at:
                raise InvalidStateError(f"session is closed: {session_id}")
            if session.active_chat_id:
                path = self._chat_path(session_id, session.active_chat_id)
                return ChatRecord.model_validate_json(path.read_text(encoding="utf-8"))
            now = _now()
            chat = ChatRecord(
                chat_id=f"chat-{uuid.uuid4().hex[:12]}",
                session_id=session_id,
                title="Main discussion",
                kind=ChatKind.MAIN,
                created_at=now,
                updated_at=now,
            )
            RequestStore._atomic_write(
                self._chat_path(session_id, chat.chat_id), chat.model_dump_json(indent=2)
            )
            payload = session.model_dump()
            payload.update(
                active_chat_id=chat.chat_id,
                chat_ids=[*session.chat_ids, chat.chat_id],
                updated_at=now,
            )
            RequestStore._atomic_write(
                session_path, SessionRecord.model_validate(payload).model_dump_json(indent=2)
            )
            return chat

    def get_chat(self, session_id: str, chat_id: str) -> ChatRecord:
        path = self._chat_path(session_id, chat_id)
        with exclusive_file_lock(self._lock_path(session_id)):
            if not path.exists():
                raise KeyError(f"unknown chat: {chat_id}")
            return ChatRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list_chats(self, session_id: str, *, include_archived: bool = False) -> list[ChatRecord]:
        session = self.get_session(session_id)
        chats = [self.get_chat(session_id, chat_id) for chat_id in session.chat_ids]
        return [chat for chat in chats if include_archived or not chat.archived_at]

    def select_chat(self, session_id: str, chat_id: str) -> SessionRecord:
        with exclusive_file_lock(self._lock_path(session_id)):
            session_path = self._session_path(session_id)
            session = SessionRecord.model_validate_json(session_path.read_text(encoding="utf-8"))
            if session.closed_at:
                raise InvalidStateError(f"session is closed: {session_id}")
            chat_path = self._chat_path(session_id, chat_id)
            if chat_id not in session.chat_ids or not chat_path.exists():
                raise KeyError(f"chat {chat_id} does not belong to session {session_id}")
            chat = ChatRecord.model_validate_json(chat_path.read_text(encoding="utf-8"))
            if chat.archived_at:
                raise InvalidStateError(f"chat is archived: {chat_id}")
            payload = session.model_dump()
            payload.update(active_chat_id=chat_id, updated_at=_now())
            updated = SessionRecord.model_validate(payload)
            RequestStore._atomic_write(session_path, updated.model_dump_json(indent=2))
            return updated

    def archive_chat(self, session_id: str, chat_id: str) -> ChatRecord:
        session = self.get_session(session_id)
        chat = self.get_chat(session_id, chat_id)
        if session.active_chat_id == chat_id:
            raise InvalidStateError("select another active chat before archiving this one")
        if chat.archived_at:
            return chat
        return self.update_chat(session_id, chat_id, archived_at=_now())

    def update_chat(self, session_id: str, chat_id: str, **updates: Any) -> ChatRecord:
        path = self._chat_path(session_id, chat_id)
        with exclusive_file_lock(self._lock_path(session_id)):
            current = ChatRecord.model_validate_json(path.read_text(encoding="utf-8"))
            payload = current.model_dump()
            payload.update(updates)
            payload["updated_at"] = _now()
            updated = ChatRecord.model_validate(payload)
            RequestStore._atomic_write(path, updated.model_dump_json(indent=2))
            session_path = self._session_path(session_id)
            session = SessionRecord.model_validate_json(session_path.read_text(encoding="utf-8"))
            session_payload = session.model_dump()
            session_payload["updated_at"] = _now()
            RequestStore._atomic_write(session_path, SessionRecord.model_validate(session_payload).model_dump_json(indent=2))
            return updated

    def append_request(self, session_id: str, chat_id: str, request_id: str) -> ChatRecord:
        path = self._chat_path(session_id, chat_id)
        with exclusive_file_lock(self._lock_path(session_id)):
            current = ChatRecord.model_validate_json(path.read_text(encoding="utf-8"))
            if request_id in current.request_ids:
                return current
            payload = current.model_dump()
            payload.update(request_ids=[*current.request_ids, request_id], updated_at=_now())
            updated = ChatRecord.model_validate(payload)
            RequestStore._atomic_write(path, updated.model_dump_json(indent=2))
            return updated
