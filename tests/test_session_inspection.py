from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fancy_gpt.cli import app
from fancy_gpt.engine import ReviewEngine
from fancy_gpt.models import ChatPolicy, RequestMode


runner = CliRunner()


def make_engine(tmp_path: Path) -> tuple[ReviewEngine, Path, Path]:
    repo = tmp_path / "repo"
    workdir = tmp_path / "work"
    repo.mkdir()
    return ReviewEngine(workdir, allowed_roots=[tmp_path]), repo, workdir


def add_failed_request(engine: ReviewEngine, session_id: str, chat_id: str) -> str:
    request_id = "req-context"
    engine.store.create_status(
        request_id,
        route_kind="skill",
        route_name="independent-design",
        skill="independent-design",
        mode=RequestMode.DESIGN,
        objective="design a bounded dashboard",
        session_id=session_id,
        chat_id=chat_id,
        chat_policy=ChatPolicy.CONTINUE,
    )
    engine.session_store.append_request(session_id, chat_id, request_id)
    engine.store.update_status(
        request_id,
        provider="chatgpt-web-automation:edge-extension-ws-remote",
        tunnel_id="edge-extension-ws-remote",
        partial_text="planner json",
    )
    engine.store.fail(request_id, "ContextTooLargeError: required P0 context does not fit context budget")
    return request_id


def test_inspect_session_reports_failed_request_recovery_hint(tmp_path: Path) -> None:
    engine, repo, _ = make_engine(tmp_path)
    session = engine.create_session(str(repo), "Dashboard")
    chat = engine.create_chat(session.session_id, "Main")
    add_failed_request(engine, session.session_id, chat.chat_id)

    inspection = engine.inspect_session(session.session_id)

    assert inspection.state == "open"
    assert inspection.active_chat_id == chat.chat_id
    assert inspection.chat_count == 1
    [inspected_chat] = inspection.chats
    assert inspected_chat.conversation_binding_state == "unbound"
    assert inspected_chat.tunnel_id == "edge-extension-ws-remote"
    assert inspected_chat.latest_request is not None
    assert inspected_chat.latest_request.state.value == "failed"
    assert inspected_chat.latest_request.has_partial_text is True
    assert inspected_chat.latest_request.recovery_hint is not None
    assert inspected_chat.latest_request.recovery_hint.code == "context-too-large"
    assert inspection.tunnels[0].state == "unknown"


def test_sessions_inspect_cli_human_output_is_not_raw_json(tmp_path: Path) -> None:
    engine, repo, workdir = make_engine(tmp_path)
    session = engine.create_session(str(repo), "Dashboard")
    chat = engine.create_chat(session.session_id, "Main")
    add_failed_request(engine, session.session_id, chat.chat_id)

    result = runner.invoke(app, ["sessions", "inspect", session.session_id, "--workdir", str(workdir)])

    assert result.exit_code == 0, result.stdout
    assert "Session " in result.stdout
    assert "ContextTooLargeError" in result.stdout
    # The failure is shown with the action that resolves it, not just the error.
    assert "Hint: Reduce required P0 context" in result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_sessions_inspect_cli_json_matches_canonical_model(tmp_path: Path) -> None:
    engine, repo, workdir = make_engine(tmp_path)
    session = engine.create_session(str(repo), "Dashboard")
    chat = engine.create_chat(session.session_id, "Main")
    add_failed_request(engine, session.session_id, chat.chat_id)

    result = runner.invoke(app, ["sessions", "inspect", session.session_id, "--json", "--workdir", str(workdir)])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "1.0"
    assert payload["session_id"] == session.session_id
    assert payload["chats"][0]["latest_request"]["recovery_hint"]["code"] == "context-too-large"


def test_inspect_session_includes_archived_and_closed_state(tmp_path: Path) -> None:
    engine, repo, _ = make_engine(tmp_path)
    session = engine.create_session(str(repo), "Dashboard")
    active = engine.create_chat(session.session_id, "Active")
    spare = engine.create_chat(session.session_id, "Spare", make_active=False)
    engine.select_chat(session.session_id, spare.chat_id)
    engine.archive_chat(session.session_id, active.chat_id)
    engine.close_session(session.session_id)

    inspection = engine.inspect_session(session.session_id)

    assert inspection.state == "closed"
    assert {chat.chat_id for chat in inspection.chats} == {active.chat_id, spare.chat_id}
    archived = next(chat for chat in inspection.chats if chat.chat_id == active.chat_id)
    assert archived.is_archived is True
