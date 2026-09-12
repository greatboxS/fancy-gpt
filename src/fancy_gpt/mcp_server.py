from __future__ import annotations

import os
import threading
from pathlib import Path

from mcp.server import MCPServer

from .catalog import load_domains, load_skills, load_workflows
from .engine import ReviewEngine
from .execution import ExecutionCoordinator, ExecutionStatus, ExecutionStore
from .focused import FocusedAnswer, FocusedAnswerEngine, FocusedQuestion
from .orchestrator import TeamOrchestrator
from .project_models import AcceptanceCriterion, AgentAssignment, AgentOutcome, ConversationStrategy, CriterionStatus, ProjectArtifactRecord, FindingRecord, ProjectEvent, ProjectRecord, ProjectSnapshot, RelevantProjectContext, SessionRecord, TeamCyclePlan, TeamStepResult
from .project_service import ProjectService
from .project_runner import ProjectRunner
from .relevance import ResponseIntent
from .models import (
    ContextPack,
    FinalReport,
    InteractionRequired,
    RawRequest,
    RequestStatus,
    ResearchManifest,
    RoutingDecision,
)
from .tunnels import TunnelHealth, TunnelManager, TunnelSelection, TunnelSpec, TunnelLayerInspector
from .web.models import LayerHealth
from .web.runtime import RuntimeContract, RuntimeRegistry
from .web.sites import SiteContract, SiteRegistry
from .web.transport import TransportContract, TransportRegistry

mcp = MCPServer("fancy-gpt")
_tunnel_locks_guard = threading.Lock()
_tunnel_locks: dict[str, threading.Lock] = {}


def _tunnel_lock(tunnel_id: str) -> threading.Lock:
    with _tunnel_locks_guard:
        return _tunnel_locks.setdefault(tunnel_id, threading.Lock())


def _allowed_roots() -> list[Path] | None:
    raw = os.getenv("FANCY_GPT_ALLOWED_ROOTS", "").strip()
    if not raw:
        return None
    return [Path(item) for item in raw.split(os.pathsep) if item]


def _engine() -> ReviewEngine:
    return ReviewEngine(
        Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt")),
        allowed_roots=_allowed_roots(),
    )



