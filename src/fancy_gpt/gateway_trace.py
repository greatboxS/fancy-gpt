"""An explicit, replayable record of what happened to one gateway turn.

A browser-backed turn crosses five processes - gateway, bridge, extension
worker, tab, and the site itself - and most of its interesting failures are not
reproducible on demand. So every turn keeps a trace that is enough to diagnose
it afterwards without re-running it.

What the trace deliberately does *not* hold is content. The prompt, the reply,
and the page URL all carry user data, and the URL can carry conversation or
session identifiers; recording them would turn a debugging aid into a leak. The
trace stores their *shape* instead: length, digest, and whether the thing was
there at all. A digest still lets you prove two turns saw the same text.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TraceStage(str, Enum):
    """Where in the pipeline an event happened."""

    ADMISSION = "admission"
    CAPABILITY = "capability"
    CORRELATION = "correlation"
    COMPACTION = "compaction"
    IDEMPOTENCY = "idempotency"
    LOCK = "lock"
    ROUTE = "route"
    PROVIDER = "provider"
    BROWSER = "browser"
    STREAM = "stream"
    CANCEL = "cancel"
    TERMINAL = "terminal"


#: Keys whose values must never be written to a trace, however they arrive.
FORBIDDEN_KEYS = frozenset({
    "prompt", "text", "response", "raw_text", "content", "messages", "instructions",
    "url", "page_url", "cookie", "cookies", "token", "authorization", "api_key",
    "password", "secret", "credential",
})

MAX_EVENTS = 500
MAX_VALUE_CHARS = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest_of(text: str) -> str:
    """Short digest, so two turns can be compared without storing either."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def shape_of(text: str | None) -> dict[str, Any]:
    """Describe a piece of content without recording it."""
    if text is None:
        return {"present": False}
    return {"present": True, "chars": len(text), "digest": digest_of(text)}


def _safe_value(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float) or value is None:
        return value
    if isinstance(value, str):
        return value[:MAX_VALUE_CHARS]
    if isinstance(value, dict):
        return {key: _safe_value(item) for key, item in value.items() if key.lower() not in FORBIDDEN_KEYS}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value][:20]
    return str(value)[:MAX_VALUE_CHARS]


def sanitize(data: dict[str, Any] | None) -> dict[str, Any]:
    """Drop anything that could carry content or credentials."""
    if not data:
        return {}
    return {
        key: _safe_value(value)
        for key, value in data.items()
        if key.lower() not in FORBIDDEN_KEYS
    }


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: str = Field(default_factory=_now)
    #: Milliseconds since the turn started, which is what you actually read.
    elapsed_ms: int = 0
    stage: TraceStage
    event: str
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class TurnTrace:
    """Append-only trace for one turn, persisted next to its other artifacts."""

    def __init__(self, response_id: str, path: Path | None = None) -> None:
        self.response_id = response_id
        self.path = path
        self.events: list[TraceEvent] = []
        self._guard = threading.Lock()
        self._started = datetime.now(timezone.utc)
        self._truncated = False

    def event(
        self,
        stage: TraceStage,
        event: str,
        detail: str = "",
        **data: Any,
    ) -> TraceEvent | None:
        """Record one step. Never raises: tracing must not break a turn."""
        try:
            elapsed = int((datetime.now(timezone.utc) - self._started).total_seconds() * 1000)
            record = TraceEvent(
                elapsed_ms=elapsed,
                stage=stage,
                event=event,
                detail=detail[:MAX_VALUE_CHARS],
                data=sanitize(data),
            )
            with self._guard:
                if len(self.events) >= MAX_EVENTS:
                    # A runaway turn must not grow the trace without bound; the
                    # fact that it was capped is itself recorded once.
                    if not self._truncated:
                        self._truncated = True
                        self.events.append(
                            TraceEvent(
                                elapsed_ms=elapsed, stage=stage, event="trace-truncated",
                                detail=f"stopped recording after {MAX_EVENTS} events",
                            )
                        )
                    return None
                self.events.append(record)
            self._append_to_file(record)
            return record
        except Exception:
            return None

    def _append_to_file(self, record: TraceEvent) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as stream:
                stream.write(record.model_dump_json() + "\n")
        except OSError:
            # Losing a trace line must never fail the turn it describes.
            pass

    def as_dicts(self) -> list[dict[str, Any]]:
        with self._guard:
            return [event.model_dump(mode="json") for event in self.events]

    def summary(self) -> dict[str, Any]:
        """A compact view: how long each stage took and how it ended."""
        with self._guard:
            events = list(self.events)
        stages: dict[str, int] = {}
        for event in events:
            stages[event.stage.value] = max(stages.get(event.stage.value, 0), event.elapsed_ms)
        terminal = next((item for item in reversed(events) if item.stage is TraceStage.TERMINAL), None)
        return {
            "response_id": self.response_id,
            "events": len(events),
            "total_ms": events[-1].elapsed_ms if events else 0,
            "stage_reached_ms": stages,
            "outcome": terminal.event if terminal else None,
            "outcome_detail": terminal.detail if terminal else "",
        }


def read_trace(path: Path) -> list[dict[str, Any]]:
    """Load a persisted trace, tolerating a partially written last line."""
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
