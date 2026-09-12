from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from .models import ModelRequest, RequestMode, RequestState
from .response_parser import parse_json_object
from .store import RequestStore
from .tunnels import TunnelManager


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GatewayContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    text: str


class GatewayTool(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    description: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class GatewayToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    arguments: dict[str, Any]


class NormalizedTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: str
    model: str
    instructions: str = ""
    messages: list[GatewayContent] = Field(default_factory=list)
    tools: list[GatewayTool] = Field(default_factory=list)
    previous_response_id: str | None = None
    session_id: str | None = None
    stream: bool = False


class GatewayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response_id: str
    session_id: str
    model: str
    site: str
    text: str = ""
    tool_calls: list[GatewayToolCall] = Field(default_factory=list)
    conversation_id: str | None = None
    created_at: str
    input_units: int
    output_units: int


class ContextLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response_id: str
    session_id: str
    protocol: str
    model: str
    site: str
    request_digest: str
    instructions_digest: str
    conversation_id: str | None = None
    previous_response_id: str | None = None
    created_at: str
    input_units: int
    output_units: int
    compaction_generation: int = 0
    tool_call_ids: list[str] = Field(default_factory=list)
    message_digests: list[str] = Field(default_factory=list)
    output_digest: str = ""


class GatewayStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve() / "gateway"
        self.turns = self.root / "turns"
        self.turns.mkdir(parents=True, exist_ok=True)

    def _path(self, response_id: str) -> Path:
        if not response_id.startswith("resp_") or not response_id[5:].isalnum():
            raise ValueError("invalid gateway response id")
        return self.turns / f"{response_id}.json"

    def save(self, ledger: ContextLedger) -> None:
        path = self._path(ledger.response_id)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(ledger.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def load(self, response_id: str) -> ContextLedger:
        return ContextLedger.model_validate_json(self._path(response_id).read_text(encoding="utf-8"))

    def latest(self, session_id: str) -> ContextLedger | None:
        matches: list[ContextLedger] = []
        for path in self.turns.glob("resp_*.json"):
            ledger = ContextLedger.model_validate_json(path.read_text(encoding="utf-8"))
            if ledger.session_id == session_id:
                matches.append(ledger)
        return max(matches, key=lambda item: item.created_at, default=None)

    def matching_predecessor(self, turn: NormalizedTurn, site: str) -> ContextLedger | None:
        incoming = [_message_digest(message) for message in turn.messages]
        joined = "\n".join(message.text for message in turn.messages)
        matches: list[ContextLedger] = []
        for path in self.turns.glob("resp_*.json"):
            ledger = ContextLedger.model_validate_json(path.read_text(encoding="utf-8"))
            if ledger.protocol != turn.protocol or ledger.model != turn.model or ledger.site != site:
                continue
            prefix_matches = bool(ledger.message_digests) and incoming[:len(ledger.message_digests)] == ledger.message_digests
            output_matches = bool(ledger.output_digest) and ledger.output_digest in incoming
            tool_matches = any(call_id in joined for call_id in ledger.tool_call_ids)
            if prefix_matches and (output_matches or tool_matches):
                matches.append(ledger)
        return max(matches, key=lambda item: item.created_at, default=None)


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _message_digest(message: GatewayContent) -> str:
    return _digest({"role": message.role, "text": message.text})


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") in {"tool_result", "function_call_output"}:
                    parts.append(f"TOOL RESULT {item.get('tool_use_id') or item.get('call_id')}: {_text(item.get('content') or item.get('output'))}")
                elif isinstance(item.get("functionResponse"), dict):
                    response = item["functionResponse"]
                    parts.append(
                        f"TOOL RESULT {response.get('id') or response.get('name')}: "
                        f"{_text(response.get('response'))}"
                    )
                elif item.get("type") == "tool_use":
                    parts.append(
                        f"TOOL CALL {item.get('id') or item.get('name')}: "
                        f"{item.get('name')} {json.dumps(item.get('input') or {}, ensure_ascii=False)}"
                    )
                elif isinstance(item.get("functionCall"), dict):
                    call = item["functionCall"]
                    parts.append(
                        f"TOOL CALL {call.get('id') or call.get('name')}: "
                        f"{call.get('name')} {json.dumps(call.get('args') or {}, ensure_ascii=False)}"
                    )
        return "\n".join(parts)
    if isinstance(value, dict):
        nested = value.get("text") or value.get("content") or value.get("parts")
        return _text(nested) if nested is not None else json.dumps(value, ensure_ascii=False)
    return str(value) if value is not None else ""


def normalize_openai(payload: dict[str, Any], session_id: str | None = None) -> NormalizedTurn:
    messages: list[GatewayContent] = []
    input_value = payload.get("input", "")
    if isinstance(input_value, str):
        messages.append(GatewayContent(role="user", text=input_value))
    else:
        for item in input_value or []:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "function_call_output":
                messages.append(GatewayContent(role="tool", text=f"TOOL RESULT {item.get('call_id')}: {_text(item.get('output'))}"))
            else:
                messages.append(GatewayContent(role=str(item.get("role", "user")), text=_text(item.get("content", item))))
    tools = []
    for tool in payload.get("tools") or []:
        if tool.get("type") == "function":
            tools.append(GatewayTool(name=tool.get("name", ""), description=tool.get("description"), parameters=tool.get("parameters") or {}))
    return NormalizedTurn(protocol="openai", model=payload.get("model", "chatgpt-web"), instructions=_text(payload.get("instructions")), messages=messages, tools=tools, previous_response_id=payload.get("previous_response_id"), session_id=session_id, stream=bool(payload.get("stream")))


def normalize_anthropic(payload: dict[str, Any], session_id: str | None = None) -> NormalizedTurn:
    messages = [GatewayContent(role=str(item.get("role", "user")), text=_text(item.get("content"))) for item in payload.get("messages") or []]
    tools = [GatewayTool(name=item.get("name", ""), description=item.get("description"), parameters=item.get("input_schema") or {}) for item in payload.get("tools") or []]
    return NormalizedTurn(protocol="anthropic", model=payload.get("model", "chatgpt-web"), instructions=_text(payload.get("system")), messages=messages, tools=tools, session_id=session_id, stream=bool(payload.get("stream")))


def normalize_gemini(payload: dict[str, Any], model: str, session_id: str | None = None) -> NormalizedTurn:
    messages = [
        GatewayContent(
            role="assistant" if item.get("role") == "model" else str(item.get("role", "user")),
            text=_text(item.get("parts")),
        )
        for item in payload.get("contents") or []
    ]
    tools: list[GatewayTool] = []
    for group in payload.get("tools") or []:
        for item in group.get("functionDeclarations") or group.get("function_declarations") or []:
            tools.append(GatewayTool(name=item.get("name", ""), description=item.get("description"), parameters=item.get("parameters") or {}))
    config = payload.get("generationConfig") or payload.get("generation_config") or {}
    return NormalizedTurn(protocol="gemini", model=model, instructions=_text(payload.get("systemInstruction") or payload.get("system_instruction")), messages=messages, tools=tools, session_id=session_id, stream=bool(config.get("stream")))


def _gateway_prompt(turn: NormalizedTurn, *, include_history: bool) -> str:
    messages = turn.messages if include_history else turn.messages[-1:]
    transcript = "\n\n".join(f"{message.role.upper()}: {message.text}" for message in messages)
    tools = [tool.model_dump(mode="json") for tool in turn.tools]
    contract = {
        "type": "message",
        "text": "final assistant text",
    }
    tool_contract = {
        "type": "tool_calls",
        "calls": [{"id": "call_unique", "name": "exact tool name", "arguments": {}}],
    }
    return f"""Respond to this conversation.
SYSTEM: {turn.instructions or '(none)'}
{transcript}

Return exactly one valid JSON object and no Markdown.
For a final answer: {json.dumps(contract)}
Available tools: {json.dumps(tools, ensure_ascii=False)}
If a tool is needed: {json.dumps(tool_contract)}
Put any requested exact output in the `text` field. Never invent tool results.
"""


class GatewayService:
    def __init__(self, root: Path | str, manager: TunnelManager | None = None) -> None:
        self.store = GatewayStore(root)
        self.requests = RequestStore(Path(root))
        self.manager = manager or TunnelManager(timeout_s=float(os.getenv("FANCY_GPT_BROWSER_TIMEOUT", "300")))
        self._lock = threading.Lock()
        self.max_input_units = int(os.getenv("FANCY_GPT_GATEWAY_MAX_INPUT_UNITS", "200000"))

    @staticmethod
    def resolve_site(model: str) -> str:
        name = model.lower()
        if name.startswith("gemini"):
            return "gemini"
        if name.startswith("chatgpt") or name.startswith("gpt") or name.startswith("claude"):
            return "chatgpt"
        raise ValueError(f"unsupported gateway model: {model}")

    def execute(self, turn: NormalizedTurn, *, tunnel_id: str | None = None) -> GatewayResult:
        previous = self.store.load(turn.previous_response_id) if turn.previous_response_id else None
        site = self.resolve_site(turn.model)
        if previous is None and turn.session_id:
            previous = self.store.latest(turn.session_id)
        if previous is None:
            previous = self.store.matching_predecessor(turn, site)
        if previous and previous.site != site:
            raise ValueError("previous response belongs to a different model site")
        session_id = turn.session_id or (previous.session_id if previous else f"gw_{uuid.uuid4().hex[:16]}")
        response_id = f"resp_{uuid.uuid4().hex}"
        prompt = _gateway_prompt(turn, include_history=previous is None)
        input_units = max(1, len(prompt) // 4)
        self.requests.create_status(
            response_id,
            route_kind="skill",
            route_name=f"{turn.protocol}-gateway",
            skill="model-gateway",
            mode=RequestMode.CONSULT,
            objective=turn.messages[-1].text if turn.messages else turn.instructions,
            session_id=session_id,
            kind="gateway",
        )
        try:
            if input_units > self.max_input_units:
                raise ValueError(
                    f"gateway input exceeds context budget: {input_units} > {self.max_input_units} units"
                )
            selection = self.manager.select(tunnel_id=tunnel_id, policy="auto", require_automatic=True)
            provider = self.manager.provider(selection)
        except Exception as exc:
            self.requests.fail(response_id, str(exc))
            raise
        self.requests.update_status(
            response_id,
            state=RequestState.RUNNING_FINAL,
            tunnel_id=selection.tunnel_id,
            provider=provider.name,
        )
        request = ModelRequest(
            request_id=response_id,
            stage="agent",
            title=f"Gateway {turn.protocol} turn",
            prompt=prompt,
            response_schema={},
            metadata={
                "site": site,
                "conversation_id": previous.conversation_id if previous else None,
                "conversation_mode": "persistent",
            },
        )
        try:
            with self._lock:
                provider.start()
                try:
                    raw = provider.execute(request)
                finally:
                    provider.stop()
            value = parse_json_object(raw.raw_text)
        except Exception as exc:
            self.requests.fail(response_id, str(exc))
            raise
        text = ""
        calls: list[GatewayToolCall] = []
        try:
            if value.get("type") == "message" and isinstance(value.get("text"), str):
                text = value["text"]
            elif value.get("type") == "tool_calls" and isinstance(value.get("calls"), list):
                allowed = {tool.name for tool in turn.tools}
                for item in value["calls"]:
                    call = GatewayToolCall.model_validate(item)
                    if call.name not in allowed:
                        raise ValueError(f"model requested unavailable tool: {call.name}")
                    calls.append(call)
            else:
                raise ValueError("gateway model returned an invalid response envelope")
        except Exception as exc:
            self.requests.fail(response_id, str(exc))
            raise
        result = GatewayResult(response_id=response_id, session_id=session_id, model=turn.model, site=site, text=text, tool_calls=calls, conversation_id=raw.conversation_id, created_at=_now(), input_units=input_units, output_units=max(1, len(raw.raw_text) // 4))
        response_path = self.requests.write_text(response_id, "gateway-response.json", result.model_dump_json(indent=2))
        self.requests.update_status(
            response_id,
            state=RequestState.COMPLETE,
            final_response_file=str(response_path),
            conversation_id=raw.conversation_id,
        )
        self.store.save(ContextLedger(response_id=response_id, session_id=session_id, protocol=turn.protocol, model=turn.model, site=site, request_digest=_digest(turn.model_dump(mode="json")), instructions_digest=_digest(turn.instructions), conversation_id=raw.conversation_id, previous_response_id=previous.response_id if previous else None, created_at=result.created_at, input_units=result.input_units, output_units=result.output_units, tool_call_ids=[call.id for call in calls], message_digests=[_message_digest(message) for message in turn.messages], output_digest=_message_digest(GatewayContent(role="assistant", text=text)) if text else ""))
        return result


def openai_response(result: GatewayResult) -> dict[str, Any]:
    output: list[dict[str, Any]] = []
    if result.text:
        output.append({"id": f"msg_{uuid.uuid4().hex[:16]}", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": result.text, "annotations": []}]})
    output.extend({"type": "function_call", "id": call.id, "call_id": call.id, "name": call.name, "arguments": json.dumps(call.arguments, separators=(",", ":")), "status": "completed"} for call in result.tool_calls)
    return {"id": result.response_id, "object": "response", "created_at": int(datetime.now().timestamp()), "status": "completed", "model": result.model, "output": output, "usage": {"input_tokens": result.input_units, "output_tokens": result.output_units, "total_tokens": result.input_units + result.output_units}}


def anthropic_response(result: GatewayResult) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if result.text:
        content.append({"type": "text", "text": result.text})
    content.extend({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments} for call in result.tool_calls)
    return {"id": result.response_id.replace("resp_", "msg_"), "type": "message", "role": "assistant", "model": result.model, "content": content, "stop_reason": "tool_use" if result.tool_calls else "end_turn", "stop_sequence": None, "usage": {"input_tokens": result.input_units, "output_tokens": result.output_units}}


def gemini_response(result: GatewayResult) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if result.text:
        parts.append({"text": result.text})
    parts.extend({"functionCall": {"id": call.id, "name": call.name, "args": call.arguments}} for call in result.tool_calls)
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": result.input_units, "candidatesTokenCount": result.output_units, "totalTokenCount": result.input_units + result.output_units}, "responseId": result.response_id, "modelVersion": result.model}


def codex_models() -> dict[str, Any]:
    models = []
    for priority, name in enumerate(("chatgpt-web", "gemini-web", "claude-web"), start=1):
        models.append({
            "slug": name,
            "display_name": name,
            "description": f"{name} through the FancyGPT browser gateway",
            "base_instructions": "You are a coding agent. Follow the supplied instructions and use tools when needed.",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [
                {"effort": "low", "description": "Faster response"},
                {"effort": "medium", "description": "Balanced response"},
                {"effort": "high", "description": "Deeper response"},
            ],
            "shell_type": "shell_command",
            "visibility": "list",
            "minimal_client_version": [0, 1, 0],
            "supported_in_api": True,
            "priority": priority,
            "availability_nux": None,
            "upgrade": None,
            "support_verbosity": False,
            "default_verbosity": None,
            "apply_patch_tool_type": None,
            "truncation_policy": {"mode": "tokens", "limit": 200000},
            "context_window": 200000,
            "experimental_supported_tools": [],
            "input_modalities": ["text"],
        })
    return {"models": models}


@dataclass
class GatewayApplication:
    service: GatewayService
    token: str | None = None

    def handler(self) -> type[BaseHTTPRequestHandler]:
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "FancyGPTGateway/0.8"

            def _send(self, status: int, body: dict[str, Any], content_type: str = "application/json") -> None:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _sse(self, events: list[tuple[str | None, dict[str, Any]]]) -> None:
                chunks = []
                for event, data in events:
                    prefix = f"event: {event}\n" if event else ""
                    chunks.append(f"{prefix}data: {json.dumps(data, ensure_ascii=False)}\n\n")
                raw = "".join(chunks).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/health":
                    self._send(200, {"ok": True, "service": "fancy-gpt-gateway"})
                elif path == "/v1/models":
                    self._send(200, codex_models())
                else:
                    self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

            def do_HEAD(self) -> None:
                path = urlparse(self.path).path
                self.send_response(200 if path in {"/health", "/api/hello"} else 404)
                self.end_headers()

            def do_POST(self) -> None:
                try:
                    if app.token and self.headers.get("Authorization") != f"Bearer {app.token}" and self.headers.get("x-api-key") != app.token:
                        self._send(401, {"error": {"message": "invalid API key", "type": "authentication_error"}})
                        return
                    size = int(self.headers.get("Content-Length", "0"))
                    if size <= 0 or size > 10 * 1024 * 1024:
                        raise ValueError("request body size is invalid")
                    payload = json.loads(self.rfile.read(size))
                    session = self.headers.get("x-fancy-session-id")
                    tunnel = self.headers.get("x-fancy-tunnel-id")
                    path = urlparse(self.path).path
                    if path == "/v1/responses":
                        turn = normalize_openai(payload, session)
                        result = app.service.execute(turn, tunnel_id=tunnel)
                        body = openai_response(result)
                        if turn.stream:
                            events: list[tuple[str | None, dict[str, Any]]] = [(None, {"type": "response.created", "response": body | {"status": "in_progress", "output": []}})]
                            if result.text:
                                item = body["output"][0]
                                empty_item = item | {"status": "in_progress", "content": []}
                                empty_part = {"type": "output_text", "text": "", "annotations": []}
                                events.extend([
                                    (None, {"type": "response.output_item.added", "output_index": 0, "item": empty_item}),
                                    (None, {"type": "response.content_part.added", "item_id": item["id"], "output_index": 0, "content_index": 0, "part": empty_part}),
                                    (None, {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0, "content_index": 0, "delta": result.text}),
                                    (None, {"type": "response.output_text.done", "item_id": item["id"], "output_index": 0, "content_index": 0, "text": result.text}),
                                    (None, {"type": "response.content_part.done", "item_id": item["id"], "output_index": 0, "content_index": 0, "part": item["content"][0]}),
                                    (None, {"type": "response.output_item.done", "output_index": 0, "item": item}),
                                ])
                            for output_index, item in enumerate(body["output"]):
                                if item["type"] != "function_call":
                                    continue
                                pending = item | {"status": "in_progress", "arguments": ""}
                                events.extend([
                                    (None, {"type": "response.output_item.added", "output_index": output_index, "item": pending}),
                                    (None, {"type": "response.function_call_arguments.delta", "item_id": item["id"], "output_index": output_index, "delta": item["arguments"]}),
                                    (None, {"type": "response.function_call_arguments.done", "item_id": item["id"], "output_index": output_index, "arguments": item["arguments"]}),
                                    (None, {"type": "response.output_item.done", "output_index": output_index, "item": item}),
                                ])
                            events.append((None, {"type": "response.completed", "response": body}))
                            self._sse(events)
                        else:
                            self._send(200, body)
                    elif path == "/v1/messages":
                        turn = normalize_anthropic(payload, session)
                        result = app.service.execute(turn, tunnel_id=tunnel)
                        body = anthropic_response(result)
                        if turn.stream:
                            events = [("message_start", {"type": "message_start", "message": body | {"content": [], "stop_reason": None}})]
                            for index, block in enumerate(body["content"]):
                                start_block = {"type": "text", "text": ""}
                                if block["type"] == "tool_use":
                                    start_block = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
                                events.append(("content_block_start", {"type": "content_block_start", "index": index, "content_block": start_block}))
                                if block["type"] == "text":
                                    events.append(("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": block["text"]}}))
                                else:
                                    events.append(("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"], separators=(",", ":"))}}))
                                events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
                            events.extend([("message_delta", {"type": "message_delta", "delta": {"stop_reason": body["stop_reason"], "stop_sequence": None}, "usage": {"output_tokens": result.output_units}}), ("message_stop", {"type": "message_stop"})])
                            self._sse(events)
                        else:
                            self._send(200, body)
                    elif ":generateContent" in path or ":streamGenerateContent" in path:
                        model = path.split("/models/", 1)[1].split(":", 1)[0]
                        turn = normalize_gemini(payload, model, session)
                        result = app.service.execute(turn, tunnel_id=tunnel)
                        body = gemini_response(result)
                        if ":streamGenerateContent" in path:
                            self._sse([(None, body)])
                        else:
                            self._send(200, body)
                    else:
                        self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})
                except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
                    self._send(400, {"error": {"message": str(exc), "type": "invalid_request_error"}})
                except Exception as exc:
                    self._send(502, {"error": {"message": str(exc), "type": "gateway_error"}})

            def log_message(self, fmt: str, *args: Any) -> None:
                print(f"gateway: {fmt % args}")

        return Handler


def serve_gateway(host: str, port: int, root: Path | str, token: str | None = None) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        raise ValueError("a gateway token is required when binding outside loopback")
    app = GatewayApplication(GatewayService(root), token=token)
    server = ThreadingHTTPServer((host, port), app.handler())
    print(f"FancyGPT model gateway listening on http://{host}:{port}")
    server.serve_forever()
