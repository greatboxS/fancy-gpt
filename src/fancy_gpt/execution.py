from __future__ import annotations

import json
import os
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, ContextManager

from pydantic import BaseModel, ConfigDict

from .engine import ReviewEngine
from .focused import FocusedAnswer, FocusedAnswerEngine, FocusedQuestion
from .project_models import AgentAssignment, AgentOutcome
from .team_agent import TeamAgentEngine
from .models import FinalReport, RawRequest
from .tunnels import TunnelManager


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionPhase(str, Enum):
    CREATED = "created"
    TUNNEL_SELECT = "tunnel-select"
    PROVIDER_START = "provider-start"
    PLANNER_DISPATCH = "planner-dispatch"
    PLANNER_WAIT = "planner-wait"
    MANIFEST_VALIDATE = "manifest-validate"
    CONTEXT_BUILD = "context-build"
    FINAL_DISPATCH = "final-dispatch"
    FINAL_WAIT = "final-wait"
    REPORT_VALIDATE = "report-validate"
    COMPLETE = "complete"
    FAILED = "failed"
    FOCUSED_DISPATCH = "focused-dispatch"
    FOCUSED_WAIT = "focused-wait"
    AGENT_DISPATCH = "agent-dispatch"
    AGENT_WAIT = "agent-wait"
    CANCELLED = "cancelled"


class ExecutionError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer: str
    code: str
    message: str
    retriable: bool = False


class ExecutionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str
    request_id: str
    kind: str = "review"
    phase: ExecutionPhase
    created_at: str
    updated_at: str
    tunnel_id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    work_item_id: str | None = None
    error: ExecutionError | None = None


class ExecutionFailed(RuntimeError):
    def __init__(self, status: ExecutionStatus) -> None:
        super().__init__(
            f"execution {status.execution_id} failed at {status.error.layer if status.error else status.phase.value}: "
            f"{status.error.message if status.error else 'unknown error'}"
        )
        self.status = status


class ExecutionStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve() / "executions"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, execution_id: str) -> Path:
        if not execution_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in execution_id):
            raise ValueError("invalid execution id")
        return self.root / f"{execution_id}.json"

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def save(self, status: ExecutionStatus) -> None:
        self._atomic_write(self._path(status.execution_id), status.model_dump_json(indent=2))

    def load(self, execution_id: str) -> ExecutionStatus:
        return ExecutionStatus.model_validate_json(self._path(execution_id).read_text(encoding="utf-8"))

    def list_recent(self, limit: int = 20) -> list[ExecutionStatus]:
        files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        return [ExecutionStatus.model_validate_json(path.read_text(encoding="utf-8")) for path in files]


