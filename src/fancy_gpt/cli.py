from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from .browser import FakeBrowserDriver, PlaywrightChatGPTDriver
from .catalog import load_domains, load_skills, load_workflows
from .engine import ReviewEngine
from .execution import ExecutionCoordinator, ExecutionStore
from .mcp_clients import MCPClientKind, MCPClientRegistry
from .io import load_mapping
from .models import ChatPolicy, FinalReport, InspectedChat, LocalContextRequirement, Priority, RawRequest, ResearchManifest, SessionInspection
from .focused import FocusedAnswerEngine, FocusedQuestion
from .orchestrator import TeamOrchestrator
from .project_models import AcceptanceCriterion, AgentOutcome, AgentRole, ConversationStrategy, CriterionStatus, WorkExecutionMode
from .conversation_index import ConversationIndex
from .project_service import ProjectService
from .project_store import load_verification_checks, save_verification_checks
from .project_runner import ProjectRunner
from .relevance import ResponseIntent
from .providers import ChatGPTWebAutomationProvider
from .skills import export_packaged_skills, packaged_skills_root, validate_skill_bundle
from .runtime_paths import default_browser_profile, user_data_dir, default_bridge_token_file, default_bridge_native_config
from .bridge import BridgeServer, load_or_create_token
from .bridge.native_manifest import install_manifest, native_host_manifest
from .extension_utils import adapter_build_id, export_extension
from .tunnels import TunnelManager, TunnelRegistry, TunnelLayerInspector
from .web.runtime import RuntimeRegistry
from .web.sites import SiteRegistry
from .web.transport import TransportRegistry
from .self_test import run_self_test
from . import __version__


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def _yn(value: bool | None) -> str:
    return "-" if value is None else ("yes" if value else "no")


def _table(headers: list[str], rows: list[list[str]], max_widths: list[int | None] | None = None) -> str:
    if not rows:
        return "(none)"
    caps = max_widths or [None] * len(headers)
    capped = [
        [cell if caps[i] is None else _truncate(cell, caps[i]) for i, cell in enumerate(row)]
        for row in rows
    ]
    widths = [len(h) for h in headers]
    for row in capped:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    last = len(headers) - 1
    def fmt(row: list[str]) -> str:
        return "  ".join(cell if i == last else cell.ljust(widths[i]) for i, cell in enumerate(row))
    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines.extend(fmt(row) for row in capped)
    return "\n".join(lines)


def _bridge_summary(metadata: dict) -> str:
    bridge = metadata.get("bridge") if isinstance(metadata, dict) else None
    if not isinstance(bridge, dict):
        return "-"
    stats = bridge.get("stats") or {}
    workers = bridge.get("workers") or []
    connected = stats.get("connected_workers", sum(1 for w in workers if w.get("alive")))
    jobs_ok = stats.get("total_jobs_succeeded", "-")
    jobs_failed = stats.get("total_jobs_failed", "-")
    return f"workers={connected} jobs_ok={jobs_ok} jobs_failed={jobs_failed}"


def _request_summary(chat: InspectedChat) -> list[str]:
    latest = chat.latest_request
    if latest is None:
        return ["  Latest request: none"]
    route = latest.route_name or latest.skill
    lines = [
        f"  Latest request: {latest.request_id}  {latest.state.value}  {latest.mode.value}  {route}",
    ]
    if latest.has_partial_text:
        lines.append(f"  Progress: partial text updated {latest.partial_text_updated_at or '-'}")
    if latest.error:
        lines.append(f"  Error: {_truncate(latest.error, 100)}")
    if latest.recovery_hint:
        lines.append(f"  Hint: {latest.recovery_hint.message}")
    return lines


def _session_inspection_text(inspection: SessionInspection) -> str:
    lines = [
        f"Session {inspection.session_id}  {inspection.title}",
        f"Repo: {inspection.repo_root}",
        f"State: {inspection.state}",
        f"Active chat: {inspection.active_chat_id or '-'}",
        f"Chats: {inspection.chat_count}  Requests: {inspection.request_count}",
        "",
        "Chats",
    ]
    if not inspection.chats:
        lines.append("(none)")
    for index, chat in enumerate(inspection.chats):
        branch = "└─" if index == len(inspection.chats) - 1 else "├─"
        flags = []
        if chat.is_active:
            flags.append("active")
        if chat.kind.value == "independent":
            flags.append("independent")
        if chat.is_archived:
            flags.append("archived")
        flag_text = f"  {', '.join(flags)}" if flags else ""
        conversation = chat.conversation_id or chat.conversation_binding_state
        lines.append(f"{branch} {chat.chat_id}  {chat.title}  {chat.kind.value}{flag_text}")
        lines.append(f"  Conversation: {conversation}")
        if chat.tunnel_id:
            lines.append(f"  Tunnel: {chat.tunnel_id}")
        lines.extend(_request_summary(chat))
    lines.extend(["", "Tunnels"])
    if not inspection.tunnels:
        lines.append("(none)")
    for tunnel in inspection.tunnels:
        browser = _yn(tunnel.browser_connected)
        bridge = _yn(tunnel.bridge_reachable)
        detail = f"  {tunnel.detail}" if tunnel.detail else ""
        lines.append(f"└─ {tunnel.tunnel_id or '-'}  {tunnel.state}  browser={browser}  bridge={bridge}{detail}")
    return "\n".join(lines)

