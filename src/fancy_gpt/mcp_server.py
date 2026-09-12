from __future__ import annotations

import os
import threading
from pathlib import Path

from mcp.server import MCPServer

from .catalog import load_domains, load_skills, load_workflows
from .engine import ReviewEngine
from .models import (
    ChatRecord,
    ContextPack,
    FinalReport,
    InteractionRequired,
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
    selection = manager.select(
        tunnel_id=tunnel_id or request.tunnel,
        policy=(tunnel_policy or request.tunnel_policy),
        require_automatic=True,
    )
    provider = manager.provider(selection)
    with _tunnel_lock(selection.tunnel_id):
        return _engine().run_automatic(request, provider, skill_name=skill, workflow_name=workflow)


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
@tracked("list_tunnel_sites")
def list_tunnel_sites() -> list[SiteContract]:
    """List site-adapter contracts available for tunnel composition."""
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
    """Inspect static site/runtime/transport/composition health independently."""
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
    return manager.probe(manager.registry.get(tunnel_id))


@mcp.tool()
@tracked("select_tunnel")
def select_tunnel(
    tunnel_id: str | None = None,
    tunnel_policy: str = "auto",
) -> TunnelSelection:
    """Resolve the tunnel that would be used without running a review."""
    return _manager().select(tunnel_id=tunnel_id, policy=tunnel_policy, require_automatic=True)


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