class ExecutionCoordinator:
    """Creates observability state before tunnel/provider work can fail."""

    def __init__(
        self,
        workdir: Path,
        *,
        engine: ReviewEngine | None = None,
        manager: TunnelManager | None = None,
        tunnel_lock_factory: Callable[[str], ContextManager[object]] | None = None,
        focused_engine: FocusedAnswerEngine | None = None,
        agent_engine: TeamAgentEngine | None = None,
    ) -> None:
        self.workdir = workdir.expanduser().resolve()
        self.store = ExecutionStore(self.workdir)
        self.engine = engine or ReviewEngine(self.workdir)
        self.manager = manager or TunnelManager()
        self.tunnel_lock_factory = tunnel_lock_factory
        self.focused_engine = focused_engine or FocusedAnswerEngine()
        self.agent_engine = agent_engine or TeamAgentEngine()

    def _update(self, status: ExecutionStatus, *, phase: ExecutionPhase | None = None, tunnel_id: str | None = None, error: ExecutionError | None = None) -> ExecutionStatus:
        updated = status.model_copy(update={
            "phase": phase or status.phase,
            "tunnel_id": tunnel_id if tunnel_id is not None else status.tunnel_id,
            "error": error,
            "updated_at": _now(),
        })
        self.store.save(updated)
        return updated

    @staticmethod
    def _error_for(phase: ExecutionPhase, exc: Exception) -> ExecutionError:
        if phase == ExecutionPhase.TUNNEL_SELECT:
            layer, code, retriable = "tunnel", "TUNNEL_SELECTION_FAILED", True
        elif phase in {ExecutionPhase.PROVIDER_START, ExecutionPhase.PLANNER_DISPATCH, ExecutionPhase.PLANNER_WAIT, ExecutionPhase.FINAL_DISPATCH, ExecutionPhase.FINAL_WAIT, ExecutionPhase.FOCUSED_DISPATCH, ExecutionPhase.FOCUSED_WAIT, ExecutionPhase.AGENT_DISPATCH, ExecutionPhase.AGENT_WAIT}:
            layer, code, retriable = "provider", "MODEL_EXECUTION_FAILED", True
        elif phase == ExecutionPhase.CONTEXT_BUILD:
            layer, code, retriable = "context", "CONTEXT_BUILD_FAILED", False
        elif phase in {ExecutionPhase.MANIFEST_VALIDATE, ExecutionPhase.REPORT_VALIDATE}:
            layer, code, retriable = "validation", "SEMANTIC_VALIDATION_FAILED", False
        else:
            layer, code, retriable = "engine", "EXECUTION_FAILED", False
        return ExecutionError(layer=layer, code=code, message=f"{type(exc).__name__}: {exc}", retriable=retriable)


    def _new_status(
        self,
        *,
        kind: str,
        project_id: str | None = None,
        session_id: str | None = None,
        work_item_id: str | None = None,
    ) -> ExecutionStatus:
        now = _now()
        status = ExecutionStatus(
            execution_id=f"exec-{uuid.uuid4().hex[:12]}",
            request_id=uuid.uuid4().hex[:16],
            kind=kind,
            phase=ExecutionPhase.CREATED,
            created_at=now,
            updated_at=now,
            project_id=project_id,
            session_id=session_id,
            work_item_id=work_item_id,
        )
        self.store.save(status)
        return status

    def run_focused(
        self,
        question: FocusedQuestion,
        *,
        tunnel_id: str | None = None,
        tunnel_policy: str = "auto",
    ) -> FocusedAnswer:
        status = self._new_status(
            kind="focused",
            project_id=question.project_context.project_id if question.project_context else None,
        )
        current_phase = ExecutionPhase.CREATED
        try:
            current_phase = ExecutionPhase.TUNNEL_SELECT
            status = self._update(status, phase=current_phase)
            selection = self.manager.select(tunnel_id=tunnel_id, policy=tunnel_policy, require_automatic=True)
            status = self._update(status, tunnel_id=selection.tunnel_id)
            provider = self.manager.provider(selection)
            lock = self.tunnel_lock_factory(selection.tunnel_id) if self.tunnel_lock_factory else nullcontext()
            with lock:
                current_phase = ExecutionPhase.FOCUSED_DISPATCH
                status = self._update(status, phase=current_phase)
                current_phase = ExecutionPhase.FOCUSED_WAIT
                status = self._update(status, phase=current_phase)
                answer = self.focused_engine.run(question, provider, request_id=status.request_id)
            status = self._update(status, phase=ExecutionPhase.COMPLETE)
            return answer
        except Exception as exc:
            error = self._error_for(current_phase, exc)
            status = self._update(status, phase=ExecutionPhase.FAILED, error=error)
            raise ExecutionFailed(status) from exc

    def run_agent(
        self,
        assignment: AgentAssignment,
        *,
        tunnel_id: str | None = None,
        tunnel_policy: str = "auto",
    ) -> AgentOutcome:
        status = self._new_status(
            kind="team",
            project_id=assignment.project_id,
            session_id=assignment.session.session_id,
            work_item_id=assignment.work_item_id,
        )
        current_phase = ExecutionPhase.CREATED
        try:
            current_phase = ExecutionPhase.TUNNEL_SELECT
            status = self._update(status, phase=current_phase)
            selection = self.manager.select(tunnel_id=tunnel_id, policy=tunnel_policy, require_automatic=True)
            status = self._update(status, tunnel_id=selection.tunnel_id)
            provider = self.manager.provider(selection)
            lock = self.tunnel_lock_factory(selection.tunnel_id) if self.tunnel_lock_factory else nullcontext()
            with lock:
                current_phase = ExecutionPhase.AGENT_DISPATCH
                status = self._update(status, phase=current_phase)
                current_phase = ExecutionPhase.AGENT_WAIT
                status = self._update(status, phase=current_phase)
                outcome = self.agent_engine.run(assignment, provider, request_id=status.request_id)
            status = self._update(status, phase=ExecutionPhase.COMPLETE)
            return outcome
        except Exception as exc:
            error = self._error_for(current_phase, exc)
            status = self._update(status, phase=ExecutionPhase.FAILED, error=error)
            raise ExecutionFailed(status) from exc

    def run_review(
        self,
        request: RawRequest,
        *,
        skill_name: str | None = None,
        workflow_name: str | None = None,
        tunnel_id: str | None = None,
        tunnel_policy: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        work_item_id: str | None = None,
    ) -> FinalReport:
        status = self._new_status(
            kind="review",
            project_id=project_id,
            session_id=session_id,
            work_item_id=work_item_id,
        )
        request_id = status.request_id
        current_phase = ExecutionPhase.CREATED
        try:
            current_phase = ExecutionPhase.TUNNEL_SELECT
            status = self._update(status, phase=current_phase)
            selection = self.manager.select(
                tunnel_id=tunnel_id or request.tunnel,
                policy=tunnel_policy or request.tunnel_policy,
                require_automatic=True,
            )
            status = self._update(status, tunnel_id=selection.tunnel_id)
            provider = self.manager.provider(selection)

            def progress(name: str) -> None:
                nonlocal status, current_phase
                current_phase = ExecutionPhase(name)
                status = self._update(status, phase=current_phase)

            lock = self.tunnel_lock_factory(selection.tunnel_id) if self.tunnel_lock_factory else nullcontext()
            with lock:
                report = self.engine.run_automatic(
                    request,
                    provider,
                    skill_name=skill_name,
                    workflow_name=workflow_name,
                    request_id=request_id,
                    progress=progress,
                )
            status = self._update(status, phase=ExecutionPhase.COMPLETE)
            return report
        except ExecutionFailed:
            raise
        except Exception as exc:
            error = self._error_for(current_phase, exc)
            status = self._update(status, phase=ExecutionPhase.FAILED, error=error)
            raise ExecutionFailed(status) from exc
