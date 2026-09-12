from __future__ import annotations

from .orchestrator import TeamOrchestrator
from .project_models import (
    AgentOutcomeStatus,
    TeamStepResult,
    TeamStepStatus,
    WorkExecutionMode,
    WorkItemState,
)
from .project_service import ProjectService
from .team_agent import TeamAgentEngine
from .tunnels import TunnelManager


class ProjectRunner:
    """Execute the next project work item without collapsing orchestration into MCP/CLI.

    Model-backed roles are executed through the runtime-selected tunnel. Local
    mutation roles default to EXTERNAL_AGENT and are handed to Codex/Claude (or
    another MCP client) as a structured AgentAssignment instead of pretending a
    browser model modified the repository.
    """

    def __init__(
        self,
        service: ProjectService,
        *,
        manager: TunnelManager | None = None,
        agent_engine: TeamAgentEngine | None = None,
    ) -> None:
        self.service = service
        self.orchestrator = TeamOrchestrator(service)
        self.manager = manager or TunnelManager()
        self.agent_engine = agent_engine or TeamAgentEngine()

    def _persist_outcome(self, project_id: str, outcome) -> None:
        self.service.apply_agent_outcome(project_id, outcome)

    def run_next(
        self,
        project_id: str,
        *,
        tunnel_id: str | None = None,
        tunnel_policy: str = "auto",
    ) -> TeamStepResult:
        plan = self.orchestrator.continue_project(project_id)
        if plan.complete:
            return TeamStepResult(
                project_id=project_id,
                status=TeamStepStatus.PROJECT_COMPLETE,
                reason=plan.reason,
            )
        if plan.blocked:
            return TeamStepResult(
                project_id=project_id,
                status=TeamStepStatus.BLOCKED,
                reason=plan.reason,
            )
        if not plan.ready_work_items:
            return TeamStepResult(
                project_id=project_id,
                status=TeamStepStatus.IDLE,
                reason=plan.reason,
            )

        # Deterministic graph order: work-items are materialized in journal order.
        item = plan.ready_work_items[0]
        assignment = self.service.start_assignment(project_id, item.work_item_id)
        if item.execution_mode == WorkExecutionMode.EXTERNAL_AGENT:
            return TeamStepResult(
                project_id=project_id,
                status=TeamStepStatus.EXTERNAL_ASSIGNMENT_REQUIRED,
                work_item=item,
                assignment=assignment,
                reason="work item requires a local/external agent with repository mutation capability",
            )

        selection = self.manager.select(
            tunnel_id=tunnel_id,
            policy=tunnel_policy,
            require_automatic=True,
        )
        try:
            outcome = self.agent_engine.run(assignment, self.manager.provider(selection))
            self._persist_outcome(project_id, outcome)
            if outcome.status == AgentOutcomeStatus.BLOCKED:
                status = TeamStepStatus.BLOCKED
            elif outcome.status == AgentOutcomeStatus.NEEDS_EXTERNAL_ACTION:
                status = TeamStepStatus.EXTERNAL_ASSIGNMENT_REQUIRED
            else:
                status = TeamStepStatus.MODEL_COMPLETED
            return TeamStepResult(
                project_id=project_id,
                status=status,
                work_item=item,
                assignment=assignment,
                outcome=outcome,
                reason=f"{assignment.role.value} assignment executed via {selection.tunnel_id}",
            )
        except Exception:
            # Failure is durable and visible in project state. Do not silently retry a model turn.
            self.service.finish_session(
                project_id,
                assignment.session.session_id,
                summary="model-backed team assignment failed before a valid outcome was persisted",
                failed=True,
            )
            raise

    def run_until_pause(
        self,
        project_id: str,
        *,
        tunnel_id: str | None = None,
        tunnel_policy: str = "auto",
        max_steps: int = 8,
    ) -> list[TeamStepResult]:
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        results: list[TeamStepResult] = []
        for _ in range(max_steps):
            result = self.run_next(project_id, tunnel_id=tunnel_id, tunnel_policy=tunnel_policy)
            results.append(result)
            if result.status in {
                TeamStepStatus.EXTERNAL_ASSIGNMENT_REQUIRED,
                TeamStepStatus.PROJECT_COMPLETE,
                TeamStepStatus.BLOCKED,
                TeamStepStatus.IDLE,
            }:
                break
        return results
