from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from fancy_gpt.cli import app

runner = CliRunner()


def test_tunnel_components_command_reports_independent_layers() -> None:
    result = runner.invoke(app, ["tunnels", "components", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert "sites" not in payload
    assert {item["id"] for item in payload["runtimes"]} >= {"extension", "playwright", "cdp", "interactive"}
    assert {item["id"] for item in payload["transports"]} >= {"native-messaging", "websocket", "local-process", "cdp"}


def test_tunnel_components_command_default_output_is_a_readable_table() -> None:
    result = runner.invoke(app, ["tunnels", "components"])
    assert result.exit_code == 0, result.stdout
    assert "RUNTIMES" in result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_sites_are_listed_outside_the_tunnel_surface() -> None:
    result = runner.invoke(app, ["sites", "list", "--json"])
    assert result.exit_code == 0, result.stdout
    assert {item["id"] for item in json.loads(result.stdout)} == {"chatgpt", "gemini", "grok", "kimi", "glm"}


def test_tunnel_explain_reports_layered_health() -> None:
    result = runner.invoke(app, ["tunnels", "explain", "chrome-remote", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["spec"]["browser"] == "chrome"
    assert {item["layer"] for item in payload["layers"]} == {"runtime", "transport", "composition"}


def test_tunnel_list_and_health_default_output_is_a_readable_table() -> None:
    list_result = runner.invoke(app, ["tunnels", "list"])
    assert list_result.exit_code == 0, list_result.stdout
    assert "RUNTIME" in list_result.stdout
    assert "chrome-remote" in list_result.stdout

    health_result = runner.invoke(app, ["tunnels", "health"])
    assert health_result.exit_code == 0, health_result.stdout
    assert "STATE" in health_result.stdout
    assert "chrome-remote" in health_result.stdout


def test_tunnel_inspect_alias_exists() -> None:
    result = runner.invoke(app, ["tunnels", "inspect", "edge-remote"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["spec"]["id"] == "edge-remote"
