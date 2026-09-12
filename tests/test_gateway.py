from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from fancy_gpt.gateway import (
    GatewayApplication,
    GatewayService,
    anthropic_response,
    gemini_response,
    normalize_anthropic,
    normalize_gemini,
    normalize_openai,
    openai_response,
    codex_models,
)
from fancy_gpt.models import AutomatedModelResponse, RequestState
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
    openai = normalize_openai({"model": "gemini-web", "instructions": "system", "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}]})
    anthropic = normalize_anthropic({"model": "chatgpt-web", "system": "system", "messages": [{"role": "user", "content": "hello"}]})
    gemini = normalize_gemini({"systemInstruction": {"parts": [{"text": "system"}]}, "contents": [{"role": "user", "parts": [{"text": "hello"}]}]}, "gemini-web")
    assert openai.messages[0].text == anthropic.messages[0].text == gemini.messages[0].text == "hello"
    assert openai.instructions == anthropic.instructions == gemini.instructions == "system"


def test_gateway_prompt_keeps_exact_output_requests_inside_json_envelope(tmp_path: Path) -> None:
    manager = Manager()
    service = GatewayService(tmp_path, manager=manager)
    service.execute(normalize_gemini({"contents": [{"role": "user", "parts": [{"text": "Return exactly HELLO"}]}]}, "gemini-web"))
    prompt = manager.model.requests[0].prompt
    assert "OUTPUT TRANSPORT CONTRACT (HIGHEST PRIORITY)" in prompt
    assert "apply to the `text` field" in prompt


def test_gateway_context_ledger_and_tool_calls(tmp_path: Path) -> None:
    manager = Manager()
    service = GatewayService(tmp_path, manager=manager)
    first = service.execute(normalize_openai({"model": "gemini-web", "input": "hello"}))
    second = service.execute(normalize_openai({"model": "gemini-web", "previous_response_id": first.response_id, "input": "Use the read_file tool", "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}]}))
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
    first = service.execute(normalize_openai({"model": "gemini-web", "input": "Use the read_file tool", "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}]}))
    final = service.execute(normalize_openai({"model": "gemini-web", "previous_response_id": first.response_id, "input": [{"type": "function_call_output", "call_id": "call_1", "output": "contents"}], "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}]}))
    assert final.text == "gateway-ok"
    assert manager.model.requests[-1].metadata["conversation_id"] == "conversation-1"


def test_codex_model_catalog_shape() -> None:
    catalog = codex_models()
    assert [model["slug"] for model in catalog["models"]] == ["chatgpt-web", "gemini-web", "claude-web"]
    assert catalog["models"][0]["truncation_policy"]["mode"] == "tokens"


def test_gemini_function_response_is_normalized_as_tool_result() -> None:
    turn = normalize_gemini({"contents": [{"role": "user", "parts": [{"functionResponse": {"id": "call_1", "name": "read_file", "response": {"output": "contents"}}}]}]}, "gemini-web")
    assert "TOOL RESULT call_1" in turn.messages[0].text
    assert "contents" in turn.messages[0].text


def test_gateway_rejects_oversized_input_and_audits_failure(tmp_path: Path) -> None:
    service = GatewayService(tmp_path, manager=Manager())
    service.max_input_units = 10
    with pytest.raises(ValueError, match="context budget"):
        service.execute(normalize_openai({"model": "gemini-web", "input": "x" * 1000}))
    status = service.requests.list_statuses()[0]
    assert status.kind == "gateway"
    assert status.state == RequestState.FAILED


def test_gateway_http_three_protocols_streaming_and_auth(tmp_path: Path) -> None:
    app = GatewayApplication(GatewayService(tmp_path, manager=Manager()), token="secret")
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(path: str, payload: dict, *, auth: bool = True) -> tuple[str, str]:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer secret"
        response = urlopen(Request(base + path, data=json.dumps(payload).encode(), headers=headers), timeout=5)
        return response.headers.get_content_type(), response.read().decode()

    try:
        with HTTPErrorContext(401):
            post("/v1/responses", {"model": "gemini-web", "input": "hello"}, auth=False)
        content_type, body = post("/v1/responses", {"model": "gemini-web", "input": "hello", "stream": True})
        assert content_type == "text/event-stream"
        assert "response.completed" in body
        assert body.index("response.output_item.added") < body.index("response.output_text.delta")
        _, body = post("/v1/messages", {"model": "chatgpt-web", "messages": [{"role": "user", "content": "hello"}]})
        assert json.loads(body)["content"][0]["text"] == "gateway-ok"
        _, body = post("/v1beta/models/gemini-web:generateContent", {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]})
        assert json.loads(body)["candidates"][0]["content"]["parts"][0]["text"] == "gateway-ok"
    finally:
        server.shutdown()
        server.server_close()


class HTTPErrorContext:
    def __init__(self, code: int) -> None:
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, kind, value, _traceback):
        return kind is HTTPError and value.code == self.code
