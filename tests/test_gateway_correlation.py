"""Correlating a stateless client's transcript back to its browser chat.

Claude Code and the Gemini CLI resend their whole transcript each turn and
compact it themselves, so a rule that demands an exact prefix fails exactly when
a session gets long.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fancy_gpt.gateway import (
    AmbiguousCorrelationError,
    GatewayService,
    normalize_anthropic,
)
from fancy_gpt.gateway_usage import estimate_tokens
from fancy_gpt.models import AutomatedModelResponse
from fancy_gpt.tunnels.models import TunnelHealth, TunnelHealthState, TunnelSelection

LONG_A = "Please analyse the authentication module and report any race conditions you find in it."
LONG_B = "Summarise the deployment pipeline and identify which stages can run concurrently today."


class Provider:
    name = "fake-web"

    def __init__(self, conversation_id: str = "conversation-1") -> None:
        self.requests: list = []
        self.conversation_id = conversation_id
        self.text = "gateway-ok"

    def start(self) -> None: ...
    def stop(self) -> None: ...

    def execute(self, request):
        self.requests.append(request)
        return AutomatedModelResponse(
            request_id=request.request_id, stage="agent", provider=self.name,
            raw_text=json.dumps({"type": "message", "text": self.text}),
            response_identity="r1", conversation_id=self.conversation_id,
        )


class Manager:
    def __init__(self, provider: Provider) -> None:
        self.model = provider

    def select(self, **_kwargs):
        return TunnelSelection(
            tunnel_id="edge-remote", reason="test", explicit=True,
            health=TunnelHealth(tunnel_id="edge-remote", state=TunnelHealthState.HEALTHY, detail="ok"),
        )

    def provider(self, _selection):
        return self.model


def anthropic(messages: list[dict]):
    return normalize_anthropic({"model": "chatgpt-web", "messages": messages})


# -- the case that used to fragment long sessions ----------------------------


def test_client_side_compaction_still_finds_the_same_browser_chat(tmp_path: Path) -> None:
    """The client drops its own early history; the chat must not be abandoned."""
    provider = Provider()
    service = GatewayService(tmp_path, manager=Manager(provider))

    first = service.execute(anthropic([{"role": "user", "content": LONG_A}]))
    assert first.conversation_id == "conversation-1"

    # The client compacts: the opening turn is replaced by a summary it invented,
    # so the stored transcript is no longer a prefix of what it now sends.
    compacted = service.execute(
        anthropic([
            {"role": "user", "content": "[summary of earlier discussion produced by the client]"},
            {"role": "assistant", "content": "gateway-ok"},
            {"role": "user", "content": "now continue"},
        ])
    )
    assert compacted.session_id == first.session_id
    assert provider.requests[-1].metadata["conversation_id"] == "conversation-1"


def test_a_tool_call_id_anchors_the_correlation(tmp_path: Path) -> None:
    """A tool-call id the gateway issued must be echoed back, so it anchors."""

    class ToolProvider(Provider):
        def execute(self, request):
            self.requests.append(request)
            if "TOOL RESULT" in request.prompt:
                envelope = {"type": "message", "text": "done"}
            else:
                envelope = {"type": "tool_calls", "calls": [{"id": "call_anchor_1", "name": "read_file", "arguments": {}}]}
            return AutomatedModelResponse(
                request_id=request.request_id, stage="agent", provider=self.name,
                raw_text=json.dumps(envelope), response_identity="r1",
                conversation_id=self.conversation_id,
            )

    provider = ToolProvider()
    service = GatewayService(tmp_path, manager=Manager(provider))
    tools = [{"name": "read_file", "input_schema": {"type": "object"}}]

    first = service.execute(
        normalize_anthropic({"model": "chatgpt-web", "tools": tools,
                             "messages": [{"role": "user", "content": LONG_A}]})
    )
    assert first.tool_calls[0].id == "call_anchor_1"

    # The client compacts away its opening message but must still echo the tool
    # result, because the tool call is unresolved.
    follow_up = service.execute(
        normalize_anthropic({
            "model": "chatgpt-web", "tools": tools,
            "messages": [
                {"role": "user", "content": "[client summary replacing earlier turns]"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "call_anchor_1", "name": "read_file", "input": {}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "call_anchor_1", "content": "file body"}]},
            ],
        })
    )
    assert follow_up.session_id == first.session_id
    assert provider.requests[-1].metadata["conversation_id"] == "conversation-1"
    assert service.store.load(follow_up.response_id).previous_response_id == first.response_id


def test_correlation_refuses_to_cross_clients(tmp_path: Path) -> None:
    """Two callers, same words: the transcript must not attach to the other."""
    service = GatewayService(tmp_path, manager=Manager(Provider()))
    service.execute(anthropic([{"role": "user", "content": LONG_A}]), client_key="alice")
    service.execute(anthropic([{"role": "user", "content": LONG_A}]), client_key="bob")

    # Alice continues. Both stored turns share the anchor, but the client key
    # partitions them, so exactly one candidate remains.
    result = service.execute(
        anthropic([
            {"role": "user", "content": LONG_A},
            {"role": "assistant", "content": "gateway-ok"},
            {"role": "user", "content": "and now the follow-up question"},
        ]),
        client_key="alice",
    )
    ledger = service.store.load(result.response_id)
    predecessor = service.store.load(ledger.previous_response_id)
    assert predecessor.client_key == "alice"


def test_a_low_entropy_message_never_anchors(tmp_path: Path) -> None:
    """"ok" is identical across unrelated clients and must not correlate."""
    service = GatewayService(tmp_path, manager=Manager(Provider()))
    service.execute(anthropic([{"role": "user", "content": "ok"}]), client_key="alice")
    service.execute(anthropic([{"role": "user", "content": "ok"}]), client_key="bob")

    from fancy_gpt.gateway import _is_distinctive

    assert _is_distinctive("ok") is False
    assert _is_distinctive("continue") is False
    assert _is_distinctive("y" * 100) is False, "repetition carries little information"
    assert _is_distinctive(LONG_A) is True


def test_ambiguity_across_sessions_is_still_refused(tmp_path: Path) -> None:
    service = GatewayService(tmp_path, manager=Manager(Provider()))
    # Same distinctive opening under two different sessions, no client key.
    for session in ("gw_one", "gw_two"):
        service.execute(anthropic([{"role": "user", "content": LONG_A}]).model_copy(update={"session_id": session}))

    with pytest.raises(AmbiguousCorrelationError):
        service.execute(
            anthropic([
                {"role": "user", "content": LONG_A},
                {"role": "assistant", "content": "gateway-ok"},
                {"role": "user", "content": "which one am I?"},
            ])
        )


def test_an_unrelated_transcript_starts_a_new_chat(tmp_path: Path) -> None:
    service = GatewayService(tmp_path, manager=Manager(Provider()))
    first = service.execute(anthropic([{"role": "user", "content": LONG_A}]))
    other = service.execute(anthropic([{"role": "user", "content": LONG_B}]))
    assert other.session_id != first.session_id


# -- usage estimation --------------------------------------------------------


def test_usage_is_reported_and_over_counts_rather_than_under(tmp_path: Path) -> None:
    """Claude Code compacts on these numbers, so under-counting overruns it."""
    service = GatewayService(tmp_path, manager=Manager(Provider()))
    result = service.execute(anthropic([{"role": "user", "content": LONG_A}]))
    assert result.input_units > 0

    code = '{"key": "value", "fn": "lambda x: x*2"}' * 20
    prose = "The quick brown fox jumps over the lazy dog. " * 20
    japanese = "日本語のテキストです。" * 20
    for text in (code, japanese):
        assert estimate_tokens(text) > len(text) // 4, "dense text must not be under-counted"
    assert estimate_tokens(prose) >= len(prose) // 4
    assert estimate_tokens("") == 0
    assert estimate_tokens("hi") >= 1


def test_capability_declares_usage_as_estimated() -> None:
    from fancy_gpt.gateway_capabilities import resolve_capability

    capability = resolve_capability(protocol="anthropic", site="chatgpt", model="claude-web")
    assert capability.as_dict()["usage_is_estimated"] is True
