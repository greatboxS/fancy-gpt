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


def test_gemini_waits_for_existing_history_before_continuing(tmp_path: Path) -> None:
    exported = export_extension("edge", tmp_path / "edge-gemini-continuation")
    background = (exported / "background.js").read_text(encoding="utf-8")
    content = (exported / "content.js").read_text(encoding="utf-8")
    gemini = (exported / "site_gemini.js").read_text(encoding="utf-8")

    assert 'continuing: job.conversation?.mode === "continue"' in background
    assert "continuing: Boolean(message.continuing)," in content
    assert "await settledResponseCount(Boolean(options.continuing))" in gemini
    assert '"Gemini conversation history did not load"' in gemini


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


def test_cancellation_is_wired_through_the_exported_extension(tmp_path: Path) -> None:
    """Stopping a turn must reach the page, not just the background script."""
    exported = export_extension("edge", tmp_path / "edge-cancel")
    background = (exported / "background.js").read_text(encoding="utf-8")
    content = (exported / "content.js").read_text(encoding="utf-8")
    kit = (exported / "site_kit.js").read_text(encoding="utf-8")

    # The controller's cancel is dispatched, not silently ignored.
    assert 'message.type === "cancel"' in background
    assert "async function cancelJob(" in background
    # A cancel naming a different generation is refused, with a reason.
    assert "cancel names a different generation" in background
    # Cancellation is reported distinctly from a normal answer.
    assert '"job_cancelled"' in background
    # Cancelling while tabs.create() is awaiting must survive setup. Replacing
    # the map entry after that await used to erase the cancellation after it had
    # already been acknowledged.
    assert "const entry = {tabId: null" in background
    assert "entry.tabId = lease.tabId" in background
    after_tab_creation = background.index("entry.tabId = lease.tabId")
    assert background.index("if (entry.cancelled)", after_tab_creation) < background.index(
        'type: "fancy_execute_turn"', after_tab_creation
    )
    # Control replies are correlated independently from the job's terminal reply.
    assert "control_id: message.control_id" in background

    assert '"fancy_cancel_turn"' in content
    assert "isCancelled:" in content

    # The stop control is the site's own, exposed once for every adapter.
    assert "function stopGeneration(" in kit


@pytest.mark.parametrize("adapter", ["site_chatgpt.js", "site_gemini.js"])
def test_each_adapter_honours_cancellation_in_its_poll_loop(tmp_path: Path, adapter: str) -> None:
    exported = export_extension("edge", tmp_path / f"edge-{adapter}")
    source = (exported / adapter).read_text(encoding="utf-8")
    assert "stopGeneration(SELECTORS.stop)" in source
    assert "cancelled: true" in source
    assert "isCancelled" in source


def test_progress_is_observed_not_polled_in_the_exported_extension(tmp_path: Path) -> None:
    """A hidden window throttles timers, so progress must be DOM-driven."""
    exported = export_extension("edge", tmp_path / "edge-observer")
    kit = (exported / "site_kit.js").read_text(encoding="utf-8")
    assert "function observeText(" in kit
    assert "new MutationObserver(" in kit
    # The reason is recorded where the next reader will need it.
    assert "throttle" in kit.lower()


@pytest.mark.parametrize("adapter", ["site_chatgpt.js", "site_gemini.js"])
def test_each_adapter_observes_and_disconnects(tmp_path: Path, adapter: str) -> None:
    exported = export_extension("edge", tmp_path / f"edge-obs-{adapter}")
    source = (exported / adapter).read_text(encoding="utf-8")
    assert "observeText(" in source
    # An observer that is never disconnected leaks for the life of the page.
    assert "stopObserving()" in source


def test_each_automation_surface_is_closed_with_its_lease(tmp_path: Path) -> None:
    """Completed jobs leave no window behind and cannot close a peer's."""
    exported = export_extension("edge", tmp_path / "edge-window")
    background = (exported / "background.js").read_text(encoding="utf-8")
    assert "async function releaseSurface(" in background
    assert "surfaceLeases.delete(lease.leaseId)" in background
    assert "ext.windows.remove(lease.windowId)" in background


def test_completion_is_gated_not_inferred_from_one_signal(tmp_path: Path) -> None:
    """Both independent reviews agreed no single signal may end a turn."""
    exported = export_extension("edge", tmp_path / "edge-gate")
    kit = (exported / "site_kit.js").read_text(encoding="utf-8")
    assert "function createCompletionGate(" in kit
    # The two traps both reviewers named are recorded where they are handled.
    assert "NEGATIVE evidence" in kit
    assert "VALIDATION, not a completion signal" in kit
    for adapter in ("site_chatgpt.js", "site_gemini.js"):
        source = (exported / adapter).read_text(encoding="utf-8")
        assert "createCompletionGate(" in source, f"{adapter} must gate completion"
        assert "gate.observe(" in source, f"{adapter} must consult the gate"


def test_the_turn_loop_is_woken_by_activity_not_only_a_timer(tmp_path: Path) -> None:
    """setTimeout is throttled in the minimized automation window."""
    exported = export_extension("edge", tmp_path / "edge-activity")
    kit = (exported / "site_kit.js").read_text(encoding="utf-8")
    assert "function createActivityWaiter(" in kit
    for adapter in ("site_chatgpt.js", "site_gemini.js"):
        source = (exported / adapter).read_text(encoding="utf-8")
        assert "activity.wait(" in source, f"{adapter} must wait on activity"
        # A fixed sleep would reintroduce the throttling it was replacing.
        assert "setTimeout(resolve, 500)" not in source, f"{adapter} still has a fixed sleep"


