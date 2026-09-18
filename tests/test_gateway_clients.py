import json
from pathlib import Path

import pytest

from fancy_gpt.gateway_clients import (
    codex_command,
    configure_gateway_clients,
    ensure_codex_gateway,
)


def test_configure_gateway_clients_preserves_existing_settings_and_is_idempotent(tmp_path: Path) -> None:
    codex = tmp_path / ".codex" / "config.toml"
    codex.parent.mkdir()
    codex.write_text(
        'approval_policy = "on-request"\nprofile = "fancy-chatgpt"\n\n'
        '[profiles.fancy-gemini]\nmodel = "fancy-gemini"\n',
        encoding="utf-8",
    )
    claude = tmp_path / ".claude" / "settings.json"
    claude.parent.mkdir()
    claude.write_text(json.dumps({"permissions": {"allow": ["Read"]}, "env": {"KEEP": "yes"}}), encoding="utf-8")
    gemini = tmp_path / ".gemini" / ".env"
    gemini.parent.mkdir()
    gemini.write_text("KEEP=yes\n", encoding="utf-8")

    first = configure_gateway_clients(tmp_path)
    second = configure_gateway_clients(tmp_path)

    assert all(item.changed for item in first)
    assert not any(item.changed for item in second)
    assert 'approval_policy = "on-request"' in codex.read_text(encoding="utf-8")
    assert "[profiles.fancy-gemini]" not in codex.read_text(encoding="utf-8")
    assert 'profile = "fancy-chatgpt"' not in codex.read_text(encoding="utf-8")
    assert (tmp_path / ".codex" / "fancy-gemini.config.toml").read_text(encoding="utf-8") == (
        'model = "fancy-gemini"\nmodel_provider = "fancy-local"\n'
    )
    global_settings = json.loads(claude.read_text(encoding="utf-8"))
    assert global_settings["permissions"] == {"allow": ["Read"]}
    assert global_settings["env"]["KEEP"] == "yes"
    assert "ANTHROPIC_BASE_URL" not in global_settings["env"]
    assert global_settings["modelPicker"]["replaceBuiltInOptions"] is False
    assert [row["model"] for row in global_settings["modelPicker"]["options"]] == [
        "fancy-chatgpt", "fancy-gemini", "fancy-claude"
    ]
    settings = json.loads((tmp_path / ".claude" / "fancy-gpt-settings.json").read_text(encoding="utf-8"))
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8787"
    assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == "local"
    assert "ANTHROPIC_API_KEY" not in settings["env"]
    assert settings["model"] == "fancy-chatgpt"
    assert [row["model"] for row in settings["modelPicker"]["options"]] == [
        "fancy-chatgpt", "fancy-gemini", "fancy-claude"
    ]
    assert settings["modelPicker"]["replaceBuiltInOptions"] is True
    assert "KEEP=yes" in gemini.read_text(encoding="utf-8")
    assert "GOOGLE_GEMINI_BASE_URL" not in gemini.read_text(encoding="utf-8")
    assert "GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8787" in (
        tmp_path / ".gemini" / "fancy-gpt.env"
    ).read_text(encoding="utf-8")


def test_codex_migration_repairs_duplicate_managed_fragments(tmp_path: Path) -> None:
    codex = tmp_path / ".codex" / "config.toml"
    codex.parent.mkdir()
    codex.write_text(
        'keep = true\n\n# >>> fancy-gpt model gateway >>>\n[model_providers.fancy-local]\nname = "old"\n\n'
        '# >>> fancy-gpt model gateway >>>\n[model_providers.fancy-local]\nname = "newer"\n'
        '# <<< fancy-gpt model gateway <<<\n',
        encoding="utf-8",
    )

    configure_gateway_clients(tmp_path)

    text = codex.read_text(encoding="utf-8")
    assert text.count("# >>> fancy-gpt model gateway >>>") == 1
    assert text.count("[model_providers.fancy-local]") == 1
    assert "keep = true" in text


def test_codex_launcher_defaults_to_chatgpt_without_persisted_profile() -> None:
    command = codex_command(["exec", "hello"], executable="/usr/bin/codex")

    assert command[0] == "/usr/bin/codex"
    assert 'model_provider="fancy-local"' in command
    assert 'model_providers.fancy-local.base_url="http://127.0.0.1:8787/v1"' in command
    assert command[command.index("--model") + 1] == "fancy-chatgpt"
    assert command[-2:] == ["exec", "hello"]


def test_codex_launcher_preserves_explicit_gateway_model() -> None:
    command = codex_command(
        ["--model", "fancy-gemini", "exec", "hello"], executable="/usr/bin/codex"
    )

    assert command.count("--model") == 1
    assert command[command.index("--model") + 1] == "fancy-gemini"


def test_codex_launcher_refuses_model_missing_from_gateway(monkeypatch) -> None:
    monkeypatch.setattr("fancy_gpt.gateway_clients.gateway_models", lambda _url: {"fancy-chatgpt"})

    with pytest.raises(RuntimeError, match="fancy-gemini"):
        ensure_codex_gateway("fancy-gemini")
