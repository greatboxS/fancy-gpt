from __future__ import annotations

from collections import OrderedDict

import hashlib
import inspect
import json
import os
import re
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from .gateway_compaction import (
    CompactionImpossible,
    CompactionRecord,
    ContextBudget,
    plan_compaction,
    units as _units,
)
from .bridge import BrowserTurnCancelled
from .gateway_capabilities import (
    Modality,
    UnsupportedModalityError,
    capability_report,
    enforce_modalities,
    resolve_capability,
)
from .gateway_streaming import DeltaStreamError, TextDeltaStream
from .gateway_trace import TraceStage, TurnTrace, shape_of
from .gateway_usage import estimate_tokens
from .browser_errors import BrowserTurnError, classify_exception
from .gateway_state import (
    ConversationLocks,
    GatewayStateStore,
    IdempotencyConflict,
    RebindReason,
    TurnRecord,
    TurnState,
    UncertainSubmitError,
)
from .models import ModelRequest, RequestMode, RequestState
from .response_parser import parse_json_object
from .store import RequestStore
from .tunnels import TunnelManager


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


#: Tool names are reflected back to clients and used for dispatch, so they are
#: restricted to the character set every supported protocol agrees on.
_VALID_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_VALID_TOOL_CALL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

#: (pattern, replacement) pairs. Each replacement keeps the identifying label
#: so a scrubbed message stays diagnosable, and drops the value entirely.
_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "[redacted-api-key]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}=*"), "Bearer [redacted]"),
    (re.compile(r"(?i)\b(x-api-key|authorization|cookie|set-cookie|session[_-]?token|access[_-]?token|refresh[_-]?token)(\s*[:=]\s*)\S+"), r"\1\2[redacted]"),
)


def _accepts_progress(provider: Any) -> bool:
    """Whether a provider can report partial output while a turn runs."""
    try:
        return "on_progress" in inspect.signature(provider.execute).parameters
    except (TypeError, ValueError):
        return False


def _scrub(message: str) -> str:
    """Remove anything that looks like a credential before it is persisted.

    Gateway errors can quote provider responses and request headers, and those
    must never reach the ledger, artifacts, or logs.
    """
    cleaned = message
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned[:4000]


class ToolLoopExhausted(RuntimeError):
    """A session answered tool calls with tool calls past the configured bound."""

    def __init__(self, depth: int, limit: int) -> None:
        super().__init__(
            f"tool loop has run for {depth} consecutive turns on this session, "
            f"which is the configured limit of {limit}; answer with a final message "
            "or start a new session"
        )
        self.depth = depth
        self.limit = limit


class CrossSessionError(ValueError):
    """One gateway session tried to continue another session's turn."""

    def __init__(self, response_id: str, session_id: str) -> None:
        super().__init__(
            f"response {response_id} does not belong to session {session_id}; "
            "a gateway session may only continue its own provider conversation"
        )
        self.response_id = response_id
        self.session_id = session_id


class AmbiguousCorrelationError(ValueError):
    """A stateless transcript matched turns belonging to more than one session."""

    def __init__(self, sessions: list[str]) -> None:
        super().__init__(
            f"transcript matches {len(sessions)} gateway sessions; refusing to guess which "
            "provider conversation to continue. Send an explicit x-fancy-session-id header."
        )
        self.sessions = sessions


class GatewayContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    text: str
    #: Tool ids taken from the real protocol structure at normalization time.
    #: Compaction relies on these rather than re-parsing the rendered text,
    #: so untrusted message content cannot forge a tool-call pairing.
    tool_call_ids: list[str] = Field(default_factory=list)
    tool_result_ids: list[str] = Field(default_factory=list)


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
    tool_choice: str = "auto"
    stream: bool = False
    idempotency_key: str | None = None


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
    #: True when snapshot-to-delta conversion had to stop mid-turn. The
    #: final text here is still authoritative, but a client that accumulated
    #: deltas holds something that contradicts it.
    stream_failed: bool = False


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
    #: Digests of messages long enough to be distinctive. A short message
    #: like "ok" collides across unrelated clients, so only high-entropy
    #: ones may anchor a correlation.
    anchor_digests: list[str] = Field(default_factory=list)
    #: Which caller owns this session. Correlation never crosses it.
    client_key: str = ""


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

    def matching_predecessor(
        self, turn: NormalizedTurn, site: str, *, client_key: str = ""
    ) -> ContextLedger | None:
        """Recover the predecessor of a stateless client's transcript.

        Claude Code and the Gemini CLI resend their whole transcript each turn,
        and they compact it themselves. Requiring the stored transcript to be an
        exact prefix therefore breaks precisely when a session gets long: the
        client edits its own history, nothing matches, and every later turn opens
        a fresh browser chat.

        So correlation anchors on things that survive a client-side compaction:

        * a tool-call id the gateway issued, which the client must echo back;
        * the digest of the gateway's own last reply, if it is still present;
        * the digest of any *distinctive* earlier message.

        "Distinctive" is load bearing. A short message like "ok" or "continue"
        is identical across unrelated clients, so matching on it would attach one
        caller's transcript to another caller's browser conversation. Only
        high-entropy digests may anchor, and correlation never crosses a declared
        session or client.
        """
        incoming = {_message_digest(message) for message in turn.messages}
        anchors = {
            _message_digest(message)
            for message in turn.messages
            if message.role == "user" and _is_distinctive(message.text)
        }
        joined = "\n".join(message.text for message in turn.messages)
        matches: list[ContextLedger] = []
        for path in self.turns.glob("resp_*.json"):
            ledger = ContextLedger.model_validate_json(path.read_text(encoding="utf-8"))
            if ledger.protocol != turn.protocol or ledger.model != turn.model or ledger.site != site:
                continue
            if turn.session_id and ledger.session_id != turn.session_id:
                continue
            # A stored client key partitions the space even when the caller
            # supplied no session id.
            if client_key and ledger.client_key and ledger.client_key != client_key:
                continue
            reply_matches = bool(ledger.output_digest) and ledger.output_digest in incoming
            tool_matches = any(call_id and call_id in joined for call_id in ledger.tool_call_ids)
            anchor_matches = bool(anchors and set(ledger.anchor_digests) & anchors)
            if reply_matches or tool_matches or anchor_matches:
                matches.append(ledger)
        if not matches:
            return None
        sessions = {ledger.session_id for ledger in matches}
        if len(sessions) > 1:
            raise AmbiguousCorrelationError(sorted(sessions))
        return max(matches, key=lambda item: item.created_at)


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


#: Below this length a message is too common to identify a conversation.
#: "ok", "continue", "yes" recur across unrelated clients.
MIN_ANCHOR_CHARS = 64


def _is_distinctive(text: str) -> bool:
    """Whether a message is specific enough to anchor a correlation."""
    stripped = " ".join((text or "").split())
    if len(stripped) < MIN_ANCHOR_CHARS:
        return False
    # Repetition of one character carries little identifying information.
    return len(set(stripped)) >= 12


def _message_digest(message: GatewayContent) -> str:
    return _digest({"role": message.role, "text": message.text})


def _tool_ids(value: Any) -> tuple[list[str], list[str]]:
    """Collect tool call and tool result ids from a protocol content structure.

    This reads the typed protocol shape, never rendered prose, so text that
    merely looks like a tool line cannot be mistaken for one.
    """
    calls: list[str] = []
    results: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        if kind == "tool_use" and node.get("id"):
            calls.append(str(node["id"]))
        elif kind == "function_call" and (node.get("call_id") or node.get("id")):
            calls.append(str(node.get("call_id") or node.get("id")))
        elif kind == "tool_result" and node.get("tool_use_id"):
            results.append(str(node["tool_use_id"]))
        elif kind == "function_call_output" and node.get("call_id"):
            results.append(str(node["call_id"]))
        if isinstance(node.get("functionCall"), dict):
            call = node["functionCall"]
            calls.append(str(call.get("id") or call.get("name") or ""))
        if isinstance(node.get("functionResponse"), dict):
            response = node["functionResponse"]
            results.append(str(response.get("id") or response.get("name") or ""))
        for key in ("content", "parts", "input", "output"):
            if key in node:
                walk(node[key])

    walk(value)
    return [item for item in calls if item], [item for item in results if item]


def _content(role: str, value: Any) -> GatewayContent:
    """Build a normalized message, preserving structural tool identity."""
    calls, results = _tool_ids(value)
    return GatewayContent(role=role, text=_text(value), tool_call_ids=calls, tool_result_ids=results)


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


