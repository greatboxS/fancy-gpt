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
from .project_models import AcceptanceCriterion, AgentAssignment, AgentOutcome, AgentRole, ConversationStrategy, CriterionStatus, ProjectArtifactRecord, FindingRecord, ProjectEvent, ProjectRecord, ProjectSnapshot, RelevantProjectContext, SessionRecord, TeamCyclePlan, TeamStepResult, WorkExecutionMode, WorkItem
from .project_service import ProjectService
from .project_store import load_verification_checks
from .project_runner import ProjectRunner
from .relevance import ResponseIntent
from .models import (
    ChatRecord,
    ContextPack,
    FinalReport,
    InteractionRequired,
    LocalContextRequirement,
    Priority,
    SessionInspection,
    RawRequest,
    RequestStatus,
    ResearchManifest,
    RoutingDecision,
    SessionRecord,
    SessionCapabilities,
)
from .server_stats import stats as _server_stats
from .server_stats import tracked
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
    root = Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    return ProjectService(root, verification_checks=load_verification_checks(root))


def _manager() -> TunnelManager:
    return TunnelManager(
        timeout_s=float(os.getenv("FANCY_GPT_BROWSER_TIMEOUT", "300")),
        headless=os.getenv("FANCY_GPT_BROWSER_HEADLESS", "1").lower() in {"1", "true", "yes"},
    )


@mcp.tool()
@tracked("prepare_request")
def prepare_request(
    request: RawRequest,
    skill: str | None = None,
    workflow: str | None = None,
) -> InteractionRequired:
    """Prepare the human-interactive planner path without selecting an automatic tunnel."""
    return _engine().prepare(request, skill_name=skill, workflow_name=workflow, open_browser=False)


@mcp.tool()
@tracked("run_request_automatic")
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
@tracked("submit_planner_result")
def submit_planner_result(request_id: str, manifest: ResearchManifest) -> InteractionRequired:
    return _engine().submit_planner(request_id, manifest.model_dump(mode="json"), open_browser=False)


@mcp.tool()
@tracked("submit_final_result")
def submit_final_result(request_id: str, result: FinalReport) -> FinalReport:
    return _engine().submit_final(request_id, result.model_dump(mode="json"))


@mcp.tool()
@tracked("get_request_status")
def get_request_status(request_id: str) -> RequestStatus:
    return _engine().status(request_id)


@mcp.tool()
@tracked("create_session")
def create_session(repo_root: str = ".", title: str | None = None) -> SessionRecord:
    """Create an isolated FancyGPT work session with its own chat collection."""
    return _engine().create_session(repo_root, title)


@mcp.tool()
@tracked("get_session")
def get_session(session_id: str) -> SessionRecord:
    return _engine().get_session(session_id)


@mcp.tool()
@tracked("list_sessions")
def list_sessions() -> list[SessionRecord]:
    return _engine().list_sessions()


@mcp.tool()
@tracked("close_session")
def close_session(session_id: str) -> SessionRecord:
    return _engine().close_session(session_id)


@mcp.tool()
@tracked("create_chat")
def create_chat(
    session_id: str,
    title: str,
    independent: bool = False,
    make_active: bool = True,
) -> ChatRecord:
    """Create a named chat; independent chats never replace the active main chat."""
    return _engine().create_chat(
        session_id, title, independent=independent, make_active=make_active
    )


@mcp.tool()
@tracked("list_chats")
def list_chats(session_id: str, include_archived: bool = False) -> list[ChatRecord]:
    return _engine().list_chats(session_id, include_archived=include_archived)


@mcp.tool()
@tracked("select_chat")
def select_chat(session_id: str, chat_id: str) -> SessionRecord:
    return _engine().select_chat(session_id, chat_id)


@mcp.tool()
@tracked("archive_chat")
def archive_chat(session_id: str, chat_id: str) -> ChatRecord:
    return _engine().archive_chat(session_id, chat_id)


@mcp.tool()
@tracked("list_session_requests")
def list_session_requests(session_id: str) -> list[RequestStatus]:
    return _engine().list_session_requests(session_id)


@mcp.tool()
@tracked("inspect_session")
def inspect_session(session_id: str) -> SessionInspection:
    """Read-only dashboard snapshot for a FancyGPT session, chats, latest requests, and recovery hints."""
    return _engine().inspect_session(session_id)


@mcp.tool()
@tracked("session_capabilities")
def session_capabilities() -> SessionCapabilities:
    """Advertise session/chat behavior so MCP clients can build the right UX."""
    return SessionCapabilities()


@mcp.tool()
@tracked("inspect_routing")
def inspect_routing(
    request: RawRequest,
    skill: str | None = None,
    workflow: str | None = None,
) -> RoutingDecision:
    return _engine().route(request, skill_name=skill, workflow_name=workflow)


@mcp.tool()
@tracked("inspect_context")
def inspect_context(request_id: str) -> ContextPack:
    return _engine().inspect_context(request_id)

@mcp.tool()
@tracked("list_sites")
def list_sites() -> list[SiteContract]:
    """List model-site adapters independently of browser tunnels."""
    return SiteRegistry().all()


@mcp.tool()
@tracked("list_tunnel_runtimes")
def list_tunnel_runtimes() -> list[RuntimeContract]:
    """List browser-runtime contracts available for tunnel composition."""
    return RuntimeRegistry().all()


@mcp.tool()
@tracked("list_tunnel_transports")
def list_tunnel_transports() -> list[TransportContract]:
    """List transport contracts available for tunnel composition."""
    return TransportRegistry().all()


@mcp.tool()
@tracked("inspect_tunnel_layers")
def inspect_tunnel_layers(tunnel_id: str) -> list[LayerHealth]:
    """Inspect static runtime/transport/composition health independently."""
    manager = _manager()
    return TunnelLayerInspector().inspect(manager.registry.get(tunnel_id))


