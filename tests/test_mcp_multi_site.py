from __future__ import annotations

from types import SimpleNamespace

from fancy_gpt import mcp_server
from fancy_gpt.project_models import AgentRole


def test_add_project_work_item_passes_site_to_service(monkeypatch) -> None:
    captured = {}

    class Service:
        def add_work_item(self, project_id, **kwargs):
            captured.update(project_id=project_id, **kwargs)
            return SimpleNamespace()

    monkeypatch.setattr(mcp_server, "_project_service", lambda: Service())
    mcp_server.add_project_work_item("project-1", "Ask Gemini", "question", AgentRole.RESEARCHER, site="gemini")

    assert captured["project_id"] == "project-1"
    assert captured["site"] == "gemini"


def test_ask_focused_uses_explicit_site(monkeypatch, tmp_path) -> None:
    captured = {}

    class Service:
        def relevant_context(self, project_id, work_item_id):
            return None

    class Coordinator:
        def __init__(self, *args, **kwargs):
            pass

        def run_focused(self, question, **kwargs):
            captured["question"] = question
            return SimpleNamespace(conversation_binding=None)

    monkeypatch.setattr(mcp_server, "_project_service", lambda: Service())
    monkeypatch.setattr(mcp_server, "_manager", lambda: SimpleNamespace())
    monkeypatch.setattr(mcp_server, "ExecutionCoordinator", Coordinator)
    monkeypatch.setenv("FANCY_GPT_WORKDIR", str(tmp_path))

    mcp_server.ask_focused("Who are you?", site="gemini")

    assert captured["question"].site == "gemini"


def test_ask_focused_does_not_move_a_session_between_sites(monkeypatch) -> None:
    class Service:
        def session(self, project_id, session_id):
            return SimpleNamespace(work_item_id="work-1", site="chatgpt")

    monkeypatch.setattr(mcp_server, "_project_service", lambda: Service())

    try:
        mcp_server.ask_focused("Who are you?", project_id="project-1", session_id="session-1", site="gemini")
    except ValueError as exc:
        assert str(exc) == "session belongs to a different site"
    else:
        raise AssertionError("cross-site session reuse must be rejected")