def _guard_modalities(payload: Any, *, protocol: str, model: str, where: str) -> None:
    """Reject attachments the resolved browser route cannot deliver.

    This runs on the raw protocol payload, before normalization flattens
    content into text, because after flattening the attachment is gone.
    """
    capability = resolve_capability(
        protocol=protocol, site=GatewayService.resolve_site(model), model=model
    )
    enforce_modalities(payload, capability, where=where)


_SESSION_HINT_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TOOL_REQUEST_VERBS = r"(?:use|run|call|invoke|execute|apply)"


def _openai_session_hint(payload: dict[str, Any]) -> str | None:
    """Use Codex's stable thread key when the caller did not send our header."""
    candidates = [payload.get("prompt_cache_key")]
    metadata = payload.get("client_metadata")
    if isinstance(metadata, dict):
        candidates.extend([metadata.get("thread_id"), metadata.get("session_id")])
    for value in candidates:
        if isinstance(value, str) and _SESSION_HINT_RE.fullmatch(value):
            return value
    return None


def _normalize_openai_tool_choice(value: Any) -> str:
    if isinstance(value, str) and value in {"auto", "none", "required"}:
        return value
    if isinstance(value, dict):
        kind = value.get("type")
        if kind == "function":
            name = value.get("name")
            if not name and isinstance(value.get("function"), dict):
                name = value["function"].get("name")
            if isinstance(name, str) and name:
                return f"function:{name}"
    return "auto"


def _explicit_tool_requests(turn: NormalizedTurn) -> list[str]:
    """Find unsatisfied user instructions that explicitly command a tool to run."""
    last_result = max(
        (index for index, message in enumerate(turn.messages) if message.tool_result_ids),
        default=-1,
    )
    user_text = "\n".join(
        message.text
        for index, message in enumerate(turn.messages)
        if index > last_result and message.role == "user"
    )
    requested: list[str] = []
    for tool in turn.tools:
        name = re.escape(tool.name)
        before = rf"\b{_TOOL_REQUEST_VERBS}\b[^\n]{{0,96}}\b{name}\b"
        after = rf"\b{name}\b[^\n]{{0,96}}\b{_TOOL_REQUEST_VERBS}\b"
        if re.search(before, user_text, flags=re.IGNORECASE) or re.search(after, user_text, flags=re.IGNORECASE):
            requested.append(tool.name)
    return requested


def _required_tool_names(turn: NormalizedTurn) -> list[str]:
    if turn.tool_choice.startswith("function:"):
        return [turn.tool_choice.split(":", 1)[1]]
    return _explicit_tool_requests(turn)


def _tool_call_required(turn: NormalizedTurn) -> bool:
    return bool(turn.tools) and (turn.tool_choice == "required" or bool(_required_tool_names(turn)))


def normalize_openai(payload: dict[str, Any], session_id: str | None = None) -> NormalizedTurn:
    model = payload.get("model", "fancy-chatgpt")
    _guard_modalities(payload.get("input"), protocol="openai", model=model, where="input")
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
                messages.append(
                    GatewayContent(
                        role="tool",
                        text=f"TOOL RESULT {item.get('call_id')}: {_text(item.get('output'))}",
                        tool_result_ids=[str(item["call_id"])] if item.get("call_id") else [],
                    )
                )
            else:
                messages.append(_content(str(item.get("role", "user")), item.get("content", item)))
    tools = []
    for tool in payload.get("tools") or []:
        if tool.get("type") == "function":
            tools.append(GatewayTool(name=tool.get("name", ""), description=tool.get("description"), parameters=tool.get("parameters") or {}))
    return NormalizedTurn(
        protocol="openai",
        model=model,
        instructions=_text(payload.get("instructions")),
        messages=messages,
        tools=tools,
        previous_response_id=payload.get("previous_response_id"),
        session_id=session_id or _openai_session_hint(payload),
        tool_choice=_normalize_openai_tool_choice(payload.get("tool_choice", "auto")),
        stream=bool(payload.get("stream")),
    )


def normalize_anthropic(payload: dict[str, Any], session_id: str | None = None) -> NormalizedTurn:
    model = payload.get("model", "fancy-chatgpt")
    _guard_modalities(payload.get("messages"), protocol="anthropic", model=model, where="messages")
    messages = [_content(str(item.get("role", "user")), item.get("content")) for item in payload.get("messages") or []]
    tools = [GatewayTool(name=item.get("name", ""), description=item.get("description"), parameters=item.get("input_schema") or {}) for item in payload.get("tools") or []]
    return NormalizedTurn(protocol="anthropic", model=model, instructions=_text(payload.get("system")), messages=messages, tools=tools, session_id=session_id, stream=bool(payload.get("stream")))


def normalize_gemini(payload: dict[str, Any], model: str, session_id: str | None = None) -> NormalizedTurn:
    _guard_modalities(payload.get("contents"), protocol="gemini", model=model, where="contents")
    messages = [
        _content("assistant" if item.get("role") == "model" else str(item.get("role", "user")), item.get("parts"))
        for item in payload.get("contents") or []
    ]
    tools: list[GatewayTool] = []
    for group in payload.get("tools") or []:
        for item in group.get("functionDeclarations") or group.get("function_declarations") or []:
            tools.append(GatewayTool(name=item.get("name", ""), description=item.get("description"), parameters=item.get("parameters") or {}))
    config = payload.get("generationConfig") or payload.get("generation_config") or {}
    return NormalizedTurn(protocol="gemini", model=model, instructions=_text(payload.get("systemInstruction") or payload.get("system_instruction")), messages=messages, tools=tools, session_id=session_id, stream=bool(config.get("stream")))