@mcp.tool()
@tracked("list_tunnels")
def list_tunnels() -> list[TunnelSpec]:
    """List every built-in tunnel composition and its static capabilities."""
    return _manager().registry.all()


@mcp.tool()
@tracked("probe_tunnels")
def probe_tunnels() -> list[TunnelHealth]:
    """Probe runtime availability of all enabled tunnel compositions."""
    return _manager().health()


@mcp.tool()
@tracked("inspect_tunnel")
def inspect_tunnel(tunnel_id: str) -> TunnelHealth:
    manager = _manager()
    return manager.inspect(manager.registry.get(tunnel_id))


@mcp.tool()
@tracked("select_tunnel")
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


def _context_requirements(
    patterns: list[str] | None, paths: list[str] | None, required: bool
) -> list[LocalContextRequirement]:
    if not (patterns or paths):
        return []
    return [
        LocalContextRequirement(
            id="LC1",
            description="caller-declared work-item context",
            patterns=list(patterns or []),
            exact_paths=list(paths or []),
            priority=Priority.P0 if required else Priority.P1,
            required=required,
        )
    ]


@mcp.tool()
def add_project_work_item(
    project_id: str,
    title: str,
    objective: str,
    role: AgentRole,
    dependencies: list[str] | None = None,
    external_agent: bool = False,
    context_patterns: list[str] | None = None,
    context_paths: list[str] | None = None,
    context_required: bool = False,
    site: str | None = None,
) -> WorkItem:
    """Add one work item, optionally choosing which supported site answers it."""
    return _project_service().add_work_item(
        project_id,
        title=title,
        objective=objective,
        role=role,
        execution_mode=WorkExecutionMode.EXTERNAL_AGENT if external_agent else WorkExecutionMode.MODEL,
        dependencies=dependencies or [],
        context_requirements=_context_requirements(context_patterns, context_paths, context_required),
        site=site,
    )


@mcp.tool()
def set_project_work_item_context(
    project_id: str,
    work_item_id: str,
    context_patterns: list[str] | None = None,
    context_paths: list[str] | None = None,
    context_required: bool = False,
) -> WorkItem:
    """Point an existing work item at the repository source it must read."""
    return _project_service().set_context_requirements(
        project_id, work_item_id, _context_requirements(context_patterns, context_paths, context_required)
    )


@mcp.tool()
def get_project_status(project_id: str) -> ProjectSnapshot:
    return _project_service().snapshot(project_id)


@mcp.tool()
def retry_project_work_item(project_id: str, work_item_id: str) -> WorkItem:
    """Return a failed work item to the ready pool after fixing its cause."""
    return _project_service().retry_work_item(project_id, work_item_id)


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
    return ProjectRunner(_project_service(), manager=manager, tunnel_lock_factory=_tunnel_lock).run_next(
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
    return ProjectRunner(_project_service(), manager=manager, tunnel_lock_factory=_tunnel_lock).run_until_pause(
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
    site: str | None = None,
) -> FocusedAnswer:
    service = _project_service()
    session = service.session(project_id, session_id) if project_id and session_id else None
    if session and work_item_id and session.work_item_id != work_item_id:
        raise ValueError("session belongs to a different work item")
    if session and site and session.site != site:
        raise ValueError("session belongs to a different site")
    effective_work_item = session.work_item_id if session else work_item_id
    context = service.relevant_context(project_id, effective_work_item) if project_id else None
    manager = _manager()
    root = Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    coordinator = ExecutionCoordinator(root, manager=manager, tunnel_lock_factory=_tunnel_lock)
    answer = coordinator.run_focused(
        FocusedQuestion(
            question=question,
            domains=domains or [],
            response_intent=response_intent,
            principles=context.principles if context else FocusedQuestion(question=question).principles,
            project_context=context,
            conversation_strategy=session.conversation_strategy if session else ConversationStrategy.FRESH,
            conversation_binding=session.conversation_binding if session else None,
            site=site or (session.site if session else None),
        ),
        tunnel_id=tunnel_id,
        tunnel_policy=tunnel_policy,
    )
    if session and answer.conversation_binding:
        service.bind_session_conversation(project_id, session.session_id, answer.conversation_binding)
    return answer


@mcp.tool()
@tracked("list_skills")
def list_skills() -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in load_skills().values()]


@mcp.tool()
@tracked("list_workflows")
def list_workflows() -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in load_workflows().values()]


@mcp.tool()
@tracked("list_domains")
def list_domains() -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in load_domains().values()]


@mcp.tool()
@tracked("server_stats")
def server_stats() -> dict[str, object]:
    """Live MCP-server process stats: uptime, per-tool call/error counts and
    latency, plus the connected bridge's worker snapshot and job counters
    when a bridge is configured and reachable. Use this instead of guessing
    from probe_tunnels whether requests are actually flowing."""
    snapshot = _server_stats.snapshot()
    bridge: dict[str, object] = {"reachable": False}
    try:
        manager = _manager()
        spec = next(
            (item for item in manager.registry.effective() if item.runtime.value == "extension"),
            None,
        )
        if spec is not None:
            token_file = manager.driver_factory.token_file(spec)
            endpoint = manager.driver_factory.endpoint(spec)
            if endpoint is not None:
                from .bridge import load_or_create_token, probe_bridge_stats

                token = load_or_create_token(token_file)
                bridge = {"reachable": True, "endpoint": endpoint, **probe_bridge_stats(endpoint, token, open_timeout_s=0.75)}
    except Exception as exc:
        bridge = {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"process": snapshot, "bridge": bridge}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
