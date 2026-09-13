from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from fancy_gpt.browser.base import BrowserResponse, BrowserTurn
from fancy_gpt.engine import ReviewEngine
from fancy_gpt.errors import InvalidStateError
from fancy_gpt.models import ChatPolicy, RawRequest, SessionCapabilities
from fancy_gpt.providers import ChatGPTWebAutomationProvider
from tests.helpers import final_payload, planner_payload


class SessionDriver:
    name = "session-driver"

    def __init__(self, sections: list[str], *, invalid_final: bool = False) -> None:
        self.sections = sections
        self.invalid_final = invalid_final
        self.calls: list[BrowserTurn] = []
        self.begin_inputs: list[dict[str, str | None]] = []
        self.counter = 0

    def start(self): pass
    def stop(self): pass
    def health_check(self): pass
    def submit(self, turn, prompt): pass
    def close_turn(self, turn): pass

    def begin_turn(self, *, request_id, stage, conversation_id=None, conversation_mode="temporary", site=None, generation_epoch=0):
        self.counter += 1
        self.begin_inputs.append({"stage": stage, "conversation_id": conversation_id, "mode": conversation_mode})
        turn = BrowserTurn(
            turn_id=f"turn-{self.counter}", request_id=request_id, stage=stage,
            conversation_id=conversation_id or f"conversation-{self.counter}",
            conversation_mode=conversation_mode,
        )
        self.calls.append(turn)
        return turn

    def wait_for_response(self, turn, *, timeout_s):
        if turn.stage == "planner":
            text = json.dumps(planner_payload(required_sections=self.sections))
        elif self.invalid_final:
            text = '{"invalid": true}'
        else:
            text = json.dumps(final_payload(turn.request_id, sections=self.sections))
        return BrowserResponse(
            turn_id=turn.turn_id, text=text, response_identity=turn.turn_id,
            conversation_id=turn.conversation_id,
        )


def make_engine(tmp_path: Path) -> tuple[ReviewEngine, Path]:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "design.md").write_text("# Example\n")
    return ReviewEngine(tmp_path / "work", allowed_roots=[tmp_path]), repo


def run(engine: ReviewEngine, request: RawRequest, *, invalid_final: bool = False) -> SessionDriver:
    route = engine.route(request, skill_name="technical-review")
    driver = SessionDriver(route.required_sections, invalid_final=invalid_final)
    engine.run_automatic(
        request, ChatGPTWebAutomationProvider(driver, tunnel_id="test-tunnel"),
        skill_name="technical-review",
    )
    return driver


def test_independent_review_does_not_replace_active_chat(tmp_path: Path) -> None:
    engine, repo = make_engine(tmp_path)
    base = RawRequest(mode="review", objective="main discussion", repo_root=str(repo))
    run(engine, base)
    session = engine.list_sessions()[0]
    main_chat_id = session.active_chat_id

    independent = base.model_copy(update={
        "objective": "independent review",
        "session_id": session.session_id,
        "chat_policy": ChatPolicy.INDEPENDENT,
    })
    driver = run(engine, independent)

    refreshed = engine.get_session(session.session_id)
    chats = engine.list_chats(session.session_id)
    assert refreshed.active_chat_id == main_chat_id
    assert len(chats) == 2
    review_chat = next(chat for chat in chats if chat.chat_id != main_chat_id)
    assert review_chat.kind.value == "independent"
    assert driver.calls[0].conversation_mode == "temporary"
    assert driver.calls[1].conversation_mode == "persistent"


def test_new_chat_becomes_active_and_can_be_selected_back(tmp_path: Path) -> None:
    engine, repo = make_engine(tmp_path)
    session = engine.create_session(str(repo), "Architecture")
    main = engine.create_chat(session.session_id, "Main")
    alternative = engine.create_chat(session.session_id, "Alternative")
    assert engine.get_session(session.session_id).active_chat_id == alternative.chat_id
    engine.select_chat(session.session_id, main.chat_id)
    assert engine.get_session(session.session_id).active_chat_id == main.chat_id


def test_new_chat_request_branches_from_main_and_becomes_active(tmp_path: Path) -> None:
    engine, repo = make_engine(tmp_path)
    base = RawRequest(mode="review", objective="main", repo_root=str(repo))
    first = run(engine, base)
    session = engine.list_sessions()[0]
    old_active = session.active_chat_id

    branch = base.model_copy(update={
        "objective": "new direction",
        "session_id": session.session_id,
        "chat_policy": ChatPolicy.NEW_CHAT,
    })
    second = run(engine, branch)
    refreshed = engine.get_session(session.session_id)
    assert refreshed.active_chat_id != old_active
    assert len(engine.list_chats(session.session_id)) == 2
    assert first.calls[1].conversation_mode == "persistent"
    assert second.calls[1].conversation_mode == "persistent"
    assert second.begin_inputs[1]["conversation_id"] is None


