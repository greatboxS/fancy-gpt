from __future__ import annotations

import json
from pathlib import Path

from importlib.resources import files

from fancy_gpt.extension_utils import export_extension


def test_extension_exports_chromium_and_firefox_manifests(tmp_path: Path) -> None:
    chrome = export_extension("chrome", tmp_path / "chrome")
    firefox = export_extension("firefox", tmp_path / "firefox")
    edge = export_extension("edge", tmp_path / "edge")

    chrome_manifest = json.loads((chrome / "manifest.json").read_text())
    edge_manifest = json.loads((edge / "manifest.json").read_text())
    firefox_manifest = json.loads((firefox / "manifest.json").read_text())

    assert chrome_manifest["manifest_version"] == 3
    assert chrome_manifest["background"]["service_worker"] == "background.js"
    assert edge_manifest == chrome_manifest
    assert firefox_manifest["background"]["scripts"] == ["bridge_transport.js", "background.js"]
    assert firefox_manifest["browser_specific_settings"]["gecko"]["id"] == "fancy-gpt@local"
    scripts = {entry["matches"][0]: entry["js"] for entry in chrome_manifest["content_scripts"]}
    assert scripts["https://chatgpt.com/*"] == ["site_kit.js", "site_chatgpt.js", "content.js"]
    assert scripts["https://gemini.google.com/*"] == ["site_kit.js", "site_gemini.js", "content.js"]
    # The kit must load before an adapter that calls into it.
    for js in scripts.values():
        assert js.index("site_kit.js") < min(js.index(name) for name in js if name.startswith("site_") and name != "site_kit.js")
    firefox_scripts = {entry["matches"][0]: entry["js"] for entry in firefox_manifest["content_scripts"]}
    assert firefox_scripts == scripts
    for directory in (chrome, edge, firefox):
        assert (directory / "background.js").is_file()
        assert (directory / "bridge_transport.js").is_file()
        assert (directory / "site_chatgpt.js").is_file()
        assert (directory / "content.js").is_file()
        assert (directory / "popup.html").is_file()


def test_extension_export_patches_browser_specific_defaults(tmp_path: Path) -> None:
    for browser in ("chrome", "edge", "firefox"):
        target = export_extension(browser, tmp_path / browser)
        background = (target / "background.js").read_text()
        popup = (target / "popup.html").read_text()
        assert f'browserName: "{browser}"' in background
        assert f'tunnelId: "{browser}-extension-ws-remote"' in background
        assert f'value="{browser}"' in popup
        assert f'value="{browser}-extension-ws-remote"' in popup


def test_native_manifest_contract_differs_for_firefox_and_chromium(tmp_path: Path) -> None:
    from fancy_gpt.bridge.native_manifest import native_host_manifest, windows_registry_key
    executable = tmp_path / "fancy-gpt-native-host"
    executable.write_text("", encoding="utf-8")
    chrome = native_host_manifest("chrome", "abc", executable)
    firefox = native_host_manifest("firefox", "fancy-gpt@local", executable)
    assert chrome["allowed_origins"] == ["chrome-extension://abc/"]
    assert "allowed_extensions" not in chrome
    assert firefox["allowed_extensions"] == ["fancy-gpt@local"]
    assert "allowed_origins" not in firefox
    assert "Google\\Chrome" in windows_registry_key("chrome")
    assert "Mozilla" in windows_registry_key("firefox")


def test_extension_layers_do_not_mix_site_selectors_into_runtime_or_transport(tmp_path: Path) -> None:
    target = export_extension("chrome", tmp_path / "chrome-layered")
    site = (target / "site_chatgpt.js").read_text(encoding="utf-8")
    content = (target / "content.js").read_text(encoding="utf-8")
    runtime = (target / "background.js").read_text(encoding="utf-8")
    transport = (target / "bridge_transport.js").read_text(encoding="utf-8")
    assert "prompt-textarea" in site
    assert "data-turn-id" in site
    assert "prompt-textarea" not in content
    assert "prompt-textarea" not in runtime
    assert "chatgpt.com" not in transport
    assert "connectNative" in transport
    assert "WebSocket" in transport


