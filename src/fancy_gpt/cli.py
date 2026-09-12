from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from .browser import FakeBrowserDriver, PlaywrightChatGPTDriver
from .catalog import load_domains, load_skills, load_workflows
from .engine import ReviewEngine
from .io import load_mapping
from .models import ChatPolicy, FinalReport, RawRequest, ResearchManifest
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

app = typer.Typer(help="fancy-gpt independent technical reasoning toolkit")
catalogs_app = typer.Typer(help="Inspect skills, workflows, domains, and routing")
validate_app = typer.Typer(help="Validate structured payloads")
browser_app = typer.Typer(help="ChatGPT Web browser automation utilities")
skills_app = typer.Typer(help="Validate/export Agent Skill bundles")
tunnels_app = typer.Typer(help="Inspect, probe, and select browser tunnels")
bridge_app = typer.Typer(help="Run/pair the browser bridge used by extension tunnels")
extension_app = typer.Typer(help="Export/configure Chrome, Edge, and Firefox tunnel extensions")
sessions_app = typer.Typer(help="Manage persistent FancyGPT work sessions")
chats_app = typer.Typer(help="Manage chats inside a FancyGPT session")
app.add_typer(catalogs_app, name="catalogs")
app.add_typer(validate_app, name="validate")
app.add_typer(browser_app, name="browser")
app.add_typer(skills_app, name="skills")
app.add_typer(tunnels_app, name="tunnels")
app.add_typer(bridge_app, name="bridge")
app.add_typer(extension_app, name="extension")
app.add_typer(sessions_app, name="sessions")
app.add_typer(chats_app, name="chats")


def _engine(workdir: Path | None, allowed_root: list[Path] | None = None) -> ReviewEngine:
    root = workdir or Path(os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    return ReviewEngine(root, allowed_roots=allowed_root)


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
    selection = manager.select(tunnel_id=tunnel or request.tunnel, policy=(tunnel_policy or request.tunnel_policy))
    provider = manager.provider(selection)
    result = _engine(workdir, allowed_root).run_automatic(request, provider, skill_name=skill, workflow_name=workflow)
    typer.echo(result.model_dump_json(indent=2))


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
    health = manager.probe(spec)
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


if __name__ == "__main__":
    app()