app = typer.Typer(help="fancy-gpt independent technical reasoning toolkit")
catalogs_app = typer.Typer(help="Inspect skills, workflows, domains, and routing")
validate_app = typer.Typer(help="Validate structured payloads")
browser_app = typer.Typer(help="ChatGPT Web browser automation utilities")
skills_app = typer.Typer(help="Validate/export Agent Skill bundles")
tunnels_app = typer.Typer(help="Inspect, probe, and select browser tunnels")
bridge_app = typer.Typer(help="Run/pair the browser bridge used by extension tunnels")
extension_app = typer.Typer(help="Export/configure Chrome, Edge, and Firefox tunnel extensions")
conversations_app = typer.Typer(help="Inspect the ChatGPT threads this runtime opened")
sessions_app = typer.Typer(help="Manage persistent FancyGPT work sessions")
chats_app = typer.Typer(help="Manage chats inside a FancyGPT session")
project_app = typer.Typer(help="Persistent engineering projects, sessions, work items, and evidence")
execution_app = typer.Typer(help="Inspect tracked execution lifecycle and structured failures")
clients_app = typer.Typer(help="Inspect/register FancyGPT with MCP clients such as Codex and Claude Code")
app.add_typer(catalogs_app, name="catalogs")
app.add_typer(validate_app, name="validate")
app.add_typer(browser_app, name="browser")
app.add_typer(skills_app, name="skills")
app.add_typer(tunnels_app, name="tunnels")
app.add_typer(bridge_app, name="bridge")
app.add_typer(extension_app, name="extension")
app.add_typer(conversations_app, name="conversations")
app.add_typer(sessions_app, name="sessions")
app.add_typer(chats_app, name="chats")
app.add_typer(project_app, name="project")
app.add_typer(execution_app, name="execution")
app.add_typer(clients_app, name="clients")


def _engine(workdir: Path | None, allowed_root: list[Path] | None = None) -> ReviewEngine:
    root = workdir or Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    return ReviewEngine(root, allowed_roots=allowed_root)



def _project_service(workdir: Path | None) -> ProjectService:
    root = workdir or Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    return ProjectService(root, verification_checks=load_verification_checks(root))


def _route_args(skill: str | None, workflow: str | None) -> tuple[str | None, str | None]:
    if bool(skill) == bool(workflow):
        raise typer.BadParameter("select exactly one of --skill or --workflow")
    return skill, workflow

_MODE_SKILL = {
    "review": "technical-review",
    "design": "independent-design",
    "consult": "technical-consult",
    "investigate": "root-cause-investigation",
    "verify": "evidence-verification",
    "write": "technical-writing",
}

def _auto_route(request: RawRequest, skill: str | None, workflow: str | None) -> tuple[str | None, str | None]:
    if skill and workflow:
        raise typer.BadParameter("select at most one of --skill or --workflow")
    if workflow:
        return None, workflow
    return skill or _MODE_SKILL[request.mode.value], None


@app.command("version")
def version_cmd() -> None:
    """Print installed version."""
    typer.echo(__version__)


@app.command("test")
def test_cmd() -> None:
    """Run the standalone offline package self-test."""
    typer.echo(json.dumps(run_self_test(), indent=2))


@app.command("verify")
def verify_cmd(
    browser: Annotated[bool, typer.Option("--browser/--no-browser")] = False,
    profile_dir: Annotated[Path, typer.Option("--profile-dir")] = default_browser_profile(),
) -> None:
    """Verify installed runtime; optionally launch Chromium and check ChatGPT UI."""
    result = run_self_test()
    if browser:
        driver = PlaywrightChatGPTDriver(profile_dir, headless=True)
        try:
            driver.start()
            result["browser"] = "launch-ok"
            result["profile_dir"] = str(profile_dir)
        finally:
            driver.stop()
    typer.echo(json.dumps(result, indent=2))