def _clip_web_text(value: str, limit: int, *, label: str) -> str:
    """Bound metadata copied into a browser composer, preserving both ends."""
    if len(value) <= limit:
        return value
    head = max(0, limit // 3)
    tail = max(0, limit - head)
    omitted = len(value) - head - tail
    return f"{value[:head]}\n[{label}: {omitted} chars omitted]\n{value[-tail:]}"


def _web_tool_catalog(tools: list[GatewayTool]) -> list[dict[str, Any]]:
    """Describe callable tools without pasting full client-owned JSON schemas."""
    catalog: list[dict[str, Any]] = []
    for tool in tools:
        properties = tool.parameters.get("properties") if isinstance(tool.parameters, dict) else None
        parameters = []
        if isinstance(properties, dict):
            required = set(tool.parameters.get("required") or [])
            for name, schema in list(properties.items())[:8]:
                kind = schema.get("type", "any") if isinstance(schema, dict) else "any"
                parameters.append({"name": name, "type": kind, "required": name in required})
        catalog.append({
            "name": tool.name,
            "description": _clip_web_text(tool.description or "", 80, label="description"),
            "parameters": parameters,
        })
    return catalog


TOOL_PROTOCOL_BEGIN = "<<<FANCY_GPT_TOOL_PROTOCOL:fancy-tool-v1>>>"
TOOL_PROTOCOL_END = "<<<END_FANCY_GPT_TOOL_PROTOCOL>>>"
BROWSER_PROMPT_MAX_CHARS = int(os.getenv("FANCY_GPT_BROWSER_MAX_PROMPT_CHARS", "700000"))


def _tool_protocol_block(turn: NormalizedTurn) -> str:
    """Render the versioned client-tool contract appended to every web turn."""
    tools = _web_tool_catalog(turn.tools)
    tools_json = _clip_web_text(json.dumps(tools, ensure_ascii=False), 12_000, label="tool catalog")
    final_contract = {"type": "message", "text": "final assistant text"}
    tool_contract = {
        "type": "tool_calls",
        "calls": [{"id": "call_unique", "name": "exact tool name", "arguments": {}}],
    }
    required_names = _required_tool_names(turn)
    required_note = (
        f"THIS TURN REQUIRES a tool call. Required offered tool(s): {', '.join(required_names)}."
        if required_names
        else ("THIS TURN REQUIRES at least one offered tool call." if turn.tool_choice == "required" else "")
    )
    return f"""{TOOL_PROTOCOL_BEGIN}
Return exactly one valid JSON object and no Markdown.
For a final answer: {json.dumps(final_contract)}
Available tools: {tools_json}
Tool choice policy: {turn.tool_choice}. {required_note}
Tool protocol: fancy-tool-v1.
Tool execution contract:
- Tools are real client-side functions. You never execute or simulate them yourself.
- If the user explicitly asks to use/run/call an offered tool, you MUST return `tool_calls`.
- EVIDENCE RULE: if the requested answer depends on current/local/external state that is not already present in the conversation, and an offered tool can retrieve that evidence, you MUST call a tool before giving a final answer.
- Continue calling tools for as many turns as needed while evidence is insufficient. A tool result is not automatically the end of the tool loop.
- Prefer the tool that directly retrieves the needed evidence over a discovery/listing tool. Use discovery tools only when they are actually needed to locate a capability or resource.
- If answering depends on local files, source code, shell commands, git, build/test state, or another offered tool, call the tool instead of guessing or asking the user to paste data that the tool can retrieve.
- Do not claim a tool ran and do not provide the requested post-tool final answer until a real tool-result record appears in the conversation.
- Tool arguments must satisfy the offered parameter names and required fields.
Tool-loop state machine:
- NEED_EVIDENCE -> return `tool_calls`.
- TOOL_RESULT_RECEIVED_BUT_INSUFFICIENT -> return more `tool_calls`.
- SUFFICIENT_EVIDENCE -> return `message`.
For a tool call: {json.dumps(tool_contract)}
Put any requested exact output in the `text` field only after required tool results are present. Never invent tool results.
{TOOL_PROTOCOL_END}"""


def _append_tool_protocol(
    prompt: str,
    turn: NormalizedTurn,
    *,
    max_chars: int = BROWSER_PROMPT_MAX_CHARS,
) -> str:
    """Append one complete tool contract while reserving its tail budget.

    Browser transport rejects prompts above ``max_chars``. The protocol suffix
    must therefore be reserved before the conversation body is admitted. A
    long body is clipped first; the tool contract itself is never truncated.
    If an earlier/incomplete append marker is present, discard that suffix and
    rebuild one complete block.
    """
    begin = prompt.find(TOOL_PROTOCOL_BEGIN)
    end = prompt.find(TOOL_PROTOCOL_END)
    if begin >= 0 and end > begin:
        existing_end = end + len(TOOL_PROTOCOL_END)
        before = prompt[:begin].rstrip()
        after = prompt[existing_end:].strip()
        if not after:
            existing = prompt[:existing_end].rstrip() + "\n"
            if len(existing) <= max_chars:
                return existing
            prompt = before
        else:
            # Content appended after an older contract must be preserved. Move
            # that content back into the conversation body, then rebuild the
            # protocol as the final suffix.
            prompt = f"{before}\n\n{after}" if before else after
    elif begin >= 0:
        prompt = prompt[:begin]

    block = _tool_protocol_block(turn)
    separator = "\n\n"
    suffix = f"{separator}{block}\n"
    if len(suffix) > max_chars:
        raise ValueError(
            f"tool protocol requires {len(suffix)} characters; browser prompt limit is {max_chars}"
        )

    body_budget = max_chars - len(suffix)
    base = prompt.rstrip()
    if len(base) > body_budget:
        base = _clip_web_text(base, body_budget, label="conversation body")
        if len(base) > body_budget:
            base = base[:body_budget]
    compiled = f"{base}{suffix}"
    if len(compiled) > max_chars:
        raise ValueError(
            f"compiled gateway prompt has {len(compiled)} characters; browser prompt limit is {max_chars}"
        )
    return compiled


def _gateway_prompt(turn: NormalizedTurn, *, include_history: bool) -> str:
    """Compile the browser prompt without per-message data loss.

    Context compaction happens before this renderer and is the only layer that
    may deliberately remove semantic transcript units.  The renderer therefore
    keeps every surviving message byte-for-byte instead of imposing the old
    4k-per-message / 2k-system caps.  The browser writer handles large prompts
    in chunks; ``_append_tool_protocol`` reserves the final protocol tail and
    applies the absolute browser safety bound only if the already-compacted
    request still cannot fit.
    """
    messages = turn.messages if include_history else turn.messages[-1:]
    transcript = "\n\n".join(
        f"{message.role.upper()}: {message.text}"
        for message in messages
    )
    instructions = turn.instructions if turn.instructions else "(none)"
    body = f"""Respond to this conversation.
SYSTEM: {instructions}
{transcript}"""
    return _append_tool_protocol(body, turn)


class GatewayOverloaded(RuntimeError):
    """Backpressure: the gateway is at capacity and the caller should retry."""

    def __init__(self, message: str, retry_after: int = 5) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class GatewayCancelled(RuntimeError):
    """The turn was cancelled before it completed."""

    def __init__(self, reason: str = "client cancelled the request") -> None:
        super().__init__(reason)
        self.reason = reason


class CancelToken:
    """Cooperative cancellation for a single turn."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason = "client cancelled the request"

    def cancel(self, reason: str = "client cancelled the request") -> None:
        self.reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise GatewayCancelled(self.reason)


@dataclass
class GatewayLimits:
    """Bounded resources. Every limit is enforced before work is admitted."""

    max_active_turns: int = 8
    max_active_turns_per_client: int = 4
    max_queue_depth: int = 32
    max_body_bytes: int = 10 * 1024 * 1024
    max_tools: int = 128
    max_tool_schema_bytes: int = 64 * 1024
    max_tool_argument_bytes: int = 256 * 1024
    max_output_bytes: int = 4 * 1024 * 1024
    max_tool_loop_iterations: int = 32

    @classmethod
    def from_env(cls) -> "GatewayLimits":
        def read(name: str, default: int) -> int:
            try:
                return max(1, int(os.getenv(name, str(default))))
            except ValueError:
                return default

        return cls(
            max_active_turns=read("FANCY_GPT_GATEWAY_MAX_ACTIVE", 8),
            max_active_turns_per_client=read("FANCY_GPT_GATEWAY_MAX_ACTIVE_PER_CLIENT", 4),
            max_queue_depth=read("FANCY_GPT_GATEWAY_MAX_QUEUE", 32),
            max_body_bytes=read("FANCY_GPT_GATEWAY_MAX_BODY_BYTES", 10 * 1024 * 1024),
            max_tools=read("FANCY_GPT_GATEWAY_MAX_TOOLS", 128),
            max_tool_schema_bytes=read("FANCY_GPT_GATEWAY_MAX_TOOL_SCHEMA_BYTES", 64 * 1024),
            max_tool_argument_bytes=read("FANCY_GPT_GATEWAY_MAX_TOOL_ARG_BYTES", 256 * 1024),
            max_output_bytes=read("FANCY_GPT_GATEWAY_MAX_OUTPUT_BYTES", 4 * 1024 * 1024),
            max_tool_loop_iterations=read("FANCY_GPT_GATEWAY_MAX_TOOL_LOOP", 32),
        )


class GatewayMetrics:
    """Counters surfaced by the metrics endpoint."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.queued = 0
        self.running = 0
        self.completed = 0
        self.rejected = 0
        self.cancelled = 0
        self.failed = 0

    @contextmanager
    def queue_slot(self) -> Any:
        with self._lock:
            self.queued += 1
        try:
            yield
        finally:
            with self._lock:
                self.queued -= 1

    @contextmanager
    def run_slot(self) -> Any:
        with self._lock:
            self.queued = max(0, self.queued - 1)
            self.running += 1
        try:
            yield
        finally:
            with self._lock:
                self.running -= 1
                self.queued += 1

    def record(self, name: str) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + 1)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "queued": max(0, self.queued),
                "running": self.running,
                "completed": self.completed,
                "rejected": self.rejected,
                "cancelled": self.cancelled,
                "failed": self.failed,
            }


