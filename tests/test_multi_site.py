from __future__ import annotations

from pathlib import Path

import pytest

from fancy_gpt.bridge import BridgeBrowserDriver
from fancy_gpt.extension_utils import export_extension
from fancy_gpt.tunnels import TunnelRegistry
from fancy_gpt.tunnels.factory import TunnelDriverFactory
from fancy_gpt.web.sites import SiteRegistry


def test_every_registered_site_is_reachable_through_a_tunnel() -> None:
    # A site contract nothing can select is a contract that will silently rot.
    sites = {item.id for item in SiteRegistry().all()}
    reachable = {spec.site for spec in TunnelRegistry().all()}
    assert sites == reachable


def test_a_tunnel_drives_the_site_its_spec_declares() -> None:
    # The job used to carry a hardcoded "chatgpt" regardless of the spec, so a
    # Gemini tunnel would have been handed to the ChatGPT adapter.
    factory = TunnelDriverFactory()
    registry = TunnelRegistry()
    for tunnel_id, expected in [("edge-extension-ws-remote", "chatgpt"), ("edge-gemini-ws-remote", "gemini")]:
        driver = factory.build(registry.get(tunnel_id))
        assert isinstance(driver, BridgeBrowserDriver)
        assert driver.site == expected


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


def test_one_browser_registers_a_tunnel_per_site(tmp_path: Path) -> None:
    # A single browser can drive every site it has an adapter for; requiring a
    # separate profile per site would be an artificial limit.
    background = (export_extension("edge", tmp_path / "edge-ids") / "background.js").read_text(encoding="utf-8")
    assert "function tunnelIdsFor(config)" in background
    assert "tunnelSuffix" in background

    transport = (export_extension("edge", tmp_path / "edge-ids") / "bridge_transport.js").read_text(encoding="utf-8")
    assert "config.tunnelIds ?? [config.tunnelId]" in transport


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