def test_chromium_manifest_requires_websocket_service_worker_lifetime_support(tmp_path: Path) -> None:
    chrome = export_extension("chrome", tmp_path / "chrome-min-version")
    manifest = json.loads((chrome / "manifest.json").read_text())
    assert int(manifest["minimum_chrome_version"]) >= 116


def test_continue_generation_is_scoped_to_the_current_response(tmp_path: Path) -> None:
    site = (export_extension("chrome", tmp_path / "chrome-current-turn") / "site_chatgpt.js").read_text()

    # A stale button on an older ChatGPT turn must not keep a completed job
    # alive until its timeout. The current response is bound first, then only
    # that turn's subtree is searched for a continuation control.
    bind_position = site.index("if (boundId == null)")
    continue_position = site.index("const continueButton")
    assert bind_position < continue_position
    assert "findButtonByText(/continue generating/i, boundTurns[0])" in site
    assert 'findButtonByText(/continue generating/i);' not in site


def test_extension_background_exposes_site_health_operation():
    source = Path("extension/common/background.js").read_text(encoding="utf-8")
    assert '"site.health"' in source
    assert '"fancy_site_health"' in source
    assert "JSON.stringify(payload)" in source


def test_export_stamps_a_build_id_the_adapter_can_report(tmp_path: Path) -> None:
    from fancy_gpt.extension_utils import adapter_build_id

    exported = export_extension("edge", tmp_path / "edge")
    kit = (exported / "site_kit.js").read_text(encoding="utf-8")
    assert "__FANCYGPT_ADAPTER_BUILD__" not in kit
    assert f'BUILD = "{adapter_build_id()}"' in kit
    # One value, reported by every adapter, rather than a copy per site.
    for name in ("site_chatgpt.js", "site_gemini.js"):
        source = (exported / name).read_text(encoding="utf-8")
        assert "__FANCYGPT_ADAPTER_BUILD__" not in source
        assert "kit.build" in source


def test_build_id_covers_every_adapter_source() -> None:
    # Adding or changing any site adapter must produce a new id, or a browser
    # left on the previous build would still claim to be current.
    from fancy_gpt.extension_utils import _adapter_source_names

    names = _adapter_source_names()
    assert "site_kit.js" in names
    assert {"site_chatgpt.js", "site_gemini.js"} <= set(names)


def test_provider_refuses_a_browser_running_a_different_adapter_build() -> None:
    # The browser is usually on another machine, so an extension that was never
    # reloaded looks exactly like one that was, until it fails somewhere that
    # has nothing to do with the real cause.
    import pytest

    from fancy_gpt.browser import BrowserUiDriftError
    from fancy_gpt.extension_utils import adapter_build_id
    from fancy_gpt.providers import ChatGPTWebAutomationProvider

    require = ChatGPTWebAutomationProvider._require_current_adapter
    require({"ok": True, "build": adapter_build_id()})

    with pytest.raises(BrowserUiDriftError, match="too old to report one"):
        require({"ok": True})
    with pytest.raises(BrowserUiDriftError, match="build deadbeef"):
        require({"ok": True, "build": "deadbeef"})


def test_adapter_drift_can_be_overridden_deliberately(monkeypatch) -> None:
    from fancy_gpt.providers import ChatGPTWebAutomationProvider

    monkeypatch.setenv("FANCY_GPT_ALLOW_ADAPTER_DRIFT", "1")
    ChatGPTWebAutomationProvider._require_current_adapter({"ok": True})


def test_composer_writes_through_the_editor_input_path(tmp_path: Path) -> None:
    # A chat composer is a rich-text editor that owns its document model.
    # Assigning textContent mutates the DOM behind its back, so the editor never
    # learns the field is non-empty and the send control never appears. This
    # lives in the shared kit so no new adapter has to rediscover it.
    kit = (export_extension("edge", tmp_path / "edge-composer") / "site_kit.js").read_text(encoding="utf-8")

    insert = kit.index('execCommand("insertText"')
    fallback = kit.index("composer.textContent = prompt")
    assert insert < fallback, "the direct write must only be a fallback"
    assert "selectAll(composer)" in kit, "a retry must replace the text, not append to it"