class GatewayService:
    def __init__(
        self,
        root: Path | str,
        manager: TunnelManager | None = None,
        limits: GatewayLimits | None = None,
    ) -> None:
        self.store = GatewayStore(root)
        self.state = GatewayStateStore(root)
        self.requests = RequestStore(Path(root))
        # An absolute backstop, not the real limit. A long reasoning turn can
        # legitimately outrun any constant, so the adapter gives up on the
        # page being *inactive* instead; this only catches a turn that never
        # ends at all.
        self.manager = manager or TunnelManager(
            timeout_s=float(os.getenv("FANCY_GPT_BROWSER_TIMEOUT", "1800"))
        )
        self.limits = limits or GatewayLimits.from_env()
        self.metrics = GatewayMetrics()
        self.locks = ConversationLocks()
        self.max_input_units = int(os.getenv("FANCY_GPT_GATEWAY_MAX_INPUT_UNITS", "200000"))
        self.compaction_reserve_output = int(os.getenv("FANCY_GPT_GATEWAY_RESERVE_OUTPUT_UNITS", "8000"))
        self.compaction_reserve_tool_loop = int(os.getenv("FANCY_GPT_GATEWAY_RESERVE_TOOL_LOOP_UNITS", "8000"))
        self._admission = threading.BoundedSemaphore(self.limits.max_active_turns)
        self._client_guard = threading.Lock()
        self._client_active: dict[str, int] = {}
        self._cancel_guard = threading.Lock()
        self._cancel_tokens: dict[str, CancelToken] = {}
        self._delta_streams: dict[str, TextDeltaStream] = {}
        # Bounded, and ordered so the oldest goes first. A gateway is a
        # long-lived server: an unbounded dict of traces, each holding up to
        # 500 events, grows for as long as the process runs. Every trace is
        # also appended to its file as it happens, so what is evicted here is
        # still on disk for anyone diagnosing a past turn.
        self._traces: OrderedDict[str, TurnTrace] = OrderedDict()

    # -- routing -------------------------------------------------------------

    @staticmethod
    def resolve_site(model: str) -> str:
        """Map a model alias to a model website. Never consults the tunnel."""
        name = model.lower()
        # Public aliases are namespaced so they cannot be confused with a
        # vendor model accidentally sent to this local gateway. Keep the old
        # suffix form readable for existing saved sessions during migration.
        if name.startswith("fancy-gemini") or name.startswith("gemini-web"):
            return "gemini"
        if name.startswith(("fancy-chatgpt", "fancy-claude", "chatgpt-web", "claude-web")):
            return "chatgpt"
        raise ValueError(f"unsupported gateway model: {model}")

    # -- cancellation --------------------------------------------------------

    def cancel(self, response_id: str, reason: str = "client cancelled the request") -> bool:
        with self._cancel_guard:
            token = self._cancel_tokens.get(response_id)
        if token is None:
            return False
        token.cancel(reason)
        return True

    @contextmanager
    def _cancellation(self, response_id: str, token: CancelToken) -> Any:
        with self._cancel_guard:
            self._cancel_tokens[response_id] = token
        try:
            yield
        finally:
            with self._cancel_guard:
                self._cancel_tokens.pop(response_id, None)

    # -- admission control ---------------------------------------------------

    @contextmanager
    def _admit(self, client_key: str) -> Any:
        """Bounded admission. Rejects rather than queueing without limit."""
        snapshot = self.metrics.snapshot()
        if snapshot["queued"] >= self.limits.max_queue_depth:
            self.metrics.record("rejected")
            raise GatewayOverloaded("gateway queue depth exceeded", retry_after=10)
        with self._client_guard:
            active = self._client_active.get(client_key, 0)
            if active >= self.limits.max_active_turns_per_client:
                self.metrics.record("rejected")
                raise GatewayOverloaded("too many concurrent turns for this client", retry_after=5)
            self._client_active[client_key] = active + 1
        try:
            with self.metrics.queue_slot():
                if not self._admission.acquire(timeout=float(os.getenv("FANCY_GPT_GATEWAY_ADMIT_TIMEOUT", "30"))):
                    self.metrics.record("rejected")
                    raise GatewayOverloaded("gateway is at capacity", retry_after=10)
                try:
                    with self.metrics.run_slot():
                        yield
                finally:
                    self._admission.release()
        finally:
            with self._client_guard:
                remaining = self._client_active.get(client_key, 1) - 1
                if remaining <= 0:
                    self._client_active.pop(client_key, None)
                else:
                    self._client_active[client_key] = remaining

    # -- validation ----------------------------------------------------------

    def _validate(self, turn: NormalizedTurn) -> None:
        if len(turn.tools) > self.limits.max_tools:
            raise ValueError(f"too many tools: {len(turn.tools)} > {self.limits.max_tools}")
        for tool in turn.tools:
            if not tool.name or not _VALID_TOOL_NAME.match(tool.name):
                raise ValueError(f"invalid tool name: {tool.name!r}")
            schema_size = len(json.dumps(tool.parameters, ensure_ascii=False))
            if schema_size > self.limits.max_tool_schema_bytes:
                raise ValueError(
                    f"tool {tool.name} schema is {schema_size} bytes, limit is {self.limits.max_tool_schema_bytes}"
                )

    # -- replay --------------------------------------------------------------

    def _stored_result(self, response_id: str) -> GatewayResult | None:
        path = self.requests.request_dir(response_id) / "gateway-response.json"
        if not path.exists():
            return None
        return GatewayResult.model_validate_json(path.read_text(encoding="utf-8"))

    # -- predecessor correlation --------------------------------------------

    def _resolve_predecessor(
        self, turn: NormalizedTurn, site: str, *, client_key: str = ""
    ) -> ContextLedger | None:
        """Find the turn this one continues, proving ownership before trusting it."""
        if turn.previous_response_id:
            ledger = self.store.load(turn.previous_response_id)
            # A response id is a bearer reference, so a caller that declares a
            # session may only continue its own turns.
            if turn.session_id and ledger.session_id != turn.session_id:
                raise CrossSessionError(turn.previous_response_id, turn.session_id)
            return ledger
        if turn.session_id:
            latest = self.store.latest(turn.session_id)
            if latest is not None:
                return latest
        return self.store.matching_predecessor(turn, site, client_key=client_key)

    # -- main entry ----------------------------------------------------------

    # Traces kept in memory for turns that may still be asked about. Older ones
    # are read back from their files instead.
    TRACE_CACHE_SIZE = 256

    def _remember_trace(self, response_id: str, trace: TurnTrace) -> None:
        self._traces[response_id] = trace
        self._traces.move_to_end(response_id)
        while len(self._traces) > self.TRACE_CACHE_SIZE:
            self._traces.popitem(last=False)

    def execute(
        self,
        turn: NormalizedTurn,
        *,
        tunnel_id: str | None = None,
        idempotency_key: str | None = None,
        cancel_token: CancelToken | None = None,
        client_key: str = "default",
        on_start: Callable[[str], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> GatewayResult:
        site = self.resolve_site(turn.model)
        self._validate(turn)
        key = idempotency_key or turn.idempotency_key
        trace = TurnTrace("pending")

        previous = self._resolve_predecessor(turn, site, client_key=client_key)
        if previous and previous.site != site:
            raise ValueError("previous response belongs to a different model site")
        session_id = turn.session_id or (previous.session_id if previous else f"gw_{uuid.uuid4().hex[:16]}")

        payload_digest = _digest(turn.model_dump(mode="json", exclude={"idempotency_key"}))
        response_id = f"resp_{uuid.uuid4().hex}"
        attempt = 1
        retry_of: str | None = None
        trace = TurnTrace(response_id, self.requests.request_dir(response_id) / "trace.jsonl")
        self._remember_trace(response_id, trace)
        trace.event(
            TraceStage.ROUTE, "resolved", f"{turn.protocol} -> {site}",
            protocol=turn.protocol, model=turn.model, site=site,
            tools=len(turn.tools), messages=len(turn.messages),
            stream=turn.stream, client=client_key,
        )
        trace.event(
            TraceStage.CORRELATION,
            "predecessor-found" if previous else "no-predecessor",
            previous.response_id if previous else "starting a new provider chat",
            session=session_id,
            declared_session=bool(turn.session_id),
            via="previous_response_id" if turn.previous_response_id else "transcript",
            conversation=previous.conversation_id if previous else None,
        )

        if key:
            disposition, claim = self.state.claim_idempotency(key, payload_digest, response_id)
            trace.event(TraceStage.IDEMPOTENCY, disposition, claim.response_id)
            if disposition == "replay":
                replayed = self._stored_result(claim.response_id)
                if replayed is not None:
                    trace.event(TraceStage.TERMINAL, "replayed", "returned the stored result")
                    return replayed
                record = self.state.load_turn(claim.response_id)
                if record is not None and record.resumable:
                    raise UncertainSubmitError(claim.response_id, record.state)
                # The prior attempt never reached the provider, so this is a
                # genuine retry: a new record linked to the one it supersedes,
                # rather than silently reusing the old id.
                if record is not None:
                    attempt = record.attempt + 1
                    retry_of = record.response_id
                    trace.event(
                        TraceStage.IDEMPOTENCY, "retry", claim.response_id, attempt=attempt,
                    )

        # A turn that may already be sitting in the provider chat must not be
        # duplicated by a fresh submit on the same session.
        for pending in self.state.unresolved_turns(session_id):
            if pending.response_id != response_id:
                trace.event(
                    TraceStage.TERMINAL, "blocked-by-uncertain-turn", pending.response_id,
                    state=pending.state.value,
                )
                raise UncertainSubmitError(pending.response_id, pending.state)

        binding = self.state.bind(session_id=session_id, site=site, model=turn.model, protocol=turn.protocol)
        # A client that keeps answering tool calls with more tool calls would
        # otherwise run until something else stopped it.
        if binding.tool_loop_depth >= self.limits.max_tool_loop_iterations:
            trace.event(
                TraceStage.ADMISSION, "tool-loop-exhausted",
                f"{binding.tool_loop_depth} consecutive tool-call turns",
                limit=self.limits.max_tool_loop_iterations,
            )
            raise ToolLoopExhausted(binding.tool_loop_depth, self.limits.max_tool_loop_iterations)
        conversation_id = binding.conversation_id or (previous.conversation_id if previous else None)
        trace.event(
            TraceStage.CORRELATION, "bound", conversation_id or "no provider conversation yet",
            generation=binding.generation, rebind=binding.rebind_reason.value if binding.rebind_reason else None,
        )
        token = cancel_token or CancelToken()

        record = self.state.save_turn(
            TurnRecord(
                response_id=response_id,
                session_id=session_id,
                protocol=turn.protocol,
                model=turn.model,
                site=site,
                payload_digest=payload_digest,
                idempotency_key=key,
                predecessor_response_id=previous.response_id if previous else None,
                conversation_id=conversation_id,
                attempt=attempt,
                retry_of=retry_of,
            )
        )
        self.state.transition(record, TurnState.QUEUED, "admitted to gateway queue")

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
            with self._cancellation(response_id, token), self._admit(client_key):
                if on_start is not None:
                    on_start(response_id)
                try:
                    return self._run_turn(
                        turn=turn,
                        site=site,
                        session_id=session_id,
                        response_id=response_id,
                        previous=previous,
                        record=record,
                        token=token,
                        tunnel_id=tunnel_id,
                        key=key,
                        client_key=client_key,
                        on_delta=on_delta,
                    )
                finally:
                    # However the turn ended. It used to be released only on the
                    # way out of a successful one, so every cancelled, failed,
                    # rejected or malformed turn left its stream -- holding the
                    # whole text emitted so far -- in the dict for the life of
                    # the server.
                    self._delta_streams.pop(response_id, None)
        except GatewayCancelled as exc:
            trace.event(TraceStage.TERMINAL, "cancelled", exc.reason)
            self.metrics.record("cancelled")
            self.state.transition(record, TurnState.CANCELLED, exc.reason)
            if key:
                self.state.release_idempotency(key)
            self.requests.fail(response_id, f"cancelled: {exc.reason}")
            raise
        except UncertainSubmitError:
            raise
        except GatewayOverloaded as exc:
            trace.event(TraceStage.ADMISSION, "rejected", str(exc))
            if key:
                self.state.release_idempotency(key)
            self.requests.fail(response_id, "rejected: gateway at capacity")
            raise

    def _run_turn(
        self,
        *,
        turn: NormalizedTurn,
        site: str,
        session_id: str,
        response_id: str,
        previous: ContextLedger | None,
        record: TurnRecord,
        token: CancelToken,
        tunnel_id: str | None,
        key: str | None,
        client_key: str = "",
        on_delta: Callable[[str], None] | None = None,
    ) -> GatewayResult:
        trace = self._traces.get(response_id) or TurnTrace(response_id)
        token.raise_if_cancelled()
        try:
            turn, compaction = self._compact(turn)
        except CompactionImpossible as exc:
            trace.event(TraceStage.COMPACTION, "impossible", str(exc))
            self._fail(record, response_id, key, str(exc))
            raise
        if compaction.applied:
            trace.event(
                TraceStage.COMPACTION, "applied",
                f"dropped {len(compaction.dropped)} of {compaction.source_message_count} messages",
                generation=compaction.generation,
                units_before=compaction.units_before, units_after=compaction.units_after,
                source_digest=compaction.source_digest[:16],
            )
        prompt = _gateway_prompt(turn, include_history=previous is None)
        # Clients act on these numbers - Claude Code uses them to decide when
        # to compact - so the estimate deliberately over-counts.
        input_units = estimate_tokens(prompt)
        try:
            if input_units > self.max_input_units:
                raise ValueError(
                    f"gateway input exceeds context budget: {input_units} > {self.max_input_units} units"
                )
            selection = self.manager.select(tunnel_id=tunnel_id, policy="auto", require_automatic=True)
            provider = self.manager.provider(selection)
        except Exception as exc:
            trace.event(TraceStage.ROUTE, "unavailable", str(exc))
            self._fail(record, response_id, key, str(exc))
            raise
        trace.event(
            TraceStage.ROUTE, "tunnel-selected", selection.tunnel_id,
            provider=provider.name, input_units=input_units,
            prompt=shape_of(prompt),
        )

        self.requests.update_status(
            response_id,
            state=RequestState.RUNNING_FINAL,
            tunnel_id=selection.tunnel_id,
            provider=provider.name,
        )

        binding = self.state.load_binding(session_id)
        conversation_id = (binding.conversation_id if binding else None) or (
            previous.conversation_id if previous else None
        )
        # The browser reports its whole reply so far, repeatedly. The delta
        # stream turns that into append-only text for the client.
        deltas = TextDeltaStream()
        self._delta_streams[response_id] = deltas

        def progress_sink(snapshot: str) -> None:
            if on_delta is None:
                return
            try:
                delta = deltas.push(snapshot)
            except DeltaStreamError as exc:
                # Already-sent text cannot be retracted, so the stream stops
                # rather than contradicting the client. The turn itself carries
                # on; the caller still receives the authoritative final result.
                trace.event(TraceStage.STREAM, "desynchronized", str(exc), emitted=len(exc.emitted))
                return
            if delta:
                trace.event(TraceStage.STREAM, "delta", "", chars=len(delta))
                on_delta(delta)

        request = ModelRequest(
            request_id=response_id,
            stage="agent",
            title=f"Gateway {turn.protocol} turn",
            prompt=prompt,
            response_schema={},
            metadata={
                "site": site,
                "conversation_id": conversation_id,
                "conversation_mode": "persistent",
                # Each browser job gets a non-zero fencing value. Today task tabs
                # are not reused, but carrying a real value keeps cancellation
                # correct if pooling is introduced later.
                "generation_epoch": time.time_ns(),
            },
        )

        # Serialize on the provider conversation, not globally: two different
        # chats may run at the same time, the same chat never may.
        lock_key = f"{site}:{conversation_id}" if conversation_id else f"session:{session_id}"
        token.raise_if_cancelled()
        try:
            with self.locks.acquire(lock_key):
                token.raise_if_cancelled()
                # Re-read the binding now that the lock is held. An earlier turn
                # on this session may have bound the provider conversation while
                # this turn was waiting; using the stale value would start a
                # second browser chat and fork the session.
                bound = self.state.load_binding(session_id)
                if bound is not None and bound.conversation_id and not conversation_id:
                    conversation_id = bound.conversation_id
                    request.metadata["conversation_id"] = conversation_id
                trace.event(TraceStage.LOCK, "acquired", lock_key)
                self.state.transition(record, TurnState.SUBMITTING, f"submitting on {lock_key}")
                provider.start()
                try:
                    self.state.transition(record, TurnState.SUBMITTED, "prompt handed to provider")
                    # execute() blocks, so cancellation is carried into the
                    # browser by a watcher that names the in-flight bridge turn
                    # on its own connection. Stopping generation makes the
                    # content script finish, which unblocks execute() normally.
                    with self._cancel_watcher(provider, token):
                        # Progress is only requested when the caller is actually
                        # streaming, and only from a provider that reports it.
                        if on_delta is not None and _accepts_progress(provider):
                            raw = provider.execute(request, on_progress=progress_sink)
                        else:
                            raw = provider.execute(request)
                    trace.event(
                        TraceStage.BROWSER, "answered", "provider returned a reply",
                        reply=shape_of(raw.raw_text), conversation=raw.conversation_id,
                    )
                    self.state.transition(record, TurnState.OBSERVING, "awaiting provider completion")
                    # Bind the provider conversation while the lock is still
                    # held. If this waited until after release, a queued turn on
                    # the same session would still see an unbound session and
                    # open a second browser chat.
                    self.state.attach_conversation(session_id, raw.conversation_id)
                except GatewayCancelled:
                    raise
                except BrowserTurnCancelled as exc:
                    # The browser confirmed it stopped. That is a clean
                    # cancellation, not an uncertain submit: we know exactly
                    # how far it got.
                    raise GatewayCancelled(token.reason or exc.reason) from exc
                except Exception as exc:
                    if isinstance(exc, RuntimeError) and "bound to provider conversation" in str(exc):
                        # The provider answered on a chat this session is not
                        # bound to: the binding is no longer trustworthy. This
                        # is a correlation fault, not a browser fault.
                        self.state.rebind(session_id, RebindReason.UNTRUSTED)
                        trace.event(TraceStage.CORRELATION, "conversation-drift", _scrub(str(exc)))
                        self._fail(record, response_id, key, str(exc))
                        raise
                    # Everything else is the browser or the page failing, and is
                    # classified so the caller learns whether retrying can help
                    # and whether a human has to act. The prompt may or may not
                    # have landed, so the turn is parked as uncertain rather
                    # than retried blindly.
                    failure = classify_exception(exc)
                    trace.event(
                        TraceStage.BROWSER, "failed", _scrub(str(exc)),
                        failure=failure.failure.value, retryable=failure.retryable,
                        needs_user_action=failure.needs_user_action,
                        guidance=failure.guidance,
                    )
                    self.state.transition(
                        record, TurnState.UNCERTAIN,
                        f"{failure.failure.value}: {_scrub(str(exc))}",
                    )
                    self.requests.fail(response_id, f"{failure.failure.value}: {_scrub(str(exc))}")
                    raise BrowserTurnError(_scrub(str(exc)), failure) from exc
                finally:
                    provider.stop()
            try:
                value = parse_json_object(raw.raw_text)
            except ValueError:
                # Browser models occasionally ignore the envelope instruction
                # and answer in plain text. Treating that as final prose is
                # safe: only a validated JSON tool_calls envelope can cause a
                # client-side tool execution.
                value = {"type": "message", "text": raw.raw_text.strip()}
        except (GatewayCancelled, UncertainSubmitError):
            raise
        except Exception as exc:
            # An uncertain turn has already been recorded, and must keep that
            # state: downgrading it to "failed" would invite a blind retry.
            if record.state is not TurnState.UNCERTAIN:
                self._fail(record, response_id, key, str(exc))
            raise

        token.raise_if_cancelled()
        if len(raw.raw_text) > self.limits.max_output_bytes:
            self._fail(record, response_id, key, "provider output exceeds the configured limit")
            raise ValueError("provider output exceeds the configured limit")

        try:
            text, calls = self._parse_envelope(value, turn)
        except Exception as exc:
            failure = classify_exception(exc)
            trace.event(
                TraceStage.BROWSER, "envelope-rejected", _scrub(str(exc)),
                failure=failure.failure.value, retryable=failure.retryable,
            )
            self._fail(record, response_id, key, str(exc))
            raise

        if on_delta is not None and text and not deltas.failed:
            try:
                remainder = deltas.finish(text)
            except DeltaStreamError:
                remainder = ""
            if remainder:
                on_delta(remainder)
        result = GatewayResult(
            response_id=response_id,
            session_id=session_id,
            model=turn.model,
            site=site,
            text=text,
            stream_failed=deltas.failed,
            tool_calls=calls,
            conversation_id=raw.conversation_id,
            created_at=_now(),
            input_units=input_units,
            output_units=estimate_tokens(raw.raw_text),
        )
        response_path = self.requests.write_text(
            response_id, "gateway-response.json", result.model_dump_json(indent=2)
        )
        self.requests.update_status(
            response_id,
            state=RequestState.COMPLETE,
            final_response_file=str(response_path),
            conversation_id=raw.conversation_id,
        )
        self.store.save(
            ContextLedger(
                response_id=response_id,
                session_id=session_id,
                protocol=turn.protocol,
                model=turn.model,
                site=site,
                request_digest=_digest(turn.model_dump(mode="json")),
                instructions_digest=_digest(turn.instructions),
                conversation_id=raw.conversation_id,
                previous_response_id=previous.response_id if previous else None,
                created_at=result.created_at,
                input_units=result.input_units,
                output_units=result.output_units,
                tool_call_ids=[call.id for call in calls],
                message_digests=[_message_digest(message) for message in turn.messages],
                anchor_digests=[
                    _message_digest(message)
                    for message in turn.messages
                    if message.role == "user" and _is_distinctive(message.text)
                ],
                client_key=client_key,
                output_digest=_message_digest(GatewayContent(role="assistant", text=text)) if text else "",
                compaction_generation=compaction.generation if compaction.applied else 0,
            )
        )
        if compaction.applied:
            # Provenance is persisted next to the turn so a later reader can
            # tell exactly what was dropped and why.
            self.requests.write_text(
                response_id, "gateway-compaction.json", compaction.model_dump_json(indent=2)
            )
        depth = self.state.record_turn_outcome(session_id, made_tool_call=bool(calls))
        trace.event(
            TraceStage.TERMINAL, "completed", f"{len(text)} chars, {len(calls)} tool call(s)",
            input_units=result.input_units, output_units=result.output_units,
            stream_failed=deltas.failed, realignments=deltas.realignments,
            conversation=raw.conversation_id, tool_loop_depth=depth,
        )
        self.state.transition(record, TurnState.COMPLETED, "provider answered")
        if key:
            self.state.update_idempotency(key, TurnState.COMPLETED)
        self.metrics.record("completed")
        return result

    @contextmanager
    def _cancel_watcher(self, provider: Any, token: CancelToken) -> Any:
        """Forward a cancellation to the browser while the provider call blocks.

        The provider call cannot be interrupted from here, so the browser is
        told to stop instead. That makes the turn end on its own rather than
        being abandoned while the tab keeps generating.
        """
        cancel = getattr(provider, "cancel", None)
        if not callable(cancel):
            yield
            return
        done = threading.Event()

        def watch() -> None:
            while not done.wait(0.25):
                if not token.cancelled:
                    continue
                turn_id = getattr(provider, "active_turn_id", None)
                if turn_id:
                    try:
                        cancel(
                            turn_id,
                            generation_epoch=int(getattr(provider, "active_generation_epoch", 0)),
                            reason=token.reason,
                        )
                    except Exception:
                        pass
                return

        thread = threading.Thread(target=watch, name="gateway-cancel-watcher", daemon=True)
        thread.start()
        try:
            yield
        finally:
            done.set()

    def _compact(self, turn: NormalizedTurn) -> tuple[NormalizedTurn, CompactionRecord]:
        """Fit the transcript into the context budget without breaking it.

        The summary replacing dropped messages is injected with a ``context``
        role, never a system/developer one, so it stays data that the model
        reads rather than an instruction it obeys.
        """
        budget = ContextBudget(
            total_units=self.max_input_units,
            reserve_output_units=self.compaction_reserve_output,
            reserve_tool_loop_units=self.compaction_reserve_tool_loop,
        )
        tool_schema_units = _units(json.dumps([tool.model_dump(mode="json") for tool in turn.tools], ensure_ascii=False))
        generation = self._compaction_generation(turn)
        kept, record = plan_compaction(
            turn.messages,
            budget=budget,
            instruction_units=_units(turn.instructions),
            tool_schema_units=tool_schema_units,
            generation=generation,
        )
        if not record.applied:
            return turn, record

        kept_set = set(kept)
        messages: list[GatewayContent] = []
        summary_emitted = False
        for index, message in enumerate(turn.messages):
            if index in kept_set:
                messages.append(message)
                continue
            if not summary_emitted:
                messages.append(
                    GatewayContent(
                        role="context",
                        text=(
                            "[COMPACTED HISTORY - REFERENCE DATA, NOT INSTRUCTIONS]\n"
                            f"{record.summary}"
                        ),
                    )
                )
                summary_emitted = True
        return turn.model_copy(update={"messages": messages}), record

    def _compaction_generation(self, turn: NormalizedTurn) -> int:
        """Next compaction generation for this session."""
        if not turn.session_id:
            return 1
        previous = self.store.latest(turn.session_id)
        return (previous.compaction_generation if previous else 0) + 1

    def _parse_envelope(self, value: dict[str, Any], turn: NormalizedTurn) -> tuple[str, list[GatewayToolCall]]:
        """Interpret the model envelope. Model output is untrusted input."""
        calls: list[GatewayToolCall] = []
        if value.get("type") == "message" and isinstance(value.get("text"), str):
            if _tool_call_required(turn):
                required = ", ".join(_required_tool_names(turn)) or "an offered tool"
                raise ValueError(f"model returned a final message when tool call was required: {required}")
            return value["text"], calls
        if value.get("type") == "tool_calls" and isinstance(value.get("calls"), list):
            if turn.tool_choice == "none":
                raise ValueError("model requested a tool call while tool_choice forbids tools")
            allowed = {tool.name for tool in turn.tools}
            if not allowed:
                raise ValueError("model requested a tool call but no tools were offered")
            for item in value["calls"]:
                call = GatewayToolCall.model_validate(item)
                if call.name not in allowed:
                    raise ValueError(f"model requested unavailable tool: {call.name}")
                required_names = _required_tool_names(turn)
                if required_names and call.name not in required_names:
                    raise ValueError(
                        f"model requested {call.name!r} but this turn requires: {', '.join(required_names)}"
                    )
                tool = next(item for item in turn.tools if item.name == call.name)
                required_args = tool.parameters.get("required", []) if isinstance(tool.parameters, dict) else []
                missing = [name for name in required_args if name not in call.arguments]
                if missing:
                    raise ValueError(
                        f"tool {call.name} is missing required argument(s): {', '.join(map(str, missing))}"
                    )
                if not _VALID_TOOL_CALL_ID.match(call.id):
                    raise ValueError(f"model returned an invalid tool call id: {call.id!r}")
                size = len(json.dumps(call.arguments, ensure_ascii=False))
                if size > self.limits.max_tool_argument_bytes:
                    raise ValueError(f"tool call arguments exceed {self.limits.max_tool_argument_bytes} bytes")
                calls.append(call)
            return "", calls
        raise ValueError("gateway model returned an invalid response envelope")

    def _fail(self, record: TurnRecord, response_id: str, key: str | None, message: str) -> None:
        scrubbed = _scrub(message)
        self.state.transition(record, TurnState.FAILED, scrubbed)
        if key:
            # The claim is kept rather than released. Same key plus same payload
            # is the same logical operation whether or not it succeeded, so the
            # next use of it is recorded as a further attempt with its lineage
            # intact, instead of looking like a brand new turn.
            self.state.update_idempotency(key, TurnState.FAILED)
        self.metrics.record("failed")
        self.requests.fail(response_id, scrubbed)


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


class OpenAIStream:
    """Responses API events, emitted as the answer actually arrives.

    The message output item is not opened until the first delta, because a turn
    that ends in a tool call has no message item at all.
    """

    def __init__(self, response_id: str, model: str) -> None:
        self.response_id = response_id
        self.model = model
        self.item_id = f"msg_{uuid.uuid4().hex[:16]}"
        self.sequence = 0
        self.text = ""
        self.opened = False

    def _event(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        payload["sequence_number"] = self.sequence
        self.sequence += 1
        return payload["type"], payload

    def _skeleton(self, status: str) -> dict[str, Any]:
        return {
            "id": self.response_id, "object": "response",
            "created_at": int(datetime.now().timestamp()), "status": status,
            "model": self.model, "output": [],
        }

    def open(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            self._event({"type": "response.created", "response": self._skeleton("in_progress")}),
            self._event({"type": "response.in_progress", "response": self._skeleton("in_progress")}),
        ]

    def delta(self, text: str) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        if not self.opened:
            self.opened = True
            item = {"id": self.item_id, "type": "message", "role": "assistant",
                    "status": "in_progress", "content": []}
            events.append(self._event({"type": "response.output_item.added", "output_index": 0, "item": item}))
            events.append(self._event({
                "type": "response.content_part.added", "item_id": self.item_id,
                "output_index": 0, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }))
        self.text += text
        events.append(self._event({
            "type": "response.output_text.delta", "item_id": self.item_id,
            "output_index": 0, "content_index": 0, "delta": text,
        }))
        return events

    def close(self, body: dict[str, Any], result: GatewayResult) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        if self.opened:
            part = {"type": "output_text", "text": self.text, "annotations": []}
            events.append(self._event({
                "type": "response.output_text.done", "item_id": self.item_id,
                "output_index": 0, "content_index": 0, "text": self.text,
            }))
            events.append(self._event({
                "type": "response.content_part.done", "item_id": self.item_id,
                "output_index": 0, "content_index": 0, "part": part,
            }))
            events.append(self._event({
                "type": "response.output_item.done", "output_index": 0,
                "item": {"id": self.item_id, "type": "message", "role": "assistant",
                         "status": "completed", "content": [part]},
            }))
        for index, item in enumerate(body["output"]):
            if item["type"] != "function_call":
                continue
            output_index = index + (1 if self.opened else 0)
            events.append(self._event({
                "type": "response.output_item.added", "output_index": output_index,
                "item": item | {"status": "in_progress", "arguments": ""},
            }))
            events.append(self._event({
                "type": "response.function_call_arguments.delta", "item_id": item["id"],
                "output_index": output_index, "delta": item["arguments"],
            }))
            events.append(self._event({
                "type": "response.function_call_arguments.done", "item_id": item["id"],
                "output_index": output_index, "arguments": item["arguments"],
            }))
            events.append(self._event({"type": "response.output_item.done", "output_index": output_index, "item": item}))
        events.append(self._event({"type": "response.completed", "response": body}))
        return events

    def fail(self, message: str) -> list[tuple[str, dict[str, Any]]]:
        """Tell the client the stream desynchronized, rather than contradicting it."""
        return [
            self._event({"type": "error", "code": "stream_desynchronized", "message": _scrub(message), "param": None}),
            self._event({"type": "response.failed", "response": self._skeleton("failed")}),
        ]


class AnthropicStream:
    """Messages API events with ordered content blocks."""

    def __init__(self, response_id: str, model: str) -> None:
        self.response_id = response_id
        self.model = model
        self.opened = False
        self.index = 0

    def open(self, result_id: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        message = {
            "id": (result_id or self.response_id).replace("resp_", "msg_"), "type": "message",
            "role": "assistant", "model": self.model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        return [("message_start", {"type": "message_start", "message": message})]

    def delta(self, text: str) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        if not self.opened:
            self.opened = True
            events.append(("content_block_start", {
                "type": "content_block_start", "index": self.index,
                "content_block": {"type": "text", "text": ""},
            }))
        events.append(("content_block_delta", {
            "type": "content_block_delta", "index": self.index,
            "delta": {"type": "text_delta", "text": text},
        }))
        return events

    def close(self, body: dict[str, Any], result: GatewayResult) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        if self.opened:
            events.append(("content_block_stop", {"type": "content_block_stop", "index": self.index}))
            self.index += 1
        for block in body["content"]:
            if block["type"] != "tool_use":
                continue
            events.append(("content_block_start", {
                "type": "content_block_start", "index": self.index,
                "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}},
            }))
            events.append(("content_block_delta", {
                "type": "content_block_delta", "index": self.index,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"], separators=(",", ":"))},
            }))
            events.append(("content_block_stop", {"type": "content_block_stop", "index": self.index}))
            self.index += 1
        events.append(("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": body["stop_reason"], "stop_sequence": None},
            "usage": {"input_tokens": result.input_units, "output_tokens": result.output_units},
        }))
        events.append(("message_stop", {"type": "message_stop"}))
        return events

    def fail(self, message: str) -> list[tuple[str, dict[str, Any]]]:
        return [("error", {"type": "error", "error": {"type": "api_error", "message": _scrub(message)}})]


class GeminiStream:
    """streamGenerateContent chunks."""

    def __init__(self, model: str) -> None:
        self.model = model

    def open(self) -> list[tuple[str | None, dict[str, Any]]]:
        return []

    def delta(self, text: str) -> list[tuple[str | None, dict[str, Any]]]:
        return [(None, {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "index": 0}],
                        "modelVersion": self.model})]

    def close(self, body: dict[str, Any], result: GatewayResult) -> list[tuple[str | None, dict[str, Any]]]:
        return [(None, body)]

    def fail(self, message: str) -> list[tuple[str | None, dict[str, Any]]]:
        return [(None, {"error": {"code": 500, "message": _scrub(message), "status": "INTERNAL"}})]


GATEWAY_MODELS = ("fancy-chatgpt", "fancy-gemini", "fancy-claude")


def codex_models() -> dict[str, Any]:
    models = []
    for priority, name in enumerate(GATEWAY_MODELS, start=1):
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
            # Declared from the resolved adapter capability, not from what
            # the protocol could theoretically express.
            "input_modalities": sorted(
                resolve_capability(
                    protocol="openai", site=GatewayService.resolve_site(name), model=name
                ).input_modalities_values
            ),
        })
    return {"models": models}


