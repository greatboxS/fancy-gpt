from __future__ import annotations

import json
from pathlib import Path

import pytest

from fancy_gpt.gateway import (
    _gateway_prompt,
    GatewayService,
    anthropic_response,
    gemini_response,
    normalize_anthropic,
    normalize_gemini,
    normalize_openai,
    openai_response,
    codex_models,
)
from fancy_gpt.gateway_app import create_app
from fancy_gpt.models import AutomatedModelResponse, RequestState
from .asgi_harness import request
from fancy_gpt.tunnels.models import TunnelHealth, TunnelHealthState, TunnelSelection


class Provider:
    name = "fake-web"

    def __init__(self) -> None:
        self.requests = []

    def start(self):
        pass

    def stop(self):
        pass

    def execute(self, request):
        self.requests.append(request)
        envelope = {"type": "message", "text": "gateway-ok"}
        if '"name": "read_file"' in request.prompt and "Use the read_file tool" in request.prompt and "TOOL RESULT" not in request.prompt:
            envelope = {"type": "tool_calls", "calls": [{"id": "call_1", "name": "read_file", "arguments": {"file_path": "README.md"}}]}
        if '"name": "exec_command"' in request.prompt and "Use the exec_command tool" in request.prompt and "TOOL RESULT" not in request.prompt:
            envelope = {"type": "tool_calls", "calls": [{"id": "call_exec_1", "name": "exec_command", "arguments": {"cmd": "printf GATEWAY-TOOL-OK"}}]}
        if '"name": "Bash"' in request.prompt and "Use the Bash tool" in request.prompt and "TOOL RESULT" not in request.prompt:
            envelope = {"type": "tool_calls", "calls": [{"id": "call_bash_1", "name": "Bash", "arguments": {"command": "printf GATEWAY-CLAUDE-TOOL-OK"}}]}
        return AutomatedModelResponse(request_id=request.request_id, stage="agent", provider=self.name, raw_text=json.dumps(envelope), response_identity="response-1", conversation_id="conversation-1")


class Manager:
    def __init__(self) -> None:
        self.model = Provider()

    def select(self, **_kwargs):
        return TunnelSelection(tunnel_id="edge-remote", reason="test", explicit=True, health=TunnelHealth(tunnel_id="edge-remote", state=TunnelHealthState.HEALTHY, detail="ok"))

    def provider(self, _selection):
        return self.model


def test_protocol_normalization_and_output_mapping() -> None:
    openai = normalize_openai({"model": "fancy-gemini", "instructions": "system", "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}]})
    anthropic = normalize_anthropic({"model": "fancy-chatgpt", "system": "system", "messages": [{"role": "user", "content": "hello"}]})
    gemini = normalize_gemini({"systemInstruction": {"parts": [{"text": "system"}]}, "contents": [{"role": "user", "parts": [{"text": "hello"}]}]}, "fancy-gemini")
    assert openai.messages[0].text == anthropic.messages[0].text == gemini.messages[0].text == "hello"
    assert openai.instructions == anthropic.instructions == gemini.instructions == "system"


def test_browser_prompt_compacts_large_claude_metadata() -> None:
    tools = [{
        "name": f"tool_{index}",
        "description": "verbose " * 200,
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "detail " * 200}},
            "required": ["path"],
        },
    } for index in range(90)]
    turn = normalize_anthropic({
        "model": "fancy-chatgpt",
        "system": "system metadata " * 5000,
        "messages": [{"role": "user", "content": "ACTUAL-QUESTION"}],
        "tools": tools,
    })
    prompt = _gateway_prompt(turn, include_history=True)
    assert "ACTUAL-QUESTION" in prompt
    assert "tool_89" in prompt
    assert '"name": "path"' in prompt
    assert "system metadata system metadata" in prompt
    assert "[system metadata:" not in prompt
    assert len(prompt) > 70_000
    assert len(prompt) < 700_000


def test_gateway_prompt_keeps_exact_output_requests_inside_json_envelope(tmp_path: Path) -> None:
    manager = Manager()
    service = GatewayService(tmp_path, manager=manager)
    service.execute(normalize_gemini({"contents": [{"role": "user", "parts": [{"text": "Return exactly HELLO"}]}]}, "fancy-gemini"))
    prompt = manager.model.requests[0].prompt
    assert "Return exactly one valid JSON object" in prompt
    assert "exact output in the `text` field" in prompt


