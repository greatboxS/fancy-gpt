from __future__ import annotations

import json
from pathlib import Path

from fancy_gpt.bridge.client import BridgeBrowserDriver
from fancy_gpt.browser import FakeBrowserDriver
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
    driver = BridgeBrowserDriver("ws://example", "tok", "chrome-extension-ws-remote")
    driver._connection = _RecordingConnection()
    turn = driver.begin_turn(request_id="r1", stage="planner", conversation_id="conv-abc")
    driver.submit(turn, "hello")
    [sent] = driver._connection.sent
    assert sent["conversation"] == {"mode": "continue", "conversation_id": "conv-abc"}


def test_bridge_client_sends_persistent_payload_when_mode_persistent_no_id():
    driver = BridgeBrowserDriver("ws://example", "tok", "chrome-extension-ws-remote")
    driver._connection = _RecordingConnection()
    turn = driver.begin_turn(request_id="r1", stage="planner", conversation_mode="persistent")
    driver.submit(turn, "hello")
    [sent] = driver._connection.sent
    assert sent["conversation"] == {"mode": "persistent"}


def test_bridge_client_sends_fresh_payload_by_default():
    driver = BridgeBrowserDriver("ws://example", "tok", "chrome-extension-ws-remote")
    driver._connection = _RecordingConnection()
    turn = driver.begin_turn(request_id="r1", stage="planner")
    driver.submit(turn, "hello")
    [sent] = driver._connection.sent
    assert sent["conversation"] == {"mode": "fresh"}