def test_conversation_binding_survives_final_validation_failure(tmp_path: Path) -> None:
    engine, repo = make_engine(tmp_path)
    request = RawRequest(mode="review", objective="invalid downstream result", repo_root=str(repo))
    route = engine.route(request, skill_name="technical-review")
    driver = SessionDriver(route.required_sections, invalid_final=True)
    with pytest.raises(Exception):
        engine.run_automatic(
            request, ChatGPTWebAutomationProvider(driver, tunnel_id="test-tunnel"),
            skill_name="technical-review",
        )

    session = engine.list_sessions()[0]
    [chat] = engine.list_chats(session.session_id)
    [status] = engine.list_session_requests(session.session_id)
    assert chat.conversation_id == "conversation-2"
    assert status.conversation_id == "conversation-2"
    assert status.state.value == "failed"


def test_temporary_request_creates_no_session_or_chat(tmp_path: Path) -> None:
    engine, repo = make_engine(tmp_path)
    request = RawRequest(
        mode="review", objective="one off", repo_root=str(repo),
        chat_policy=ChatPolicy.TEMPORARY,
    )
    driver = run(engine, request)
    assert engine.list_sessions() == []
    assert all(turn.conversation_mode == "temporary" for turn in driver.calls)


def test_session_profile_advertises_the_runtime_contract() -> None:
    profile = Path("profiles/session-management.json").read_text(encoding="utf-8")
    assert SessionCapabilities.model_validate_json(profile) == SessionCapabilities()


def test_session_is_repo_scoped_and_closed_session_cannot_resume(tmp_path: Path) -> None:
    engine, repo = make_engine(tmp_path)
    session = engine.create_session(str(repo), "Scoped")
    other = tmp_path / "other"
    other.mkdir()
    request = RawRequest(
        mode="review", objective="wrong repository", repo_root=str(other),
        session_id=session.session_id,
    )
    with pytest.raises(ValueError, match="different repo_root"):
        engine.conversations.resolve(request)

    engine.close_session(session.session_id)
    request = request.model_copy(update={"repo_root": str(repo)})
    with pytest.raises(InvalidStateError, match="closed"):
        engine.conversations.resolve(request)
    with pytest.raises(InvalidStateError, match="closed"):
        engine.create_chat(session.session_id, "Not allowed")


def test_active_chat_must_be_changed_before_archive(tmp_path: Path) -> None:
    engine, repo = make_engine(tmp_path)
    session = engine.create_session(str(repo))
    active = engine.create_chat(session.session_id, "Active")
    spare = engine.create_chat(session.session_id, "Spare", make_active=False)
    with pytest.raises(InvalidStateError, match="select another"):
        engine.archive_chat(session.session_id, active.chat_id)
    engine.select_chat(session.session_id, spare.chat_id)
    archived = engine.archive_chat(session.session_id, active.chat_id)
    assert archived.archived_at is not None


def test_focused_turn_resolves_and_reuses_a_session_conversation(tmp_path: Path) -> None:
    engine, repo = make_engine(tmp_path)
    session = engine.create_session(str(repo), "Focused continuity")

    first = engine.conversations.resolve_focused(
        session.session_id, title="Remember a token", site="gemini"
    )
    assert first.site == "gemini"
    assert first.conversation_id is None
    engine.conversations.bind_conversation(first, "gemini-conversation-1", "edge-remote")

    second = engine.conversations.resolve_focused(
        session.session_id, title="Recall the token", site="gemini"
    )
    assert second.chat_id == first.chat_id
    assert second.conversation_id == "gemini-conversation-1"

    with pytest.raises(ValueError, match="chat belongs to site gemini"):
        engine.conversations.resolve_focused(
            session.session_id, title="Wrong site", site="chatgpt"
        )


def test_concurrent_main_resolution_and_request_attachment_are_atomic(tmp_path: Path) -> None:
    engine, repo = make_engine(tmp_path)
    request = RawRequest(mode="review", objective="parallel work", repo_root=str(repo))

    with ThreadPoolExecutor(max_workers=8) as pool:
        resolutions = list(pool.map(lambda _: engine.conversations.resolve(request), range(16)))
    assert len({item.chat_id for item in resolutions}) == 1

    resolution = resolutions[0]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: engine.conversations.attach_request(resolution, f"req-{index}"), range(32)))
    chat = engine.session_store.get_chat(resolution.session_id, resolution.chat_id)
    assert set(chat.request_ids) == {f"req-{index}" for index in range(32)}
