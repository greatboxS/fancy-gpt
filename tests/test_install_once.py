from pathlib import Path

from typer.testing import CliRunner

from fancy_gpt.cli import app
from fancy_gpt.runtime_paths import default_browser_profile

runner = CliRunner()


def test_version_and_standalone_self_test() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.7.0"

    result = runner.invoke(app, ["test"])
    assert result.exit_code == 0
    assert '"offline_two_pass": "complete"' in result.stdout
    assert '"skills": 6' in result.stdout
    assert '"workflows": 4' in result.stdout
    assert '"domains": 13' in result.stdout


def test_init_creates_minimal_request(tmp_path: Path) -> None:
    out = tmp_path / "request.yaml"
    result = runner.invoke(app, ["init", "--output", str(out)])
    assert result.exit_code == 0
    text = out.read_text(encoding="utf-8")
    assert "mode: review" in text
    assert "repo_root: ." in text


def test_browser_profile_is_user_global(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FANCY_GPT_DATA_HOME", str(tmp_path / "data"))
    assert default_browser_profile() == tmp_path / "data" / "browser-profile"


def test_install_script_uses_bundled_tool_flow_without_forcing_playwright(tmp_path: Path) -> None:
    import os
    import subprocess

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        """#!/usr/bin/env bash
set -eu
echo "uv:$*" >> "$FAKE_LOG"
if [[ "$1 $2 $3" == "tool dir --bin" ]]; then
  echo "$FAKE_BIN"; exit 0
fi
if [[ "$1 $2" == "tool install" ]]; then
  cat > "$FAKE_BIN/fancy-gpt" <<'EOF'
#!/usr/bin/env bash
set -eu
echo "fg:$*" >> "$FAKE_LOG"
case "${1:-}" in
  test) echo '{"ok":true}' ;;
  verify) echo '{"ok":true}' ;;
  bridge) echo '{"pair_token":"fake"}' ;;
  browser-setup) echo '{"ok":true}' ;;
esac
EOF
  chmod +x "$FAKE_BIN/fancy-gpt"
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    dummy_wheel = tmp_path / "fancy_gpt-0.7.0-py3-none-any.whl"
    dummy_wheel.write_bytes(b"fake")
    env = os.environ.copy()
    env.update({
        "FANCY_GPT_UV_BIN": str(fake_uv),
        "FANCY_GPT_INSTALL_SOURCE": str(dummy_wheel),
        "FAKE_LOG": str(log),
        "FAKE_BIN": str(fake_bin),
    })
    completed = subprocess.run(["bash", "install.sh"], cwd=Path(__file__).resolve().parents[1], env=env, check=True, capture_output=True, text=True)
    assert '"pair_token":"fake"' in completed.stdout
    calls = log.read_text(encoding="utf-8")
    assert "uv:tool install --force" in calls
    assert "fg:test" in calls
    assert "fg:verify" in calls
    assert "fg:bridge init" in calls
    assert "browser-setup" not in calls


def test_install_script_playwright_is_explicit_opt_in(tmp_path: Path) -> None:
    import os
    import subprocess

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        """#!/usr/bin/env bash
set -eu
echo "uv:$*" >> "$FAKE_LOG"
if [[ "$1 $2 $3" == "tool dir --bin" ]]; then echo "$FAKE_BIN"; exit 0; fi
if [[ "$1 $2" == "tool install" ]]; then
  cat > "$FAKE_BIN/fancy-gpt" <<'EOF'
#!/usr/bin/env bash
set -eu
echo "fg:$*" >> "$FAKE_LOG"
[[ "${1:-}" == "bridge" ]] && echo '{"pair_token":"fake"}' || true
EOF
  chmod +x "$FAKE_BIN/fancy-gpt"
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    dummy_wheel = tmp_path / "fancy_gpt-0.7.0-py3-none-any.whl"
    dummy_wheel.write_bytes(b"fake")
    env = os.environ.copy()
    env.update({
        "FANCY_GPT_UV_BIN": str(fake_uv),
        "FANCY_GPT_INSTALL_SOURCE": str(dummy_wheel),
        "FAKE_LOG": str(log),
        "FAKE_BIN": str(fake_bin),
    })
    subprocess.run(["bash", "install.sh", "--with-playwright", "--skip-verify"], cwd=Path(__file__).resolve().parents[1], env=env, check=True, capture_output=True, text=True)
    assert "fg:browser-setup" in log.read_text(encoding="utf-8")


def test_native_config_derives_browser_specific_tunnel(monkeypatch, tmp_path: Path) -> None:
    import json

    data_home = tmp_path / "data"
    monkeypatch.setenv("FANCY_GPT_DATA_HOME", str(data_home))
    result = runner.invoke(app, ["extension", "native-config", "--browser", "edge"])
    assert result.exit_code == 0
    payload = json.loads((data_home / "native-host.json").read_text(encoding="utf-8"))
    assert payload["browser"] == "edge"
    assert payload["tunnel_ids"] == ["edge-extension-native-local"]


def test_native_config_rejects_unknown_browser(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FANCY_GPT_DATA_HOME", str(tmp_path / "data"))
    result = runner.invoke(app, ["extension", "native-config", "--browser", "safari"])
    assert result.exit_code != 0