_HEADER_VALUE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,200}$")


def _validate_header(value: str | None, name: str) -> str | None:
    """Reject header values that could not be a legitimate identifier."""
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if not _HEADER_VALUE.match(candidate):
        raise ValueError(f"invalid {name} header value")
    return candidate


def openai_stream_events(body: dict[str, Any], result: GatewayResult) -> list[tuple[str | None, dict[str, Any]]]:
    """Responses API event stream.

    Order matters to the Codex client, and every event carries a monotonic
    ``sequence_number`` as the Responses streaming spec requires.
    """
    events: list[tuple[str | None, dict[str, Any]]] = []

    def emit(payload: dict[str, Any]) -> None:
        payload["sequence_number"] = len(events)
        events.append((payload["type"], payload))

    emit({"type": "response.created", "response": body | {"status": "in_progress", "output": []}})
    emit({"type": "response.in_progress", "response": body | {"status": "in_progress", "output": []}})
    for output_index, item in enumerate(body["output"]):
        if item["type"] == "message":
            empty_item = item | {"status": "in_progress", "content": []}
            emit({"type": "response.output_item.added", "output_index": output_index, "item": empty_item})
            emit({
                "type": "response.content_part.added",
                "item_id": item["id"],
                "output_index": output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })
            emit({
                "type": "response.output_text.delta",
                "item_id": item["id"],
                "output_index": output_index,
                "content_index": 0,
                "delta": result.text,
            })
            emit({
                "type": "response.output_text.done",
                "item_id": item["id"],
                "output_index": output_index,
                "content_index": 0,
                "text": result.text,
            })
            emit({
                "type": "response.content_part.done",
                "item_id": item["id"],
                "output_index": output_index,
                "content_index": 0,
                "part": item["content"][0],
            })
            emit({"type": "response.output_item.done", "output_index": output_index, "item": item})
        else:
            pending = item | {"status": "in_progress", "arguments": ""}
            emit({"type": "response.output_item.added", "output_index": output_index, "item": pending})
            emit({
                "type": "response.function_call_arguments.delta",
                "item_id": item["id"],
                "output_index": output_index,
                "delta": item["arguments"],
            })
            emit({
                "type": "response.function_call_arguments.done",
                "item_id": item["id"],
                "output_index": output_index,
                "arguments": item["arguments"],
            })
            emit({"type": "response.output_item.done", "output_index": output_index, "item": item})
    emit({"type": "response.completed", "response": body})
    return events


