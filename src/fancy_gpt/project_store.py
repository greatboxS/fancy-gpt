from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .file_lock import exclusive_file_lock
from .project_models import ProjectEvent, ProjectEventType, ProjectRecord


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectStore:
    """Append-only project journal plus immutable project metadata.

    Chat/session history is not replayed directly into models. Higher layers
    reduce this journal into a small relevant working set for each agent.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.projects_dir = self.root / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_id(project_id: str) -> None:
        if not project_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in project_id):
            raise ValueError("invalid project id")

    def project_dir(self, project_id: str) -> Path:
        self._validate_id(project_id)
        path = self.projects_dir / project_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _lock_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / ".project.lock"

    @contextmanager
    def lock(self, project_id: str) -> Iterator[None]:
        with exclusive_file_lock(self._lock_path(project_id)):
            yield

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def create(self, project: ProjectRecord) -> None:
        directory = self.project_dir(project.project_id)
        meta = directory / "project.json"
        with self.lock(project.project_id):
            if meta.exists():
                raise FileExistsError(f"project already exists: {project.project_id}")
            self._atomic_write(meta, project.model_dump_json(indent=2))
            self._append_unlocked(project.project_id, ProjectEventType.PROJECT_CREATED, project.model_dump(mode="json"))

    def load_project(self, project_id: str) -> ProjectRecord:
        path = self.project_dir(project_id) / "project.json"
        with self.lock(project_id):
            return ProjectRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def save_project(self, project: ProjectRecord) -> None:
        path = self.project_dir(project.project_id) / "project.json"
        with self.lock(project.project_id):
            self._atomic_write(path, project.model_dump_json(indent=2))

    def _events_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "events.jsonl"

    def _read_events_unlocked(self, project_id: str) -> list[ProjectEvent]:
        path = self._events_path(project_id)
        if not path.exists():
            return []
        events: list[ProjectEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(ProjectEvent.model_validate_json(line))
        return events

    def events(self, project_id: str) -> list[ProjectEvent]:
        with self.lock(project_id):
            return self._read_events_unlocked(project_id)

    def _append_unlocked(self, project_id: str, event_type: ProjectEventType, payload: dict) -> ProjectEvent:
        events = self._read_events_unlocked(project_id)
        event = ProjectEvent(
            sequence=(events[-1].sequence + 1 if events else 1),
            event_type=event_type,
            timestamp=utc_now(),
            payload=payload,
        )
        path = self._events_path(project_id)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(event.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return event

    def append(self, project_id: str, event_type: ProjectEventType, payload: dict) -> ProjectEvent:
        with self.lock(project_id):
            return self._append_unlocked(project_id, event_type, payload)

    def list_project_ids(self) -> list[str]:
        return sorted(path.name for path in self.projects_dir.iterdir() if path.is_dir() and (path / "project.json").exists())
