from __future__ import annotations

import json

from typer.testing import CliRunner

from fancy_gpt.cli import app

runner = CliRunner()


def test_tunnel_components_command_reports_independent_layers() -> None:
    result = runner.invoke(app, ["tunnels", "components"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert {item["id"] for item in payload["sites"]} == {"chatgpt"}
    assert {item["id"] for item in payload["runtimes"]} >= {"extension", "playwright", "cdp", "interactive"}
    assert {item["id"] for item in payload["transports"]} >= {"native-messaging", "websocket", "local-process", "cdp"}


def test_tunnel_explain_reports_layered_health() -> None:
    result = runner.invoke(app, ["tunnels", "explain", "playwright-chromium-local"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["spec"]["runtime"] == "playwright"
    assert {item["layer"] for item in payload["layers"]} == {"site", "runtime", "transport", "composition"}


def test_tunnel_inspect_alias_exists() -> None:
    result = runner.invoke(app, ["tunnels", "inspect", "interactive-manual"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["spec"]["id"] == "interactive-manual"