def anthropic_stream_events(body: dict[str, Any], result: GatewayResult) -> list[tuple[str | None, dict[str, Any]]]:
    """Messages API event stream with ordered content blocks."""
    events: list[tuple[str | None, dict[str, Any]]] = [
        ("message_start", {"type": "message_start", "message": body | {"content": [], "stop_reason": None}})
    ]
    for index, block in enumerate(body["content"]):
        if block["type"] == "text":
            start_block: dict[str, Any] = {"type": "text", "text": ""}
        else:
            start_block = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
        events.append(("content_block_start", {"type": "content_block_start", "index": index, "content_block": start_block}))
        if block["type"] == "text":
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"], separators=(",", ":"))}
        events.append(("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
    events.append((
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": body["stop_reason"], "stop_sequence": None},
            "usage": {"input_tokens": result.input_units, "output_tokens": result.output_units},
        },
    ))
    events.append(("message_stop", {"type": "message_stop"}))
    return events


def gemini_stream_events(body: dict[str, Any]) -> list[tuple[str | None, dict[str, Any]]]:
    return [(None, body)]


#: Header names the three supported clients may use to carry an idempotency
#: key. The internal store is keyed by value, never by which header carried it.
IDEMPOTENCY_HEADERS = ("idempotency-key", "x-idempotency-key", "x-request-id", "x-fancy-idempotency-key")


def _protocol_error(protocol: str, status: int, message: str, kind: str) -> dict[str, Any]:
    """Render an error in the envelope the client's own SDK expects."""
    scrubbed = _scrub(message)
    if protocol == "anthropic":
        return {"type": "error", "error": {"type": kind, "message": scrubbed}}
    if protocol == "gemini":
        status_name = {
            400: "INVALID_ARGUMENT",
            401: "UNAUTHENTICATED",
            409: "ABORTED",
            429: "RESOURCE_EXHAUSTED",
            499: "CANCELLED",
            502: "UNAVAILABLE",
            503: "UNAVAILABLE",
        }.get(status, "UNKNOWN")
        return {"error": {"code": status, "message": scrubbed, "status": status_name}}
    return {"error": {"message": scrubbed, "type": kind, "code": None, "param": None}}


def classify_error(exc: Exception, protocol: str) -> tuple[int, dict[str, Any], int | None]:
    """Map a gateway exception to (status, body, retry_after) for a protocol.

    Anthropic uses 529/overloaded_error for backpressure where OpenAI and
    Gemini use 429, so the status itself is protocol dependent.
    """
    if isinstance(exc, GatewayOverloaded):
        if protocol == "anthropic":
            return 529, _protocol_error(protocol, 529, str(exc), "overloaded_error"), exc.retry_after
        kind = "rate_limit_error" if protocol == "openai" else "RESOURCE_EXHAUSTED"
        return 429, _protocol_error(protocol, 429, str(exc), kind), exc.retry_after
    if isinstance(exc, GatewayCancelled):
        # 499 is the Gemini-documented client-cancelled code; the other two
        # clients simply see the request aborted with a matching envelope.
        return 499, _protocol_error(protocol, 499, exc.reason, "request_cancelled"), None
    if isinstance(exc, ToolLoopExhausted):
        return 409, _protocol_error(protocol, 409, str(exc), "invalid_request_error"), None
    if isinstance(exc, BrowserTurnError):
        # The browser failure taxonomy already decided what this honestly is,
        # including whether a retry can help.
        policy = exc.policy
        status = policy.http_status
        kind = {
            401: "authentication_error", 429: "rate_limit_error", 409: "invalid_request_error",
            422: "invalid_request_error", 503: "overloaded_error", 504: "timeout_error",
        }.get(status, "api_error")
        if protocol == "anthropic" and status == 429:
            status, kind = 529, "overloaded_error"
        message = f"{policy.failure.value}: {exc}. {policy.guidance}"
        retry = 30 if policy.failure.value == "rate-limited" else (5 if policy.retryable else None)
        return status, _protocol_error(protocol, status, message, kind), retry
    if isinstance(exc, CrossSessionError):
        return 403, _protocol_error(protocol, 403, str(exc), "permission_error"), None
    if isinstance(exc, AmbiguousCorrelationError):
        return 409, _protocol_error(protocol, 409, str(exc), "invalid_request_error"), None
    if isinstance(exc, IdempotencyConflict):
        return 409, _protocol_error(protocol, 409, str(exc), "invalid_request_error"), None
    if isinstance(exc, UncertainSubmitError):
        return 409, _protocol_error(protocol, 409, str(exc), "invalid_request_error"), None
    if isinstance(exc, UnsupportedModalityError):
        return 400, _protocol_error(protocol, 400, str(exc), "invalid_request_error"), None
    if isinstance(exc, (ValueError, FileNotFoundError, json.JSONDecodeError)):
        return 400, _protocol_error(protocol, 400, str(exc), "invalid_request_error"), None
    return 502, _protocol_error(protocol, 502, str(exc), "api_error"), None


def serve_gateway(
    host: str,
    port: int,
    root: Path | str,
    token: str | None = None,
    *,
    browser_slots: int | None = None,
) -> None:
    """Serve the gateway over ASGI.

    ``browser_slots`` bounds how many automation tabs may be open at once; it is
    the real resource, so it is deliberately separate from HTTP concurrency.
    """
    import uvicorn

    from .gateway_app import create_app

    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        raise ValueError("a gateway token is required when binding outside loopback")
    service = GatewayService(root)
    slots = browser_slots or int(os.getenv("FANCY_GPT_GATEWAY_BROWSER_SLOTS", "4"))
    app = create_app(service, token=token, browser_slots=slots)
    print(f"FancyGPT model gateway listening on http://{host}:{port} ({slots} browser slots)")
    uvicorn.run(app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=30)