def test_gateway_context_ledger_and_tool_calls(tmp_path: Path) -> None:
    manager = Manager()
    service = GatewayService(tmp_path, manager=manager)
    first = service.execute(normalize_openai({"model": "fancy-gemini", "input": "hello"}))
    second = service.execute(normalize_openai({"model": "fancy-gemini", "previous_response_id": first.response_id, "input": "Use the read_file tool", "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}]}))
    assert second.session_id == first.session_id
    assert second.tool_calls[0].name == "read_file"
    assert manager.model.requests[1].metadata["conversation_id"] == "conversation-1"
    assert service.store.load(second.response_id).previous_response_id == first.response_id
    assert service.requests.load_status(second.response_id).state == RequestState.COMPLETE
    assert service.requests.load_status(second.response_id).kind == "gateway"
    assert openai_response(second)["output"][0]["type"] == "function_call"
    assert anthropic_response(second)["stop_reason"] == "tool_use"
    assert "functionCall" in gemini_response(second)["candidates"][0]["content"]["parts"][0]


def test_gateway_tool_result_continues_existing_browser_conversation(tmp_path: Path) -> None:
    manager = Manager()
    service = GatewayService(tmp_path, manager=manager)
    first = service.execute(normalize_openai({"model": "fancy-gemini", "input": "Use the read_file tool", "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}]}))
    final = service.execute(normalize_openai({"model": "fancy-gemini", "previous_response_id": first.response_id, "input": [{"type": "function_call_output", "call_id": "call_1", "output": "contents"}], "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}]}))
    assert final.text == "gateway-ok"
    assert manager.model.requests[-1].metadata["conversation_id"] == "conversation-1"


def test_gateway_correlates_full_transcript_to_one_provider_chat(tmp_path: Path) -> None:
    manager = Manager()
    service = GatewayService(tmp_path, manager=manager)
    first = service.execute(normalize_anthropic({"model": "fancy-chatgpt", "messages": [{"role": "user", "content": "remember alpha"}]}))
    second = service.execute(normalize_anthropic({"model": "fancy-chatgpt", "messages": [
        {"role": "user", "content": "remember alpha"},
        {"role": "assistant", "content": "gateway-ok"},
        {"role": "user", "content": "what was it?"},
    ]}))
    assert second.session_id == first.session_id
    assert manager.model.requests[-1].metadata["conversation_id"] == "conversation-1"
    assert service.store.load(second.response_id).previous_response_id == first.response_id


def test_codex_model_catalog_shape() -> None:
    catalog = codex_models()
    assert [model["slug"] for model in catalog["models"]] == ["fancy-chatgpt", "fancy-gemini", "fancy-claude"]
    assert catalog["models"][0]["truncation_policy"]["mode"] == "tokens"


def test_gemini_function_response_is_normalized_as_tool_result() -> None:
    turn = normalize_gemini({"contents": [{"role": "user", "parts": [{"functionResponse": {"id": "call_1", "name": "read_file", "response": {"output": "contents"}}}]}]}, "fancy-gemini")
    assert "TOOL RESULT call_1" in turn.messages[0].text
    assert "contents" in turn.messages[0].text


def test_gemini_model_role_is_canonicalized_for_context_matching() -> None:
    turn = normalize_gemini({"contents": [{"role": "model", "parts": [{"text": "acknowledged"}]}]}, "fancy-gemini")
    assert turn.messages[0].role == "assistant"


def test_gateway_rejects_oversized_input_and_audits_failure(tmp_path: Path) -> None:
    service = GatewayService(tmp_path, manager=Manager())
    service.max_input_units = 10
    with pytest.raises(ValueError, match="context budget"):
        service.execute(normalize_openai({"model": "fancy-gemini", "input": "x" * 1000}))
    status = service.requests.list_statuses()[0]
    assert status.kind == "gateway"
    assert status.state == RequestState.FAILED


def test_gateway_http_three_protocols_streaming_and_auth(tmp_path: Path) -> None:
    app = create_app(GatewayService(tmp_path, manager=Manager()), token="secret")

    def post(path: str, payload: dict, *, auth: bool = True):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer secret"
        return request(app, "POST", path, body=payload, headers=headers)

    assert post("/v1/responses", {"model": "fancy-gemini", "input": "hello"}, auth=False).status == 401

    streamed = post("/v1/responses", {"model": "fancy-gemini", "input": "hello", "stream": True})
    assert streamed.status == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")
    names = [event.get("event") for event in streamed.sse_events()]
    assert "response.completed" in names
    assert names.index("response.output_item.added") < names.index("response.output_text.delta")

    messages = post("/v1/messages", {"model": "fancy-chatgpt", "messages": [{"role": "user", "content": "hello"}]})
    assert messages.json()["content"][0]["text"] == "gateway-ok"

    gemini = post(
        "/v1beta/models/fancy-gemini:generateContent",
        {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
    )
    assert gemini.json()["candidates"][0]["content"]["parts"][0]["text"] == "gateway-ok"


def test_codex_prompt_cache_key_becomes_gateway_session() -> None:
    turn = normalize_openai({
        "model": "fancy-chatgpt",
        "prompt_cache_key": "01a0b542-1691-7432-8400-d934626596b6",
        "input": "hello",
    })
    assert turn.session_id == "01a0b542-1691-7432-8400-d934626596b6"


def test_gateway_prompt_requires_explicitly_requested_tool() -> None:
    turn = normalize_openai({
        "model": "fancy-chatgpt",
        "input": "Use the exec_command tool exactly once to run printf OK",
        "tools": [{
            "type": "function", "name": "exec_command",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
        }],
    })
    prompt = _gateway_prompt(turn, include_history=True)
    assert "THIS TURN REQUIRES a tool call" in prompt
    assert "Required offered tool(s): exec_command" in prompt
    assert "do not provide the requested post-tool final answer" in prompt


def test_gateway_rejects_final_message_when_explicit_tool_was_required(tmp_path: Path) -> None:
    service = GatewayService(tmp_path, manager=Manager())
    turn = normalize_openai({
        "model": "fancy-chatgpt",
        "input": "Use the exec_command tool to run printf OK",
        "tools": [{
            "type": "function", "name": "exec_command",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
        }],
    })
    with pytest.raises(ValueError, match="tool call was required"):
        service._parse_envelope({"type": "message", "text": "OK"}, turn)
