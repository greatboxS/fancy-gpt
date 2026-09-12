from __future__ import annotations

from pathlib import Path

import pytest

from fancy_gpt.bridge import BridgeBrowserDriver
from fancy_gpt.extension_utils import export_extension
from fancy_gpt.tunnels import TunnelRegistry
from fancy_gpt.tunnels.factory import TunnelDriverFactory
from fancy_gpt.web.sites import SiteRegistry


def test_a_tunnel_is_not_bound_to_one_site(tmp_path: Path) -> None:
    # A tunnel says how a browser is reached; which model answers is chosen per
    # request. Tying them together would multiply browsers by sites into ids.
    from fancy_gpt.browser import FakeBrowserDriver
    from fancy_gpt.models import ModelRequest
    from fancy_gpt.providers import ChatGPTWebAutomationProvider

    driver = FakeBrowserDriver(['{"ok": true}'])
    provider = ChatGPTWebAutomationProvider(driver)
    request = ModelRequest(
        request_id="r1", stage="agent", title="t", prompt="p",
        response_schema={}, metadata={"site": "gemini"},
    )
    provider.start()
    try:
        provider.execute(request)
    finally:
        provider.stop()
    assert driver.turns[-1].site == "gemini"


def test_review_request_site_reaches_planner_and_final_model_requests(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from fancy_gpt.models import RawRequest
    from fancy_gpt.request_builder import FinalRequestBuilder, PlannerRequestBuilder

    request = RawRequest(mode="review", objective="review this", site="gemini")
    route = SimpleNamespace(route_kind="workflow", route_name="standard-review", primary_skill="review-code")
    prompt_builder = SimpleNamespace(build_prompt=lambda *args: "prompt")
    planner = PlannerRequestBuilder(planner=prompt_builder).build("request-1", request, route)
    final = FinalRequestBuilder(compiler=prompt_builder).build(
        "request-1", request, route, SimpleNamespace(), SimpleNamespace(context_hash="hash"),
        SimpleNamespace(policy=SimpleNamespace(value="temporary"), conversation_id=None),
    )

    assert planner.metadata["site"] == "gemini"
    assert final.metadata["site"] == "gemini"


def test_the_job_carries_the_requested_site(tmp_path: Path) -> None:
    # The bridge driver carries only the site explicitly attached to the turn;
    # it never derives one from the tunnel.
    from fancy_gpt.bridge import BridgeBrowserDriver

    driver = BridgeBrowserDriver("ws://127.0.0.1:1", "token", "edge-remote")
    assert driver.begin_turn(request_id="r", stage="agent").site is None
    assert driver.begin_turn(request_id="r", stage="agent", site="gemini").site == "gemini"


def test_site_contracts_do_not_share_a_host() -> None:
    seen: dict[str, str] = {}
    for site in SiteRegistry().all():
        for host in site.hosts:
            assert host not in seen, f"{host} claimed by both {seen.get(host)} and {site.id}"
            seen[host] = site.id


def test_exported_extension_carries_an_adapter_for_every_site(tmp_path: Path) -> None:
    exported = export_extension("edge", tmp_path / "edge")
    for site in SiteRegistry().all():
        adapter = exported / f"site_{site.id}.js"
        assert adapter.is_file(), f"no browser adapter shipped for site {site.id}"
        source = adapter.read_text(encoding="utf-8")
        assert f"FancyGPTSites.{site.id}" in source
        for host in site.hosts:
            assert host in source


def test_runtime_layer_knows_where_to_open_each_site(tmp_path: Path) -> None:
    background = (export_extension("edge", tmp_path / "edge-bg") / "background.js").read_text(encoding="utf-8")
    for site in SiteRegistry().all():
        assert f"  {site.id}: {{" in background, f"background.js has no URL policy for {site.id}"
        for host in site.hosts:
            assert host in background


def test_one_browser_registers_one_browser_tunnel(tmp_path: Path) -> None:
    # A single browser tunnel can drive every site for which it ships an adapter.
    background = (export_extension("edge", tmp_path / "edge-ids") / "background.js").read_text(encoding="utf-8")
    assert "function tunnelIdsFor(config)" not in background
    assert "tunnelSuffix" not in background

    transport = (export_extension("edge", tmp_path / "edge-ids") / "bridge_transport.js").read_text(encoding="utf-8")
    assert "tunnel_ids: [config.tunnelId]" in transport


@pytest.mark.parametrize("site_id", ["chatgpt", "gemini"])
def test_adapter_uses_the_shared_input_path(tmp_path: Path, site_id: str) -> None:
    # Writing straight to textContent is what broke ChatGPT on a loaded page; a
    # new adapter must not have to rediscover that.
    exported = export_extension("edge", tmp_path / f"edge-{site_id}")
    source = (exported / f"site_{site_id}.js").read_text(encoding="utf-8")
    kit = (exported / "site_kit.js").read_text(encoding="utf-8")
    assert 'execCommand("insertText"' in kit
    assert "setComposer" in source
    assert "composer.textContent = prompt" not in source, "the adapter must use the kit, not its own write"


def test_gemini_extracts_code_without_the_visual_language_label(tmp_path: Path) -> None:
    source = (export_extension("edge", tmp_path / "edge-gemini-code") / "site_gemini.js").read_text(encoding="utf-8")
    assert 'content.querySelectorAll("code")' in source
    assert "codeBlocks.length === 1" in source


def test_a_binding_is_never_reused_across_sites(tmp_path: Path) -> None:
    # A conversation id only means something on the site that issued it; opening
    # a ChatGPT thread id on Gemini navigates to a URL that does not exist.
    from fancy_gpt.project_models import AgentRole, ConversationStrategy
    from fancy_gpt.project_service import ProjectService

    service = ProjectService(tmp_path)
    project = service.create_project(name="p", target="ship it", repo_root=str(tmp_path))

    on_chatgpt = service.add_work_item(
        project.project_id, title="Ask", objective="ask", role=AgentRole.RESEARCHER, site="chatgpt"
    )
    first = service.start_assignment(project.project_id, on_chatgpt.work_item_id)
    service.bind_session_conversation(project.project_id, first.session.session_id, "6aa52775-9f48-83ec-a24e-c8e42")
    service.finish_session(project.project_id, first.session.session_id, summary="done")

    on_gemini = service.add_work_item(
        project.project_id, title="Ask again", objective="ask", role=AgentRole.RESEARCHER,
        site="gemini", conversation_strategy=ConversationStrategy.RESUME,
    )
    second = service.start_assignment(project.project_id, on_gemini.work_item_id)
    assert second.session.site == "gemini"
    # Gemini must not inherit the ChatGPT thread; it opens one of its own.
    assert second.session.conversation_binding != "6aa52775-9f48-83ec-a24e-c8e42"

    # The same site does resume its own thread.
    again = service.add_work_item(
        project.project_id, title="Ask once more", objective="ask", role=AgentRole.RESEARCHER,
        site="chatgpt", conversation_strategy=ConversationStrategy.RESUME,
    )
    third = service.start_assignment(project.project_id, again.work_item_id)
    assert third.session.conversation_binding == "6aa52775-9f48-83ec-a24e-c8e42"


def test_review_chat_cannot_reuse_a_conversation_on_another_site(tmp_path: Path) -> None:
    from fancy_gpt.conversations import ConversationManager
    from fancy_gpt.models import RawRequest
    from fancy_gpt.store import SessionStore

    manager = ConversationManager(SessionStore(tmp_path))
    first = manager.resolve(RawRequest(mode="review", objective="first", repo_root=str(tmp_path), site="chatgpt"))
    manager.bind_conversation(first, "chatgpt-conversation", "edge-remote")

    with pytest.raises(ValueError, match="use a new chat for gemini"):
        manager.resolve(RawRequest(mode="review", objective="second", repo_root=str(tmp_path), site="gemini"))
