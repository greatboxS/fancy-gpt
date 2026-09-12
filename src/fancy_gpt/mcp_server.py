from __future__ import annotations

import os
import threading
from pathlib import Path

from mcp.server import MCPServer

from .catalog import load_domains, load_skills, load_workflows
from .engine import ReviewEngine
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
    selection = manager.select(
        tunnel_id=tunnel_id or request.tunnel,
        policy=(tunnel_policy or request.tunnel_policy),
        require_automatic=True,
    )
    provider = manager.provider(selection)
    with _tunnel_lock(selection.tunnel_id):
        return _engine().run_automatic(request, provider, skill_name=skill, workflow_name=workflow)


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
    return manager.probe(manager.registry.get(tunnel_id))


@mcp.tool()
def select_tunnel(
    tunnel_id: str | None = None,
    tunnel_policy: str = "auto",
) -> TunnelSelection:
    """Resolve the tunnel that would be used without running a review."""
    return _manager().select(tunnel_id=tunnel_id, policy=tunnel_policy, require_automatic=True)


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
