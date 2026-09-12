from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from fancy_gpt.cli import app
from fancy_gpt.conversation_index import ConversationIndex
from fancy_gpt.engine import ReviewEngine
from fancy_gpt.project_models import AgentRole, ConversationStrategy
from fancy_gpt.project_service import ProjectService

runner = CliRunner()

CONV_A = "6aa52775-9f48-83ec-a24e-c8e42ebdd602"
CONV_B = "6aa526fe-28a8-83ec-8e86-42344a54367f"


def test_a_temporary_chat_is_never_listed(tmp_path: Path) -> None:
    # A temporary chat is not saved by ChatGPT and yields no id, so there is
    # nothing to link to and nothing to account for.
    engine = ReviewEngine(tmp_path, allowed_roots=[tmp_path])
    session = engine.create_session(str(tmp_path), "Work")
    engine.create_chat(session.session_id, "Main")
    assert ConversationIndex(tmp_path).all() == []


def test_a_bound_chat_is_listed_with_a_link(tmp_path: Path) -> None:
    engine = ReviewEngine(tmp_path, allowed_roots=[tmp_path])
    session = engine.create_session(str(tmp_path), "Work")
    chat = engine.create_chat(session.session_id, "Main")
    engine.session_store.update_chat(
        session.session_id, chat.chat_id, conversation_id=CONV_A, tunnel_id="edge-extension-ws-remote"
    )

    [found] = ConversationIndex(tmp_path).all()
    assert found.origin == "session"
    assert found.conversation_id == CONV_A
    assert found.url == f"https://chatgpt.com/c/{CONV_A}"
    assert found.tunnel_id == "edge-extension-ws-remote"


def test_work_items_sharing_a_thread_count_as_one_conversation(tmp_path: Path) -> None:
    service = ProjectService(tmp_path)
    project = service.create_project(name="p", target="ship it", repo_root=str(tmp_path))
    for title, role in [("Research", AgentRole.RESEARCHER), ("Design", AgentRole.DESIGNER)]:
        item = service.add_work_item(
            project.project_id, title=title, objective="work", role=role,
            conversation_key="main", conversation_strategy=ConversationStrategy.RESUME,
        )
        assignment = service.start_assignment(project.project_id, item.work_item_id)
        service.bind_session_conversation(project.project_id, assignment.session.session_id, CONV_A)
        service.finish_session(project.project_id, assignment.session.session_id, summary="done")

    [found] = ConversationIndex(tmp_path).all()
    assert found.conversation_id == CONV_A
    assert found.thread_key == "main"
    # Two work items, one thread: the index reports turns, not duplicates.
    assert found.turns == 2


def test_separate_threads_are_listed_separately(tmp_path: Path) -> None:
    service = ProjectService(tmp_path)
    project = service.create_project(name="p", target="ship it", repo_root=str(tmp_path))
    for title, role, binding in [
        ("Research", AgentRole.RESEARCHER, CONV_A),
        ("Design", AgentRole.DESIGNER, CONV_B),
    ]:
        item = service.add_work_item(project.project_id, title=title, objective="work", role=role)
        assignment = service.start_assignment(project.project_id, item.work_item_id)
        service.bind_session_conversation(project.project_id, assignment.session.session_id, binding)
        service.finish_session(project.project_id, assignment.session.session_id, summary="done")

    assert {item.conversation_id for item in ConversationIndex(tmp_path).all()} == {CONV_A, CONV_B}


def test_cli_lists_threads_and_says_so_when_there_are_none(tmp_path: Path) -> None:
    empty = runner.invoke(app, ["conversations", "list", "--workdir", str(tmp_path)])
    assert empty.exit_code == 0, empty.stdout
    assert "No ChatGPT thread has been opened" in empty.stdout

    engine = ReviewEngine(tmp_path, allowed_roots=[tmp_path])
    session = engine.create_session(str(tmp_path), "Work")
    chat = engine.create_chat(session.session_id, "Main")
    engine.session_store.update_chat(session.session_id, chat.chat_id, conversation_id=CONV_A)

    listed = runner.invoke(app, ["conversations", "list", "--workdir", str(tmp_path)])
    assert listed.exit_code == 0, listed.stdout
    assert CONV_A in listed.stdout