@app.command("init")
def init_cmd(
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("fancy-gpt-request.yaml"),
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Create a minimal request template in the current project."""
    if output.exists() and not force:
        raise typer.BadParameter(f"{output} already exists; use --force to overwrite")
    output.write_text(
        """mode: review
objective: Review this project for architecture, correctness, failure modes, and verification gaps.
repo_root: .
domains:
  - architecture
focus:
  - boundaries and ownership
  - failure behavior
  - verification evidence
freshness: version-specific
risk: production
include_git_diff: true
""",
        encoding="utf-8",
    )
    typer.echo(str(output))


@app.command("browser-setup")
def browser_setup(
    with_deps: Annotated[bool, typer.Option("--with-deps")] = False,
) -> None:
    """Install Playwright Chromium for the installed fancy-gpt runtime."""
    import subprocess, sys
    cmd = [sys.executable, "-m", "playwright", "install"]
    if with_deps:
        cmd.append("--with-deps")
    cmd.append("chromium")
    subprocess.run(cmd, check=True)
    typer.echo(json.dumps({"ok": True, "chromium": "installed", "with_deps": with_deps}, indent=2))


@app.command("run")
def run_cmd(
    request_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    skill: Annotated[str | None, typer.Option("--skill")] = None,
    workflow: Annotated[str | None, typer.Option("--workflow")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
    allowed_root: Annotated[list[Path] | None, typer.Option("--allowed-root")] = None,
    profile_dir: Annotated[Path, typer.Option("--profile-dir")] = default_browser_profile(),
    timeout_s: Annotated[float, typer.Option("--timeout")] = 300.0,
    headless: Annotated[bool, typer.Option("--headless")] = True,
    tunnel: Annotated[str | None, typer.Option("--tunnel")] = None,
    tunnel_policy: Annotated[str | None, typer.Option("--tunnel-policy")] = None,
    session_id: Annotated[str | None, typer.Option("--session")] = None,
    chat_id: Annotated[str | None, typer.Option("--chat")] = None,
    chat_policy: Annotated[ChatPolicy | None, typer.Option("--chat-policy")] = None,
) -> None:
    """Run through a runtime-selected tunnel; skill is inferred from mode by default."""
    request = RawRequest.model_validate(load_mapping(request_file))
    request = request.model_copy(update={
        **({"session_id": session_id} if session_id else {}),
        **({"chat_id": chat_id} if chat_id else {}),
        **({"chat_policy": chat_policy} if chat_policy else {}),
    })
    skill, workflow = _auto_route(request, skill, workflow)
    manager = TunnelManager(timeout_s=timeout_s, headless=headless)
    root = workdir or Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    coordinator = ExecutionCoordinator(root, engine=_engine(workdir, allowed_root), manager=manager)
    result = coordinator.run_review(
        request,
        skill_name=skill,
        workflow_name=workflow,
        tunnel_id=tunnel or request.tunnel,
        tunnel_policy=tunnel_policy or request.tunnel_policy,
    )
    typer.echo(result.model_dump_json(indent=2))


@app.command("ask")
def ask_cmd(
    question: Annotated[str, typer.Argument()],
    domain: Annotated[list[str] | None, typer.Option("--domain")] = None,
    intent: Annotated[ResponseIntent, typer.Option("--intent")] = ResponseIntent.FOCUSED,
    project_id: Annotated[str | None, typer.Option("--project")] = None,
    work_item_id: Annotated[str | None, typer.Option("--work-item")] = None,
    session_id: Annotated[str | None, typer.Option("--session")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
    tunnel: Annotated[str | None, typer.Option("--tunnel")] = None,
    tunnel_policy: Annotated[str, typer.Option("--tunnel-policy")] = "auto",
    timeout_s: Annotated[float, typer.Option("--timeout")] = 300.0,
) -> None:
    """Ask one focused technical question without forcing a full review report."""
    service = _project_service(workdir)
    session = service.session(project_id, session_id) if project_id and session_id else None
    if session and work_item_id and session.work_item_id != work_item_id:
        raise typer.BadParameter("--session belongs to a different work item")
    effective_work_item = session.work_item_id if session else work_item_id
    context = service.relevant_context(project_id, effective_work_item) if project_id else None
    manager = TunnelManager(timeout_s=timeout_s)
    root = workdir or Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    coordinator = ExecutionCoordinator(root, manager=manager)
    answer = coordinator.run_focused(
        FocusedQuestion(
            question=question,
            domains=domain or [],
            response_intent=intent,
            principles=context.principles if context else FocusedQuestion(question=question).principles,
            project_context=context,
            conversation_strategy=session.conversation_strategy if session else ConversationStrategy.FRESH,
            conversation_binding=session.conversation_binding if session else None,
        ),
        tunnel_id=tunnel,
        tunnel_policy=tunnel_policy,
    )
    if session and answer.conversation_binding:
        service.bind_session_conversation(project_id, session.session_id, answer.conversation_binding)
    typer.echo(answer.model_dump_json(indent=2))


@clients_app.command("list")
def clients_list() -> None:
    registry = MCPClientRegistry()
    typer.echo(json.dumps([item.__dict__ | {"client": item.client.value} for item in registry.all_status()], indent=2))


@clients_app.command("register")
def clients_register(
    client: Annotated[MCPClientKind, typer.Argument()],
    server_executable: Annotated[Path | None, typer.Option("--server")] = None,
) -> None:
    import shutil
    server = server_executable or Path(shutil.which("fancy-gpt-mcp") or "fancy-gpt-mcp")
    result = MCPClientRegistry().register(client, server)
    typer.echo(json.dumps(result.__dict__ | {"client": result.client.value}, indent=2))
    if result.installed and not result.linked:
        raise typer.Exit(code=1)


@clients_app.command("register-detected")
def clients_register_detected(
    server_executable: Annotated[Path | None, typer.Option("--server")] = None,
) -> None:
    import shutil
    server = server_executable or Path(shutil.which("fancy-gpt-mcp") or "fancy-gpt-mcp")
    results = MCPClientRegistry().register_detected(server)
    typer.echo(json.dumps([item.__dict__ | {"client": item.client.value} for item in results], indent=2))
    if any(item.installed and not item.linked for item in results):
        raise typer.Exit(code=1)


@app.command("mcp")
def mcp_cmd() -> None:
    """Run the stdio MCP server."""
    from .mcp_server import main
    main()


@app.command()
def prepare(
    request_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    skill: Annotated[str | None, typer.Option("--skill")] = None,
    workflow: Annotated[str | None, typer.Option("--workflow")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
    allowed_root: Annotated[list[Path] | None, typer.Option("--allowed-root")] = None,
    open_browser: Annotated[bool, typer.Option("--open-browser")] = False,
) -> None:
    skill, workflow = _route_args(skill, workflow)
    request = RawRequest.model_validate(load_mapping(request_file))
    result = _engine(workdir, allowed_root).prepare(
        request, skill_name=skill, workflow_name=workflow, open_browser=open_browser
    )
    typer.echo(result.model_dump_json(indent=2))


@app.command("run-auto")
def run_auto(
    request_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    skill: Annotated[str | None, typer.Option("--skill")] = None,
    workflow: Annotated[str | None, typer.Option("--workflow")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
    allowed_root: Annotated[list[Path] | None, typer.Option("--allowed-root")] = None,
    profile_dir: Annotated[Path, typer.Option("--profile-dir")] = default_browser_profile(),
    timeout_s: Annotated[float, typer.Option("--timeout")] = 300.0,
    headless: Annotated[bool, typer.Option("--headless")] = False,
    session_id: Annotated[str | None, typer.Option("--session")] = None,
    chat_id: Annotated[str | None, typer.Option("--chat")] = None,
    chat_policy: Annotated[ChatPolicy | None, typer.Option("--chat-policy")] = None,
) -> None:
    skill, workflow = _route_args(skill, workflow)
    request = RawRequest.model_validate(load_mapping(request_file))
    request = request.model_copy(update={
        **({"session_id": session_id} if session_id else {}),
        **({"chat_id": chat_id} if chat_id else {}),
        **({"chat_policy": chat_policy} if chat_policy else {}),
    })
    provider = ChatGPTWebAutomationProvider(PlaywrightChatGPTDriver(profile_dir, headless=headless), timeout_s=timeout_s)
    result = _engine(workdir, allowed_root).run_automatic(
        request, provider, skill_name=skill, workflow_name=workflow
    )
    typer.echo(result.model_dump_json(indent=2))


@app.command("simulate-auto")
def simulate_auto(
    request_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    planner_response: Annotated[Path, typer.Option("--planner-response", exists=True, dir_okay=False)],
    final_response: Annotated[Path, typer.Option("--final-response", exists=True, dir_okay=False)],
    skill: Annotated[str | None, typer.Option("--skill")] = None,
    workflow: Annotated[str | None, typer.Option("--workflow")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
    allowed_root: Annotated[list[Path] | None, typer.Option("--allowed-root")] = None,
) -> None:
    skill, workflow = _route_args(skill, workflow)
    request = RawRequest.model_validate(load_mapping(request_file))
    planner_text = planner_response.read_text(encoding="utf-8")
    final_template = final_response.read_text(encoding="utf-8")
    driver = FakeBrowserDriver([
        planner_text,
        lambda turn: final_template.replace("__REQUEST_ID__", turn.request_id),
    ])
    provider = ChatGPTWebAutomationProvider(driver)
    result = _engine(workdir, allowed_root).run_automatic(
        request, provider, skill_name=skill, workflow_name=workflow
    )
    typer.echo(result.model_dump_json(indent=2))


@app.command("planner-import")
def planner_import(
    request_id: str,
    result_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
    allowed_root: Annotated[list[Path] | None, typer.Option("--allowed-root")] = None,
    open_browser: Annotated[bool, typer.Option("--open-browser")] = False,
) -> None:
    result = _engine(workdir, allowed_root).submit_planner(request_id, load_mapping(result_file), open_browser=open_browser)
    typer.echo(result.model_dump_json(indent=2))


@app.command("final-import")
def final_import(
    request_id: str,
    result_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    result = _engine(workdir).submit_final(request_id, load_mapping(result_file))
    typer.echo(result.model_dump_json(indent=2))


@app.command()
def status(request_id: str, workdir: Annotated[Path | None, typer.Option("--workdir")] = None) -> None:
    typer.echo(_engine(workdir).status(request_id).model_dump_json(indent=2))


@sessions_app.command("create")
def sessions_create(
    repo_root: Annotated[Path, typer.Argument(exists=True, file_okay=False)] = Path("."),
    title: Annotated[str | None, typer.Option("--title")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    typer.echo(_engine(workdir, [repo_root]).create_session(str(repo_root), title).model_dump_json(indent=2))


@sessions_app.command("list")
def sessions_list(workdir: Annotated[Path | None, typer.Option("--workdir")] = None) -> None:
    sessions = _engine(workdir).list_sessions()
    typer.echo(_table(
        ["SESSION", "TITLE", "CHATS", "ACTIVE", "STATE"],
        [[s.session_id, s.title, str(len(s.chat_ids)), s.active_chat_id or "-", "closed" if s.closed_at else "active"] for s in sessions],
        max_widths=[None, 36, None, None, None],
    ))


@sessions_app.command("show")
def sessions_show(
    session_id: str,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    typer.echo(_engine(workdir).get_session(session_id).model_dump_json(indent=2))


@sessions_app.command("inspect")
def sessions_inspect(
    session_id: str,
    json_output: Annotated[bool, typer.Option("--json", help="Emit stable machine-readable JSON.")] = False,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    inspection = _engine(workdir).inspect_session(session_id)
    if json_output:
        typer.echo(inspection.model_dump_json(indent=2))
    else:
        typer.echo(_session_inspection_text(inspection))


@sessions_app.command("close")
def sessions_close(
    session_id: str,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    typer.echo(_engine(workdir).close_session(session_id).model_dump_json(indent=2))


@chats_app.command("create")
def chats_create(
    session_id: str,
    title: Annotated[str, typer.Option("--title")],
    independent: Annotated[bool, typer.Option("--independent")] = False,
    no_select: Annotated[bool, typer.Option("--no-select")] = False,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    chat = _engine(workdir).create_chat(
        session_id, title, independent=independent, make_active=not no_select
    )
    typer.echo(chat.model_dump_json(indent=2))


@chats_app.command("list")
def chats_list(
    session_id: str,
    include_archived: Annotated[bool, typer.Option("--all")] = False,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    engine = _engine(workdir)
    session = engine.get_session(session_id)
    chats = engine.list_chats(session_id, include_archived=include_archived)
    typer.echo(_table(
        ["CHAT", "KIND", "ACTIVE", "REQUESTS", "TITLE"],
        [[c.chat_id, c.kind.value, "yes" if c.chat_id == session.active_chat_id else "", str(len(c.request_ids)), c.title] for c in chats],
        max_widths=[None, None, None, None, 42],
    ))


@chats_app.command("select")
def chats_select(
    session_id: str,
    chat_id: str,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    typer.echo(_engine(workdir).select_chat(session_id, chat_id).model_dump_json(indent=2))


@chats_app.command("archive")
def chats_archive(
    session_id: str,
    chat_id: str,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    typer.echo(_engine(workdir).archive_chat(session_id, chat_id).model_dump_json(indent=2))


@app.command("show-prompt")
def show_prompt(request_id: str, stage: str, workdir: Annotated[Path | None, typer.Option("--workdir")] = None) -> None:
    typer.echo(_engine(workdir).read_prompt(request_id, stage))


@app.command()
def doctor() -> None:
    checks = {
        "python": True,
        "domains": len(load_domains()),
        "skills": len(load_skills()),
        "workflows": len(load_workflows()),
        "chatgpt_web_interactive": True,
        "chatgpt_web_automation_code": True,
        "browser_contract_mock_testable": True,
        "real_web_verified": False,
        "private_chatgpt_api": False,
        "codex_dependency": False,
        "openai_api_dependency": False,
        "cookie_import": False,
        "version": __version__,
        "user_data_dir": str(user_data_dir()),
        "browser_profile": str(default_browser_profile()),
        "install_once_cli": True,
        "standalone_self_test": True,
    }
    typer.echo(json.dumps(checks, indent=2))


@browser_app.command("login")
def browser_login(
    profile_dir: Annotated[Path, typer.Option("--profile-dir")] = default_browser_profile(),
    timeout_s: Annotated[float, typer.Option("--timeout")] = 300.0,
) -> None:
    driver = PlaywrightChatGPTDriver(profile_dir, headless=False)
    try:
        driver.start()
        driver.login(timeout_s=timeout_s)
        typer.echo(json.dumps({"ok": True, "authenticated": True, "profile_dir": str(profile_dir)}, indent=2))
    finally:
        driver.stop()


@browser_app.command("check")
def browser_check(
    profile_dir: Annotated[Path, typer.Option("--profile-dir")] = default_browser_profile(),
    headless: Annotated[bool, typer.Option("--headless")] = False,
) -> None:
    driver = PlaywrightChatGPTDriver(profile_dir, headless=headless)
    try:
        driver.start()
        driver.health_check()
        typer.echo(json.dumps({"ok": True, "driver": driver.name, "profile_dir": str(profile_dir)}, indent=2))
    finally:
        driver.stop()


@catalogs_app.command("skills")
def catalog_skills() -> None:
    typer.echo(json.dumps([item.model_dump(mode="json") for item in load_skills().values()], indent=2, ensure_ascii=False))


@catalogs_app.command("workflows")
def catalog_workflows() -> None:
    typer.echo(json.dumps([item.model_dump(mode="json") for item in load_workflows().values()], indent=2, ensure_ascii=False))


@catalogs_app.command("domains")
def catalog_domains() -> None:
    typer.echo(json.dumps([item.model_dump(mode="json") for item in load_domains().values()], indent=2, ensure_ascii=False))


@catalogs_app.command("route")
def catalog_route(
    request_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    skill: Annotated[str | None, typer.Option("--skill")] = None,
    workflow: Annotated[str | None, typer.Option("--workflow")] = None,
    allowed_root: Annotated[list[Path] | None, typer.Option("--allowed-root")] = None,
) -> None:
    skill, workflow = _route_args(skill, workflow)
    request = RawRequest.model_validate(load_mapping(request_file))
    typer.echo(_engine(None, allowed_root).route(request, skill_name=skill, workflow_name=workflow).model_dump_json(indent=2))


@skills_app.command("validate")
def skills_validate(
    root: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    target = root or packaged_skills_root()
    errors = validate_skill_bundle(target)
    if errors:
        typer.echo(json.dumps({"ok": False, "errors": errors}, indent=2))
        raise typer.Exit(1)
    typer.echo(json.dumps({"ok": True, "skills": len(load_skills()), "root": str(target)}, indent=2))


@skills_app.command("export")
def skills_export(destination: Path) -> None:
    exported = export_packaged_skills(destination)
    typer.echo(json.dumps({"ok": True, "exported": [str(p) for p in exported]}, indent=2))


@validate_app.command("request")
def validate_request(path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    typer.echo(RawRequest.model_validate(load_mapping(path)).model_dump_json(indent=2))


@validate_app.command("manifest")
def validate_manifest(path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    typer.echo(ResearchManifest.model_validate(load_mapping(path)).model_dump_json(indent=2))


@validate_app.command("result")
def validate_result(path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    typer.echo(FinalReport.model_validate(load_mapping(path)).model_dump_json(indent=2))




@tunnels_app.command("components")
def tunnels_components(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show independently testable site/runtime/transport layer contracts."""
    payload = {
        "sites": [item.model_dump(mode="json") for item in SiteRegistry().all()],
        "runtimes": [item.model_dump(mode="json") for item in RuntimeRegistry().all()],
        "transports": [item.model_dump(mode="json") for item in TransportRegistry().all()],
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo("SITES")
    typer.echo(_table(["ID", "HOSTS"], [[s["id"], ", ".join(s.get("hosts", []))] for s in payload["sites"]]))
    typer.echo("\nRUNTIMES")
    typer.echo(_table(["ID", "AUTO", "EXT"], [
        [r["id"], _yn(r.get("automatic")), _yn(r.get("requires_extension"))] for r in payload["runtimes"]
    ]))
    typer.echo("\nTRANSPORTS")
    typer.echo(_table(["ID", "REMOTE", "BRIDGE"], [
        [t["id"], _yn(t.get("remote_capable")), _yn(t.get("requires_bridge"))] for t in payload["transports"]
    ]))


@tunnels_app.command("explain")
def tunnels_explain(
    tunnel_id: Annotated[str, typer.Argument()],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Explain one tunnel composition and health of every layer."""
    manager = TunnelManager()
    spec = manager.registry.get(tunnel_id)
    layers = TunnelLayerInspector().inspect(spec)
    health = manager.inspect(spec)
    if json_output:
        typer.echo(json.dumps({
            "spec": spec.model_dump(mode="json"),
            "layers": [item.model_dump(mode="json") for item in layers],
            "health": health.model_dump(mode="json"),
        }, indent=2))
        return
    typer.echo(f"{spec.id}  ({spec.runtime.value}/{spec.transport.value}, browser={spec.browser})")
    typer.echo(spec.description)
    typer.echo("")
    typer.echo(_table(
        ["LAYER", "COMPONENT", "STATE", "DETAIL"],
        [[item.layer.value, item.component, item.state, item.detail] for item in layers],
        max_widths=[None, None, None, 50],
    ))
    typer.echo("")
    typer.echo(f"health: {health.state.value} — {health.detail}")
    if health.latency_ms is not None:
        typer.echo(f"latency: {health.latency_ms:.1f}ms")
    if health.metadata.get("bridge"):
        typer.echo(f"bridge: {_bridge_summary(health.metadata)}")


@tunnels_app.command("list")
def tunnels_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    manager = TunnelManager()
    specs = manager.registry.all()
    if json_output:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in specs], indent=2))
        return
    typer.echo(_table(["ID", "RUNTIME", "TRANSPORT", "BROWSER", "AUTO"], [
        [s.id, s.runtime.value, s.transport.value, s.browser, _yn(s.capabilities.automatic)]
        for s in specs
        if s.enabled
    ]))


@tunnels_app.command("health")
def tunnels_health(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    manager = TunnelManager()
    healths = manager.health()
    if json_output:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in healths], indent=2))
        return
    typer.echo(_table(
        ["ID", "STATE", "CONN", "MS", "DETAIL"],
        [
            [
                h.tunnel_id,
                h.state.value,
                _yn(h.browser_connected),
                "-" if h.latency_ms is None else f"{h.latency_ms:.0f}",
                h.detail,
            ]
            for h in healths
        ],
        max_widths=[None, None, None, None, 40],
    ))
    typer.echo("\n(run 'tunnels explain <id>' for layer/bridge detail)")


@tunnels_app.command("select")
def tunnels_select(
    tunnel: Annotated[str | None, typer.Option("--tunnel")] = None,
    policy: Annotated[str, typer.Option("--policy")] = "auto",
) -> None:
    manager = TunnelManager()
    selection = manager.select(tunnel_id=tunnel, policy=policy)
    typer.echo(selection.model_dump_json(indent=2))


@bridge_app.command("init")
def bridge_init() -> None:
    token = load_or_create_token(default_bridge_token_file())
    typer.echo(json.dumps({
        "endpoint": os.getenv("FANCY_GPT_BRIDGE_ENDPOINT", "ws://127.0.0.1:8765"),
        "token_file": str(default_bridge_token_file()),
        "pair_token": token,
    }, indent=2))


@bridge_app.command("serve")
def bridge_serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8765,
    allow_non_loopback: Annotated[bool, typer.Option("--allow-non-loopback")] = False,
) -> None:
    token = load_or_create_token(default_bridge_token_file())
    typer.echo(json.dumps({"listening": f"ws://{host}:{port}", "token_file": str(default_bridge_token_file())}))
    BridgeServer(host, port, token, allow_non_loopback=allow_non_loopback).serve_forever()


@extension_app.command("export")
def extension_export(
    browser: Annotated[str, typer.Argument()],
    destination: Annotated[Path, typer.Argument()],
) -> None:
    target = export_extension(browser, destination)
    typer.echo(json.dumps({"ok": True, "browser": browser, "path": str(target)}, indent=2))


@extension_app.command("export-all")
def extension_export_all(
    destination: Annotated[Path, typer.Argument()],
) -> None:
    """Export every browser bundle at once, each into its own subdirectory.

    Exporting one browser at a time is how a stale bundle survives: the browser
    you did not re-export keeps running yesterday's adapter.
    """
    exported = []
    for browser in ("chrome", "edge", "firefox"):
        target = export_extension(browser, destination / browser)
        exported.append({"browser": browser, "path": str(target)})
    typer.echo(json.dumps({
        "ok": True,
        "adapter_build": adapter_build_id(),
        "exported": exported,
    }, indent=2))


@extension_app.command("native-config")
def extension_native_config(
    browser: Annotated[str, typer.Option("--browser")] = "chrome",
    tunnel_id: Annotated[str | None, typer.Option("--tunnel")] = None,
    endpoint: Annotated[str, typer.Option("--endpoint")] = "ws://127.0.0.1:8765",
) -> None:
    if browser not in {"chrome", "edge", "firefox"}:
        raise typer.BadParameter("--browser must be chrome, edge, or firefox")
    selected_tunnel = tunnel_id or f"{browser}-extension-native-local"
    token = load_or_create_token(default_bridge_token_file())
    path = default_bridge_native_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "endpoint": endpoint, "token": token, "tunnel_ids": [selected_tunnel], "browser": browser
    }, indent=2) + "\n", encoding="utf-8")
    typer.echo(str(path))


@extension_app.command("native-manifest")
def extension_native_manifest(
    browser: Annotated[str, typer.Option("--browser")],
    extension_id: Annotated[str, typer.Option("--extension-id")],
    destination: Annotated[Path | None, typer.Option("--destination")] = None,
) -> None:
    import shutil
    executable = Path(shutil.which("fancy-gpt-native-host") or "fancy-gpt-native-host")
    if destination is not None:
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / "com.fancygpt.bridge.json"
        target.write_text(json.dumps(native_host_manifest(browser, extension_id, executable), indent=2) + "\n", encoding="utf-8")
    else:
        target = install_manifest(browser, extension_id, executable)
    typer.echo(str(target))


@execution_app.command("status")
def execution_status(
    execution_id: Annotated[str, typer.Argument()],
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    root = workdir or Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    typer.echo(ExecutionStore(root).load(execution_id).model_dump_json(indent=2))


@execution_app.command("recent")
def execution_recent(
    limit: Annotated[int, typer.Option("--limit")] = 20,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    root = workdir or Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    typer.echo(json.dumps([item.model_dump(mode="json") for item in ExecutionStore(root).list_recent(limit)], indent=2))


@project_app.command("init")
def project_init(
    name: Annotated[str, typer.Argument()],
    target: Annotated[str, typer.Option("--target")],
    repo_root: Annotated[str, typer.Option("--repo-root")] = ".",
    acceptance: Annotated[list[str] | None, typer.Option("--acceptance")] = None,
    project_id: Annotated[str | None, typer.Option("--id")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    criteria = [
        AcceptanceCriterion(id=f"ac-{index + 1}", statement=statement)
        for index, statement in enumerate(acceptance or [])
    ]
    project = _project_service(workdir).create_project(
        project_id=project_id, name=name, target=target, repo_root=repo_root, acceptance=criteria
    )
    typer.echo(project.model_dump_json(indent=2))


@project_app.command("bootstrap")
def project_bootstrap(
    project_id: Annotated[str, typer.Argument()],
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    service = _project_service(workdir)
    ids = TeamOrchestrator(service).bootstrap_developer_cycle(project_id)
    typer.echo(json.dumps({"project_id": project_id, "work_item_ids": ids}, indent=2))


def _context_requirement(
    patterns: list[str] | None, paths: list[str] | None, terms: list[str] | None, required: bool
) -> list[LocalContextRequirement]:
    if not (patterns or paths or terms):
        return []
    return [
        LocalContextRequirement(
            id="LC1",
            description="operator-declared work-item context",
            patterns=list(patterns or []),
            exact_paths=list(paths or []),
            search_terms=list(terms or []),
            priority=Priority.P0 if required else Priority.P1,
            required=required,
        )
    ]


@project_app.command("work-item")
def project_work_item(
    project_id: Annotated[str, typer.Argument()],
    title: Annotated[str, typer.Option("--title")],
    objective: Annotated[str, typer.Option("--objective")],
    role: Annotated[AgentRole, typer.Option("--role")],
    depends_on: Annotated[list[str] | None, typer.Option("--depends-on")] = None,
    external: Annotated[bool, typer.Option("--external")] = False,
    pattern: Annotated[list[str] | None, typer.Option("--pattern")] = None,
    path: Annotated[list[str] | None, typer.Option("--path")] = None,
    search: Annotated[list[str] | None, typer.Option("--search")] = None,
    required_context: Annotated[bool, typer.Option("--required-context")] = False,
    thread: Annotated[str | None, typer.Option("--thread")] = None,
    site: Annotated[str | None, typer.Option("--site")] = None,
    strategy: Annotated[ConversationStrategy | None, typer.Option("--strategy")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    """Add one work item, optionally declaring the repository source it needs.

    Work items given the same --thread share one ChatGPT conversation, so several
    requests continue in the same chat instead of each opening a new one.
    """
    item = _project_service(workdir).add_work_item(
        project_id,
        title=title,
        objective=objective,
        role=role,
        execution_mode=WorkExecutionMode.EXTERNAL_AGENT if external else WorkExecutionMode.MODEL,
        dependencies=list(depends_on or []),
        context_requirements=_context_requirement(pattern, path, search, required_context),
        conversation_key=thread,
        conversation_strategy=ConversationStrategy.RESUME if thread and strategy is None else strategy,
        site=site,
    )
    typer.echo(item.model_dump_json(indent=2))


@project_app.command("context-requirements")
def project_context_requirements(
    project_id: Annotated[str, typer.Argument()],
    work_item_id: Annotated[str, typer.Argument()],
    pattern: Annotated[list[str] | None, typer.Option("--pattern")] = None,
    path: Annotated[list[str] | None, typer.Option("--path")] = None,
    search: Annotated[list[str] | None, typer.Option("--search")] = None,
    required_context: Annotated[bool, typer.Option("--required-context")] = False,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    """Point an existing work item at the repository source it must read.

    Passing no selector clears the declaration, so the item reads nothing.
    """
    item = _project_service(workdir).set_context_requirements(
        project_id, work_item_id, _context_requirement(pattern, path, search, required_context)
    )
    typer.echo(item.model_dump_json(indent=2))


@conversations_app.command("list")
def conversations_list(
    json_output: Annotated[bool, typer.Option("--json")] = False,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    """List every ChatGPT thread this runtime opened, with a link to each.

    This is built from local records only. FancyGPT holds no ChatGPT account
    credentials and calls no private endpoint, so it can account for the threads
    it opened itself but cannot enumerate -- or delete -- everything in your
    ChatGPT history. Use the links to open one and manage it there.
    """
    root = workdir or Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    items = ConversationIndex(root).all()
    if json_output:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in items], indent=2))
        return
    if not items:
        typer.echo("No ChatGPT thread has been opened from this workdir yet.")
        typer.echo("Threads appear here once a turn runs with a persistent conversation;")
        typer.echo("a temporary chat is never saved and so is never listed.")
        return
    typer.echo(_table(
        ["ORIGIN", "OWNER", "LABEL", "THREAD", "TURNS", "URL"],
        [
            [item.origin, item.owner, item.label, item.thread_key or "-", str(item.turns), item.url]
            for item in items
        ],
        max_widths=[None, None, 14, 10, None, None],
    ))


@project_app.command("checks")
def project_checks(
    name: Annotated[str | None, typer.Argument()] = None,
    argv: Annotated[list[str] | None, typer.Option("--argv")] = None,
    remove: Annotated[bool, typer.Option("--remove")] = False,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    """List, add or remove the verification commands teammates may request by name."""
    root = workdir or Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    checks = load_verification_checks(root)
    if name and remove:
        checks.pop(name, None)
        save_verification_checks(root, checks)
    elif name and argv:
        checks[name] = list(argv)
        save_verification_checks(root, checks)
    elif name:
        raise typer.BadParameter("pass --argv to define the check, or --remove to delete it")
    typer.echo(json.dumps(checks, indent=2, sort_keys=True))


@project_app.command("status")
def project_status(
    project_id: Annotated[str, typer.Argument()],
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    typer.echo(_project_service(workdir).snapshot(project_id).model_dump_json(indent=2))


@project_app.command("retry")
def project_retry(
    project_id: Annotated[str, typer.Argument()],
    work_item_id: Annotated[str, typer.Argument()],
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    """Return a failed work item to the ready pool after fixing its cause."""
    item = _project_service(workdir).retry_work_item(project_id, work_item_id)
    typer.echo(item.model_dump_json(indent=2))


@project_app.command("continue")
def project_continue(
    project_id: Annotated[str, typer.Argument()],
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    service = _project_service(workdir)
    typer.echo(TeamOrchestrator(service).continue_project(project_id).model_dump_json(indent=2))


@project_app.command("run-next")
def project_run_next(
    project_id: Annotated[str, typer.Argument()],
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
    tunnel: Annotated[str | None, typer.Option("--tunnel")] = None,
    tunnel_policy: Annotated[str, typer.Option("--tunnel-policy")] = "auto",
    timeout_s: Annotated[float, typer.Option("--timeout")] = 300.0,
) -> None:
    """Execute one ready team work item or return an external-agent assignment."""
    service = _project_service(workdir)
    manager = TunnelManager(timeout_s=timeout_s)
    result = ProjectRunner(service, manager=manager).run_next(
        project_id, tunnel_id=tunnel, tunnel_policy=tunnel_policy
    )
    typer.echo(result.model_dump_json(indent=2))


@project_app.command("run")
def project_run(
    project_id: Annotated[str, typer.Argument()],
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
    tunnel: Annotated[str | None, typer.Option("--tunnel")] = None,
    tunnel_policy: Annotated[str, typer.Option("--tunnel-policy")] = "auto",
    max_steps: Annotated[int, typer.Option("--max-steps")] = 8,
    timeout_s: Annotated[float, typer.Option("--timeout")] = 300.0,
) -> None:
    """Run model-backed teammates until completion, blockage, or an external-agent handoff."""
    service = _project_service(workdir)
    manager = TunnelManager(timeout_s=timeout_s)
    results = ProjectRunner(service, manager=manager).run_until_pause(
        project_id, tunnel_id=tunnel, tunnel_policy=tunnel_policy, max_steps=max_steps
    )
    typer.echo(json.dumps([item.model_dump(mode="json") for item in results], indent=2))


@project_app.command("finding")
def project_finding(
    project_id: Annotated[str, typer.Argument()],
    claim: Annotated[str, typer.Option("--claim")],
    impact: Annotated[str, typer.Option("--impact")],
    severity: Annotated[str, typer.Option("--severity")] = "medium",
    action: Annotated[str | None, typer.Option("--action")] = None,
    session_id: Annotated[str | None, typer.Option("--session")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    result = _project_service(workdir).record_finding(
        project_id, claim=claim, impact=impact, severity=severity,
        required_action=action, source_session_id=session_id
    )
    typer.echo(result.model_dump_json(indent=2))


@project_app.command("resolve-finding")
def project_resolve_finding(
    project_id: Annotated[str, typer.Argument()],
    finding_id: Annotated[str, typer.Argument()],
    resolution: Annotated[str, typer.Option("--resolution")],
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    result = _project_service(workdir).resolve_finding(project_id, finding_id, resolution=resolution)
    typer.echo(result.model_dump_json(indent=2))


@project_app.command("artifact")
def project_artifact(
    project_id: Annotated[str, typer.Argument()],
    path: Annotated[str, typer.Option("--path")],
    kind: Annotated[str, typer.Option("--kind")] = "file",
    sha256: Annotated[str | None, typer.Option("--sha256")] = None,
    description: Annotated[str | None, typer.Option("--description")] = None,
    session_id: Annotated[str | None, typer.Option("--session")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    result = _project_service(workdir).record_artifact(
        project_id, path=path, kind=kind, sha256=sha256, description=description,
        source_session_id=session_id
    )
    typer.echo(result.model_dump_json(indent=2))


@project_app.command("sessions")
def project_sessions(
    project_id: Annotated[str, typer.Argument()],
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    snapshot = _project_service(workdir).snapshot(project_id)
    typer.echo(json.dumps([item.model_dump(mode="json") for item in snapshot.sessions], indent=2))


@project_app.command("history")
def project_history(
    project_id: Annotated[str, typer.Argument()],
    limit: Annotated[int, typer.Option("--limit")] = 100,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    events = _project_service(workdir).history(project_id, limit=limit)
    typer.echo(json.dumps([item.model_dump(mode="json") for item in events], indent=2))


@project_app.command("context")
def project_context(
    project_id: Annotated[str, typer.Argument()],
    work_item_id: Annotated[str | None, typer.Option("--work-item")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    typer.echo(_project_service(workdir).relevant_context(project_id, work_item_id).model_dump_json(indent=2))


@project_app.command("assign")
def project_assign(
    project_id: Annotated[str, typer.Argument()],
    work_item_id: Annotated[str, typer.Argument()],
    strategy: Annotated[ConversationStrategy | None, typer.Option("--strategy")] = None,
    binding: Annotated[str | None, typer.Option("--binding")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    assignment = _project_service(workdir).start_assignment(
        project_id, work_item_id, conversation_strategy=strategy, conversation_binding=binding
    )
    typer.echo(assignment.model_dump_json(indent=2))


@project_app.command("start-session")
def project_start_session(
    project_id: Annotated[str, typer.Argument()],
    work_item_id: Annotated[str, typer.Argument()],
    strategy: Annotated[ConversationStrategy | None, typer.Option("--strategy")] = None,
    binding: Annotated[str | None, typer.Option("--binding")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    session = _project_service(workdir).start_session(
        project_id, work_item_id, conversation_strategy=strategy, conversation_binding=binding
    )
    typer.echo(session.model_dump_json(indent=2))


@project_app.command("submit-outcome")
def project_submit_outcome(
    project_id: Annotated[str, typer.Argument()],
    outcome_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    """Persist a structured teammate outcome (for example from Codex/Claude) and close its session."""
    outcome = AgentOutcome.model_validate(load_mapping(outcome_file))
    result = _project_service(workdir).apply_agent_outcome(project_id, outcome)
    typer.echo(result.model_dump_json(indent=2))


@project_app.command("finish-session")
def project_finish_session(
    project_id: Annotated[str, typer.Argument()],
    session_id: Annotated[str, typer.Argument()],
    summary: Annotated[str, typer.Option("--summary")],
    failed: Annotated[bool, typer.Option("--failed")] = False,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    result = _project_service(workdir).finish_session(project_id, session_id, summary=summary, failed=failed)
    typer.echo(result.model_dump_json(indent=2))


@project_app.command("evidence")
def project_evidence(
    project_id: Annotated[str, typer.Argument()],
    claim: Annotated[str, typer.Option("--claim")],
    source: Annotated[str, typer.Option("--source")],
    locator: Annotated[str | None, typer.Option("--locator")] = None,
    session_id: Annotated[str | None, typer.Option("--session")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    result = _project_service(workdir).record_evidence(
        project_id, claim=claim, source=source, locator=locator, source_session_id=session_id
    )
    typer.echo(result.model_dump_json(indent=2))


@project_app.command("criterion")
def project_criterion(
    project_id: Annotated[str, typer.Argument()],
    criterion_id: Annotated[str, typer.Argument()],
    status: Annotated[CriterionStatus, typer.Option("--status")],
    evidence_id: Annotated[list[str] | None, typer.Option("--evidence")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    result = _project_service(workdir).update_criterion(
        project_id, criterion_id, status=status, evidence_ids=evidence_id
    )
    typer.echo(result.model_dump_json(indent=2))


@tunnels_app.command("inspect")
def tunnels_inspect(
    tunnel_id: Annotated[str, typer.Argument()],
    human: Annotated[bool, typer.Option("--human")] = False,
) -> None:
    """Alias with the natural diagnostic name used by MCP and operators.

    Defaults to JSON because its callers are machines; `tunnels explain` stays
    the human-readable view.
    """
    tunnels_explain(tunnel_id, json_output=not human)


if __name__ == "__main__":
    app()