def _project_service() -> ProjectService:
    return ProjectService(Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt")))


def _manager() -> TunnelManager:
    return TunnelManager(
        timeout_s=float(os.getenv("FANCY_GPT_BROWSER_TIMEOUT", "300")),
        headless=os.getenv("FANCY_GPT_BROWSER_HEADLESS", "1").lower() in {"1", "true", "yes"},
    )


@mcp.tool()
def prepare_request(
    request: RawRequest,
    skill: str | None = None,
    workflow: str | None = None,
) -> InteractionRequired:
    """Prepare the human-interactive planner path without selecting an automatic tunnel."""
    return _engine().prepare(request, skill_name=skill, workflow_name=workflow, open_browser=False)


@mcp.tool()
def run_request_automatic(
    request: RawRequest,
    skill: str | None = None,
    workflow: str | None = None,
    tunnel_id: str | None = None,
    tunnel_policy: str | None = None,
) -> FinalReport:
    """Run planner + final turns through a runtime-selected browser tunnel.

    `tunnel_id` overrides request.tunnel. When omitted, the resolver probes
    available tunnel compositions and applies request.tunnel_policy.
    """
    manager = _manager()
    root = Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    coordinator = ExecutionCoordinator(
        root, engine=_engine(), manager=manager, tunnel_lock_factory=_tunnel_lock
    )
    return coordinator.run_review(
        request,
        skill_name=skill,
        workflow_name=workflow,
        tunnel_id=tunnel_id or request.tunnel,
        tunnel_policy=tunnel_policy or request.tunnel_policy,
    )


@mcp.tool()
def get_execution_status(execution_id: str) -> ExecutionStatus:
    root = Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    return ExecutionStore(root).load(execution_id)


@mcp.tool()
def list_recent_executions(limit: int = 20) -> list[ExecutionStatus]:
    root = Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    return ExecutionStore(root).list_recent(limit)


@mcp.tool()
def submit_planner_result(request_id: str, manifest: ResearchManifest) -> InteractionRequired:
    return _engine().submit_planner(request_id, manifest.model_dump(mode="json"), open_browser=False)


@mcp.tool()
def submit_final_result(request_id: str, result: FinalReport) -> FinalReport:
    return _engine().submit_final(request_id, result.model_dump(mode="json"))


@mcp.tool()
def get_request_status(request_id: str) -> RequestStatus:
    return _engine().status(request_id)


@mcp.tool()
def inspect_routing(
    request: RawRequest,
    skill: str | None = None,
    workflow: str | None = None,
) -> RoutingDecision:
    return _engine().route(request, skill_name=skill, workflow_name=workflow)


@mcp.tool()
def inspect_context(request_id: str) -> ContextPack:
    return _engine().inspect_context(request_id)




@mcp.tool()
def list_tunnel_sites() -> list[SiteContract]:
    """List site-adapter contracts available for tunnel composition."""
    return SiteRegistry().all()


@mcp.tool()
def list_tunnel_runtimes() -> list[RuntimeContract]:
    """List browser-runtime contracts available for tunnel composition."""
    return RuntimeRegistry().all()


@mcp.tool()
def list_tunnel_transports() -> list[TransportContract]:
    """List transport contracts available for tunnel composition."""
    return TransportRegistry().all()


@mcp.tool()
def inspect_tunnel_layers(tunnel_id: str) -> list[LayerHealth]:
    """Inspect static site/runtime/transport/composition health independently."""
    manager = _manager()
    return TunnelLayerInspector().inspect(manager.registry.get(tunnel_id))


@mcp.tool()
def list_tunnels() -> list[TunnelSpec]:
    """List every built-in tunnel composition and its static capabilities."""
    return _manager().registry.all()


@mcp.tool()
def probe_tunnels() -> list[TunnelHealth]:
    """Probe runtime availability of all enabled tunnel compositions."""
    return _manager().health()


@mcp.tool()
def inspect_tunnel(tunnel_id: str) -> TunnelHealth:
    manager = _manager()
    return manager.inspect(manager.registry.get(tunnel_id))


@mcp.tool()
def select_tunnel(
    tunnel_id: str | None = None,
    tunnel_policy: str = "auto",
) -> TunnelSelection:
    """Resolve the tunnel that would be used without running a review."""
    return _manager().select(tunnel_id=tunnel_id, policy=tunnel_policy, require_automatic=True)


@mcp.tool()
def create_project(
    name: str,
    target: str,
    repo_root: str = ".",
    acceptance: list[str] | None = None,
    project_id: str | None = None,
) -> ProjectRecord:
    criteria = [AcceptanceCriterion(id=f"ac-{index + 1}", statement=item) for index, item in enumerate(acceptance or [])]
    return _project_service().create_project(
        name=name, target=target, repo_root=repo_root, acceptance=criteria, project_id=project_id
    )


@mcp.tool()
def bootstrap_project_cycle(project_id: str) -> list[str]:
    service = _project_service()
    return TeamOrchestrator(service).bootstrap_developer_cycle(project_id)


@mcp.tool()
def get_project_status(project_id: str) -> ProjectSnapshot:
    return _project_service().snapshot(project_id)


@mcp.tool()
def continue_project(project_id: str) -> TeamCyclePlan:
    service = _project_service()
    return TeamOrchestrator(service).continue_project(project_id)


@mcp.tool()
def run_project_next(
    project_id: str,
    tunnel_id: str | None = None,
    tunnel_policy: str = "auto",
) -> TeamStepResult:
    """Run one ready team work item, or return a structured external-agent assignment."""
    manager = _manager()
    return ProjectRunner(_project_service(), manager=manager).run_next(
        project_id, tunnel_id=tunnel_id, tunnel_policy=tunnel_policy
    )


@mcp.tool()
def run_project_until_pause(
    project_id: str,
    tunnel_id: str | None = None,
    tunnel_policy: str = "auto",
    max_steps: int = 8,
) -> list[TeamStepResult]:
    """Run model-backed teammates until complete, blocked, idle, or external-agent handoff."""
    manager = _manager()
    return ProjectRunner(_project_service(), manager=manager).run_until_pause(
        project_id, tunnel_id=tunnel_id, tunnel_policy=tunnel_policy, max_steps=max_steps
    )


@mcp.tool()
def record_project_finding(
    project_id: str,
    claim: str,
    impact: str,
    severity: str = "medium",
    required_action: str | None = None,
    session_id: str | None = None,
) -> FindingRecord:
    return _project_service().record_finding(
        project_id, claim=claim, impact=impact, severity=severity,
        required_action=required_action, source_session_id=session_id
    )


@mcp.tool()
def resolve_project_finding(project_id: str, finding_id: str, resolution: str) -> FindingRecord:
    return _project_service().resolve_finding(project_id, finding_id, resolution=resolution)


@mcp.tool()
def record_project_artifact(
    project_id: str,
    path: str,
    kind: str = "file",
    sha256: str | None = None,
    description: str | None = None,
    session_id: str | None = None,
) -> ProjectArtifactRecord:
    return _project_service().record_artifact(
        project_id, path=path, kind=kind, sha256=sha256, description=description,
        source_session_id=session_id
    )


@mcp.tool()
def get_project_history(project_id: str, limit: int = 100) -> list[ProjectEvent]:
    """Return durable project events without replaying raw browser chat history."""
    return _project_service().history(project_id, limit=limit)


@mcp.tool()
def get_relevant_project_context(project_id: str, work_item_id: str | None = None) -> RelevantProjectContext:
    return _project_service().relevant_context(project_id, work_item_id)


@mcp.tool()
def start_agent_assignment(
    project_id: str,
    work_item_id: str,
    conversation_strategy: ConversationStrategy | None = None,
    conversation_binding: str | None = None,
) -> AgentAssignment:
    return _project_service().start_assignment(
        project_id, work_item_id, conversation_strategy=conversation_strategy, conversation_binding=conversation_binding
    )


@mcp.tool()
def start_project_session(
    project_id: str,
    work_item_id: str,
    conversation_strategy: ConversationStrategy | None = None,
    conversation_binding: str | None = None,
) -> SessionRecord:
    return _project_service().start_session(
        project_id, work_item_id, conversation_strategy=conversation_strategy, conversation_binding=conversation_binding
    )


@mcp.tool()
def submit_agent_outcome(project_id: str, outcome: AgentOutcome) -> SessionRecord:
    """Persist one structured teammate handoff and close/transition its session."""
    return _project_service().apply_agent_outcome(project_id, outcome)


@mcp.tool()
def finish_project_session(project_id: str, session_id: str, summary: str, failed: bool = False) -> SessionRecord:
    return _project_service().finish_session(project_id, session_id, summary=summary, failed=failed)


@mcp.tool()
def record_project_evidence(
    project_id: str,
    claim: str,
    source: str,
    locator: str | None = None,
    session_id: str | None = None,
):
    return _project_service().record_evidence(
        project_id, claim=claim, source=source, locator=locator, source_session_id=session_id
    )


@mcp.tool()
def update_project_criterion(
    project_id: str,
    criterion_id: str,
    status: CriterionStatus,
    evidence_ids: list[str] | None = None,
):
    return _project_service().update_criterion(
        project_id, criterion_id, status=status, evidence_ids=evidence_ids
    )


@mcp.tool()
def ask_focused(
    question: str,
    domains: list[str] | None = None,
    response_intent: ResponseIntent = ResponseIntent.FOCUSED,
    project_id: str | None = None,
    work_item_id: str | None = None,
    session_id: str | None = None,
    tunnel_id: str | None = None,
    tunnel_policy: str = "auto",
) -> FocusedAnswer:
    service = _project_service()
    session = service.session(project_id, session_id) if project_id and session_id else None
    if session and work_item_id and session.work_item_id != work_item_id:
        raise ValueError("session belongs to a different work item")
    effective_work_item = session.work_item_id if session else work_item_id
    context = service.relevant_context(project_id, effective_work_item) if project_id else None
    manager = _manager()
    selection = manager.select(tunnel_id=tunnel_id, policy=tunnel_policy, require_automatic=True)
    with _tunnel_lock(selection.tunnel_id):
        answer = FocusedAnswerEngine().run(
            FocusedQuestion(
                question=question,
                domains=domains or [],
                response_intent=response_intent,
                principles=context.principles if context else FocusedQuestion(question=question).principles,
                project_context=context,
                conversation_strategy=session.conversation_strategy if session else ConversationStrategy.FRESH,
                conversation_binding=session.conversation_binding if session else None,
            ),
            manager.provider(selection),
        )
    if session and answer.conversation_binding:
        service.bind_session_conversation(project_id, session.session_id, answer.conversation_binding)
    return answer


@mcp.tool()
def list_skills() -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in load_skills().values()]


@mcp.tool()
def list_workflows() -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in load_workflows().values()]


@mcp.tool()
def list_domains() -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in load_domains().values()]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