def test_the_tick_uses_a_port_not_a_service_worker_timer(tmp_path: Path) -> None:
    """A finished reply stops mutating, so something must still wake the turn.

    A setInterval in the background does not survive: this is a Manifest V3
    service worker and a timer does not keep one alive, so it is terminated
    after about 30s idle and the turn hangs. A connected port does keep it
    alive. This was observed intermittently in live runs before the fix.
    """
    exported = export_extension("edge", tmp_path / "edge-tick")
    background = (exported / "background.js").read_text(encoding="utf-8")
    content = (exported / "content.js").read_text(encoding="utf-8")
    manifest = (exported / "manifest.json").read_text(encoding="utf-8")

    assert '"manifest_version": 3' in manifest, "the reasoning below assumes MV3"
    assert "ext.runtime.onConnect.addListener(" in background
    assert "port.onDisconnect.addListener(" in background, "the ticker must stop with the port"
    assert "ext.runtime.connect(" in content
    assert "closeTickPort()" in content, "the port must be released when the turn ends"
    # The reason is recorded where the next reader will need it.
    assert "does not keep a Manifest V3 service worker" in background
    # The measured throttling figure is recorded, so the next reader does not
    # have to rediscover why a clock is needed at all.
    assert "once a MINUTE" in background


def test_submission_is_verified_rather_than_assumed(tmp_path: Path) -> None:
    """A click the page ignores otherwise waits out the whole timeout."""
    exported = export_extension("edge", tmp_path / "edge-submit")
    source = (exported / "site_chatgpt.js").read_text(encoding="utf-8")
    assert "did not accept the submitted prompt" in source
    # The check must be synchronous: waitFor does not await its getter, so an
    # async one is always truthy and every submission looks accepted.
    assert "const submitted = () => {" in source


def test_gemini_selectors_do_not_depend_on_a_language(tmp_path: Path) -> None:
    exported = export_extension("edge", tmp_path / "edge-locale")
    source = (exported / "site_gemini.js").read_text(encoding="utf-8")
    send_block = source[source.index("send: ["):source.index("stop: [")]
    # A structural anchor must come before any aria-label text match.
    assert send_block.index("button.send-button") < send_block.index("aria-label")


def test_gemini_strips_site_chrome_from_the_reply(tmp_path: Path) -> None:
    exported = export_extension("edge", tmp_path / "edge-chrome")
    source = (exported / "site_gemini.js").read_text(encoding="utf-8")
    assert "function readWithoutChrome(" in source
    for selector in ("button", "model-thoughts", "sources-list"):
        assert selector in source, f"{selector} must be excluded from the reply text"


def test_a_turn_stalls_on_inactivity_not_on_elapsed_time(tmp_path: Path) -> None:
    """A long reasoning turn can legitimately outrun any fixed deadline.

    Killing it while the page is visibly still working reports a failure that
    did not happen.
    """
    exported = export_extension("edge", tmp_path / "edge-idle")
    for adapter in ("site_chatgpt.js", "site_gemini.js"):
        source = (exported / adapter).read_text(encoding="utf-8")
        assert "idleLimitMs" in source, f"{adapter} must bound inactivity, not elapsed time"
        assert "lastActivityAt" in source
        # The site saying it is working must count as being alive.
        assert "if (active || text !== lastSeenText)" in source
        # The absolute cap survives only as a backstop.
        assert "hardDeadline" in source


def test_glm_thinking_is_activity_not_a_response(tmp_path: Path) -> None:
    exported = export_extension("edge", tmp_path / "edge-glm-thinking")
    source = (exported / "site_extra.js").read_text(encoding="utf-8")
    assert "excludeResponses" in source
    assert '[class*="thinking" i]' in source
    assert "node.closest?.(selector)" in source


def test_the_adapter_reports_a_stall_before_the_bridge_gives_up(tmp_path: Path) -> None:
    """Otherwise the bridge wins the race and reports a generic timeout."""
    exported = export_extension("edge", tmp_path / "edge-margin")
    background = (exported / "background.js").read_text(encoding="utf-8")
    assert "- 5) * 1000" in background
    assert "the bridge wins the race" in background


def test_the_composer_is_not_submitted_after_a_user_edits_it(tmp_path: Path) -> None:
    """The automation window is a real window the user can reach."""
    exported = export_extension("edge", tmp_path / "edge-composer")
    source = (exported / "site_chatgpt.js").read_text(encoding="utf-8")
    assert "userTouchedComposer" in source
    # Only real user input counts; our own writes are synthetic.
    assert "event.isTrusted" in source
    assert "refusing to submit" in source


def test_the_job_carries_its_fencing_identity(tmp_path: Path) -> None:
    """Without it the extension only ever sees the default generation."""
    import inspect

    from fancy_gpt.bridge.client import BridgeBrowserDriver

    source = inspect.getsource(BridgeBrowserDriver.submit)
    assert '"generation_epoch"' in source, "the job must carry the epoch a cancel is checked against"


def test_the_extension_answers_every_cancel(tmp_path: Path) -> None:
    exported = export_extension("edge", tmp_path / "edge-cancel-ack")
    background = (exported / "background.js").read_text(encoding="utf-8")
    assert '"cancel_result"' in background
    # Each refusal path must say why, not fall silent.
    for reason in ("no such job is running", "cancel names a different generation"):
        assert reason in background
