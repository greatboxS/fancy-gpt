from __future__ import annotations

import json
from pathlib import Path

from fancy_gpt.bridge.client import BridgeBrowserDriver
from fancy_gpt.browser import FakeBrowserDriver
from fancy_gpt.browser.base import BrowserResponse, BrowserTurn
from fancy_gpt.engine import ReviewEngine
from fancy_gpt.models import RawRequest
from fancy_gpt.providers import ChatGPTWebAutomationProvider
from tests.helpers import final_payload, planner_payload


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "design.md").write_text("# Design\nA -> B\n")
    return repo


def test_raw_request_defaults_to_persistent_conversation_mode():
    request = RawRequest(mode="review", objective="check something")
    assert request.conversation_mode == "persistent"
    assert request.conversation_id is None


def test_engine_persists_conversation_id_from_final_response(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    engine = ReviewEngine(tmp_path / "work", allowed_roots=[tmp_path])
    req = RawRequest(mode="review", objective="review architecture", repo_root=str(repo), domains=["architecture"])
    route = engine.route(req, skill_name="technical-review")
    driver = FakeBrowserDriver([
        json.dumps(planner_payload(required_sections=route.required_sections)),
        lambda turn: json.dumps(final_payload(turn.request_id, sections=route.required_sections)),
    ])
    provider = ChatGPTWebAutomationProvider(driver, tunnel_id="fake")
    engine.run_automatic(req, provider, skill_name="technical-review")

    request_id = tmp_path.joinpath("work", "requests")
    [request_dir] = list(request_id.iterdir())
    status = engine.status(request_dir.name)
    # RawRequest.conversation_mode defaults to "persistent" with no explicit
    # conversation_id, so FakeBrowserDriver echoes back a synthesized id for
    # the final stage.
    assert status.conversation_id == f"fake-conv-{request_dir.name}"


def test_engine_continues_an_explicit_conversation_id(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    engine = ReviewEngine(tmp_path / "work", allowed_roots=[tmp_path])
    req = RawRequest(
        mode="review",
        objective="follow up on the prior review",
        repo_root=str(repo),
        domains=["architecture"],
        conversation_id="existing-conv-123",
    )
    route = engine.route(req, skill_name="technical-review")
    driver = FakeBrowserDriver([
        json.dumps(planner_payload(required_sections=route.required_sections)),
        lambda turn: json.dumps(final_payload(turn.request_id, sections=route.required_sections)),
    ])
    provider = ChatGPTWebAutomationProvider(driver, tunnel_id="fake")
    engine.run_automatic(req, provider, skill_name="technical-review")

    assert driver.prompts  # sanity: turns were actually submitted
    # Both planner and final turns should have targeted the same existing
    # conversation, and the engine should keep reporting that same id back.
    request_dir = next((tmp_path / "work" / "requests").iterdir())
    status = engine.status(request_dir.name)
    assert status.conversation_id == "existing-conv-123"


class _RecordingConnection:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def test_bridge_client_sends_continue_payload_when_conversation_id_given():
    driver = BridgeBrowserDriver("ws://example", "tok", "chrome-remote")
    driver._connection = _RecordingConnection()
    turn = driver.begin_turn(request_id="r1", stage="planner", conversation_id="conv-abc")
    driver.submit(turn, "hello")
    [sent] = driver._connection.sent
    assert sent["conversation"] == {"mode": "continue", "conversation_id": "conv-abc"}


def test_bridge_client_sends_persistent_payload_when_mode_persistent_no_id():
    driver = BridgeBrowserDriver("ws://example", "tok", "chrome-remote")
    driver._connection = _RecordingConnection()
    turn = driver.begin_turn(request_id="r1", stage="planner", conversation_mode="persistent")
    driver.submit(turn, "hello")
    [sent] = driver._connection.sent
    assert sent["conversation"] == {"mode": "persistent"}


def test_bridge_client_sends_fresh_payload_by_default():
    driver = BridgeBrowserDriver("ws://example", "tok", "chrome-remote")
    driver._connection = _RecordingConnection()
    turn = driver.begin_turn(request_id="r1", stage="planner")
    driver.submit(turn, "hello")
    [sent] = driver._connection.sent
    assert sent["conversation"] == {"mode": "fresh"}


class _DistinctConversationDriver:
    """Every fresh turn gets its OWN unique conversation id unless one is
    explicitly passed in, so this can actually distinguish "the final turn
    inherited the planner's conversation_id" from "they coincidentally
    computed the same string" (FakeBrowserDriver derives both from the
    shared request_id, which would pass even without the engine fix).
    """

    name = "distinct-conv-driver"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.begin_turn_calls: list[dict] = []
        self._counter = 0

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def health_check(self) -> None:
        return None

    def begin_turn(self, *, request_id, stage, conversation_id=None, conversation_mode="temporary", site=None):
        self._counter += 1
        self.begin_turn_calls.append({"stage": stage, "conversation_id": conversation_id, "mode": conversation_mode})
        resolved = conversation_id or f"auto-conv-{self._counter}"
        return BrowserTurn(
            turn_id=f"turn-{self._counter}", request_id=request_id, stage=stage,
            conversation_id=resolved, conversation_mode=conversation_mode,
        )

    def submit(self, turn, prompt):
        return None

    def wait_for_response(self, turn, *, timeout_s):
        scripted = self._responses.pop(0)
        text = scripted(turn) if callable(scripted) else scripted
        return BrowserResponse(turn_id=turn.turn_id, text=text, response_identity=turn.turn_id, conversation_id=turn.conversation_id)

    def close_turn(self, turn):
        return None


def test_planner_is_temporary_and_main_chat_continues_across_requests(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    engine = ReviewEngine(tmp_path / "work", allowed_roots=[tmp_path])
    req = RawRequest(mode="review", objective="review architecture", repo_root=str(repo), domains=["architecture"])
    route = engine.route(req, skill_name="technical-review")
    driver = _DistinctConversationDriver([
        json.dumps(planner_payload(required_sections=route.required_sections)),
        lambda turn: json.dumps(final_payload(turn.request_id, sections=route.required_sections)),
        json.dumps(planner_payload(required_sections=route.required_sections)),
        lambda turn: json.dumps(final_payload(turn.request_id, sections=route.required_sections)),
    ])
    provider = ChatGPTWebAutomationProvider(driver, tunnel_id="distinct")
    engine.run_automatic(req, provider, skill_name="technical-review")
    engine.run_automatic(req, provider, skill_name="technical-review")

    first_planner, first_final, second_planner, second_final = driver.begin_turn_calls
    assert first_planner == {"stage": "planner", "conversation_id": None, "mode": "temporary"}
    assert first_final == {"stage": "final", "conversation_id": None, "mode": "persistent"}
    assert second_planner == {"stage": "planner", "conversation_id": None, "mode": "temporary"}
    assert second_final == {"stage": "final", "conversation_id": "auto-conv-2", "mode": "persistent"}

    [session] = engine.list_sessions()
    [chat] = engine.list_chats(session.session_id)
    assert session.active_chat_id == chat.chat_id
    assert chat.conversation_id == "auto-conv-2"
    assert len(chat.request_ids) == 2
