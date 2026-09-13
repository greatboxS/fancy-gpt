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
        assert f'tunnelId: "{browser}-remote"' in background
        assert f'value="{browser}"' in popup
        assert f'value="{browser}-remote"' in popup


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


def test_the_build_id_is_the_same_for_every_browser(tmp_path: Path) -> None:
    """Otherwise the drift check locks out every browser but Chromium.

    The runtime compares whatever the browser reports against one expected id.
    While that id was computed per family, Firefox reported its own, could
    never match, and every Firefox turn was refused with a message telling the
    user to reload an extension that was already current -- with disabling the
    check as the only way past it.
    """
    from fancy_gpt.extension_utils import adapter_build_id

    ids = {family: adapter_build_id(family) for family in ("chromium", "firefox")}
    assert len(set(ids.values())) == 1, f"one adapter surface, one id: {ids}"
    # And what each browser actually ships says the same.
    stamped = {
        browser: (export_extension(browser, tmp_path / browser) / "site_kit.js").read_text(encoding="utf-8")
        for browser in ("chrome", "edge", "firefox")
    }
    for browser, text in stamped.items():
        assert f'BUILD = "{adapter_build_id()}"' in text, f"{browser} stamps a different build"


def test_a_real_adapter_change_still_changes_the_build_id() -> None:
    # Normalising the per-browser defaults must not blunt the check itself.
    from fancy_gpt.extension_utils import _normalised_for_build_id

    base = 'tunnelId: "chrome-remote",\nbrowserName: "chrome",\nconst SELECTORS = {a: 1};'
    other_browser = 'tunnelId: "firefox-remote",\nbrowserName: "firefox",\nconst SELECTORS = {a: 1};'
    real_change = 'tunnelId: "chrome-remote",\nbrowserName: "chrome",\nconst SELECTORS = {a: 2};'
    assert _normalised_for_build_id(base) == _normalised_for_build_id(other_browser)
    assert _normalised_for_build_id(base) != _normalised_for_build_id(real_change)


def test_the_exporter_and_the_runtime_agree_on_the_build_id(tmp_path: Path) -> None:
    """The build script must not carry its own copy of this calculation.

    It did, and they drifted: the runtime learned to normalise the per-browser
    defaults out of the id while the script did not, so every exported bundle
    was stamped with a value the runtime would reject. The drift guard would
    have done exactly its job and refused every browser, pointing the user at
    an extension that was in fact current.
    """
    import subprocess
    import sys

    from fancy_gpt.extension_utils import adapter_build_id

    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "build_extension.py"
    if not script.exists():  # pragma: no cover - source checkout only
        pytest.skip("build script is not part of the installed package")
    printed = subprocess.run(
        [sys.executable, "-c",
         "import runpy,sys;sys.argv=['x'];"
         f"m=runpy.run_path(r'{script}');print(m['adapter_build_id']())"],
        capture_output=True, text=True, cwd=root,
    )
    assert printed.returncode == 0, printed.stderr
    assert printed.stdout.strip() == adapter_build_id(), "exporter and runtime must compute one id"


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


def test_drift_points_at_the_side_that_is_actually_stale(monkeypatch, tmp_path: Path) -> None:
    """Two ids that differ say only that; either side could be the old one.

    The guard fired in three directions in one day: a browser behind the
    install, a browser ahead of it, and a long-running process holding code
    from before the install it was reporting. Telling everyone to reload the
    extension is wrong in two of those, and pointing at the wrong side costs
    more than saying nothing.
    """
    import pytest

    from fancy_gpt.browser import BrowserUiDriftError
    from fancy_gpt.providers import ChatGPTWebAutomationProvider

    with pytest.raises(BrowserUiDriftError) as raised:
        ChatGPTWebAutomationProvider._require_current_adapter({"ok": True, "build": "deadbeef"})
    message = str(raised.value)
    assert "deadbeef" in message
    # Both remedies are named, and neither is presented as the only one.
    assert "reload the extension" in message
    assert "restart" in message


def test_the_hint_never_asserts_which_side_is_stale() -> None:
    """It guessed once and guessed wrong.

    A build id is computed from files on disk when it is asked, so a process
    holding older code still reports the current value whenever the calculation
    itself has not changed. "This process is older than its package" therefore
    does not establish that the process is the stale side -- and the first time
    that conclusion fired, the extension was the stale one and the message sent
    the user to restart the runtime instead.
    """
    from fancy_gpt.providers.web_automation import _stale_side_hint

    hint = _stale_side_hint()
    assert "reload the extension" in hint
    assert "restart this process" in hint.lower()
    # Stated as a reason to try something, never as a finding.
    assert "must be" not in hint


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


def test_automation_never_drives_a_hidden_document(tmp_path: Path) -> None:
    """The page being automated must be one the browser is still drawing.

    This test used to require the opposite -- a minimized window -- on the
    assumption that a minimized window keeps rendering. It does not. Chromium
    reports a minimized, fully occluded, or merely-not-active document as
    hidden, and a hidden document stops being painted part-way through a
    streamed reply: measured live at 21 to 61 characters of a 1294 character
    answer, with the stop control frozen alongside it, so the turn looked
    finished while holding a fragment and stalled until its deadline.
    """
    background = (export_extension("edge", tmp_path / "edge-background") / "background.js").read_text(encoding="utf-8")
    # The automation always gets its own window: a tab in the user's window
    # would have to keep taking over the one in front of them, because a tab
    # that is not active is a hidden document that stops being drawn.
    assert "separateTaskWindow" not in background
    assert "tab = await taskWindowFor(taskUrl)" in background
    # Its own window, still out of the way -- but never minimized.
    assert 'state: "minimized"' not in background
    assert 'state: "normal"' in background
    # And never a background tab. There is one path that opens one now, since
    # the shared-window mode is gone, and it opens it active.
    assert "active: false" not in background
    assert "active: true" in background


def test_the_task_window_survives_the_job_that_created_it(tmp_path: Path) -> None:
    """Otherwise every single turn takes the user's focus.

    Removing a window's last tab closes the window. The task tab was simply
    removed when its job ended, so the window never lasted past one job: the
    next found a dead window id and created a fresh, focused one. The reuse
    path and the idle close were unreachable code stating the opposite.
    """
    background = (export_extension("edge", tmp_path / "edge-window-life") / "background.js").read_text(encoding="utf-8")
    # The tab is handed back through one place, which parks it rather than
    # removing it when it is the last one in the task window.
    assert "await releaseTaskTab(tab.id)" in background
    assert 'ext.tabs.update(tabId, {url: "about:blank"})' in background
    # And the idle close still refuses to take a tab the user opened.
    assert "item.id === taskKeeperTabId" in background
