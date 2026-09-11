from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TypeVar

from pydantic import BaseModel

from .errors import InvalidStateError
from .file_lock import exclusive_file_lock
from contextlib import contextmanager
from .models import RequestMode, RequestState, RequestStatus

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
