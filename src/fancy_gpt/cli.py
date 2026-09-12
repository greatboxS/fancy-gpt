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
from .models import FinalReport, RawRequest, ResearchManifest
from .focused import FocusedAnswerEngine, FocusedQuestion
from .orchestrator import TeamOrchestrator
from .project_models import AcceptanceCriterion, AgentOutcome, AgentRole, ConversationStrategy, CriterionStatus
from .project_service import ProjectService
from .project_runner import ProjectRunner
from .relevance import ResponseIntent
from .providers import ChatGPTWebAutomationProvider
from .skills import export_packaged_skills, packaged_skills_root, validate_skill_bundle
from .runtime_paths import default_browser_profile, user_data_dir, default_bridge_token_file, default_bridge_native_config
from .bridge import BridgeServer, load_or_create_token
from .bridge.native_manifest import install_manifest, native_host_manifest
from .extension_utils import export_extension
from .tunnels import TunnelManager, TunnelRegistry, TunnelLayerInspector
from .web.runtime import RuntimeRegistry
from .web.sites import SiteRegistry
from .web.transport import TransportRegistry
from .self_test import run_self_test
from . import __version__

app = typer.Typer(help="fancy-gpt independent technical reasoning toolkit")
catalogs_app = typer.Typer(help="Inspect skills, workflows, domains, and routing")
validate_app = typer.Typer(help="Validate structured payloads")
browser_app = typer.Typer(help="ChatGPT Web browser automation utilities")
skills_app = typer.Typer(help="Validate/export Agent Skill bundles")
tunnels_app = typer.Typer(help="Inspect, probe, and select browser tunnels")
bridge_app = typer.Typer(help="Run/pair the browser bridge used by extension tunnels")
extension_app = typer.Typer(help="Export/configure Chrome, Edge, and Firefox tunnel extensions")
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
app.add_typer(project_app, name="project")
app.add_typer(execution_app, name="execution")
app.add_typer(clients_app, name="clients")


def _engine(workdir: Path | None, allowed_root: list[Path] | None = None) -> ReviewEngine:
    root = workdir or Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    return ReviewEngine(root, allowed_roots=allowed_root)



def _project_service(workdir: Path | None) -> ProjectService:
    root = workdir or Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    return ProjectService(root)


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
) -> None:
    """Run through a runtime-selected tunnel; skill is inferred from mode by default."""
    request = RawRequest.model_validate(load_mapping(request_file))
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
) -> None:
    skill, workflow = _route_args(skill, workflow)
    request = RawRequest.model_validate(load_mapping(request_file))
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
def tunnels_components() -> None:
    """Show independently testable site/runtime/transport layer contracts."""
    payload = {
        "sites": [item.model_dump(mode="json") for item in SiteRegistry().all()],
        "runtimes": [item.model_dump(mode="json") for item in RuntimeRegistry().all()],
        "transports": [item.model_dump(mode="json") for item in TransportRegistry().all()],
    }
    typer.echo(json.dumps(payload, indent=2))


@tunnels_app.command("explain")
def tunnels_explain(tunnel_id: Annotated[str, typer.Argument()]) -> None:
    """Explain one tunnel composition and health of every layer."""
    manager = TunnelManager()
    spec = manager.registry.get(tunnel_id)
    typer.echo(json.dumps({
        "spec": spec.model_dump(mode="json"),
        "layers": [item.model_dump(mode="json") for item in TunnelLayerInspector().inspect(spec)],
        "health": manager.inspect(spec).model_dump(mode="json"),
    }, indent=2))


@tunnels_app.command("list")
def tunnels_list() -> None:
    manager = TunnelManager()
    typer.echo(json.dumps([item.model_dump(mode="json") for item in manager.registry.all()], indent=2))


@tunnels_app.command("health")
def tunnels_health() -> None:
    manager = TunnelManager()
    typer.echo(json.dumps([item.model_dump(mode="json") for item in manager.health()], indent=2))


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


@project_app.command("status")
def project_status(
    project_id: Annotated[str, typer.Argument()],
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
) -> None:
    typer.echo(_project_service(workdir).snapshot(project_id).model_dump_json(indent=2))


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
def tunnels_inspect(tunnel_id: Annotated[str, typer.Argument()]) -> None:
    """Alias with the natural diagnostic name used by MCP and operators."""
    tunnels_explain(tunnel_id)


if __name__ == "__main__":
    app()
