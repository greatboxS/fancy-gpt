from fancy_gpt.gateway import GatewayContent, GatewayTool, NormalizedTurn, _gateway_prompt


def _review_turn() -> NormalizedTurn:
    return NormalizedTurn(
        protocol="openai",
        model="fancy-chatgpt",
        instructions="Review the current repository.",
        messages=[GatewayContent(role="user", text="Inspect the current source and review it using tools.")],
        tools=[
            GatewayTool(name="list_mcp_resources", description="List MCP resources."),
            GatewayTool(
                name="exec_command",
                description="Run a local shell command and return its output.",
                parameters={
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                },
            ),
        ],
    )


def test_web_prompt_embeds_versioned_tool_evidence_protocol() -> None:
    prompt = _gateway_prompt(_review_turn(), include_history=True)

    assert "Tool protocol: fancy-tool-v1." in prompt
    assert "EVIDENCE RULE:" in prompt
    assert "Continue calling tools for as many turns as needed" in prompt
    assert "Prefer the tool that directly retrieves the needed evidence" in prompt
    assert "NEED_EVIDENCE -> return `tool_calls`." in prompt
    assert "TOOL_RESULT_RECEIVED_BUT_INSUFFICIENT -> return more `tool_calls`." in prompt
    assert "SUFFICIENT_EVIDENCE -> return `message`." in prompt


def test_tool_protocol_is_appended_after_conversation_body() -> None:
    prompt = _gateway_prompt(_review_turn(), include_history=True)

    user_pos = prompt.index("USER: Inspect the current source")
    protocol_pos = prompt.index("<<<FANCY_GPT_TOOL_PROTOCOL:fancy-tool-v1>>>")
    assert protocol_pos > user_pos
    assert prompt.rstrip().endswith("<<<END_FANCY_GPT_TOOL_PROTOCOL>>>")


def test_tool_protocol_append_is_idempotent() -> None:
    from fancy_gpt.gateway import _append_tool_protocol

    turn = _review_turn()
    once = _append_tool_protocol("BASE PROMPT", turn)
    twice = _append_tool_protocol(once, turn)

    assert twice == once
    assert twice.count("<<<FANCY_GPT_TOOL_PROTOCOL:fancy-tool-v1>>>") == 1
    assert twice.count("<<<END_FANCY_GPT_TOOL_PROTOCOL>>>") == 1


def test_long_prompt_reserves_tail_for_complete_tool_protocol() -> None:
    from fancy_gpt.gateway import (
        TOOL_PROTOCOL_BEGIN,
        TOOL_PROTOCOL_END,
        _append_tool_protocol,
        _tool_protocol_block,
    )

    turn = _review_turn()
    block = _tool_protocol_block(turn)
    max_chars = len(block) + 700
    compiled = _append_tool_protocol("X" * 50_000, turn, max_chars=max_chars)

    assert len(compiled) <= max_chars
    assert compiled.count(TOOL_PROTOCOL_BEGIN) == 1
    assert compiled.count(TOOL_PROTOCOL_END) == 1
    assert compiled.rstrip().endswith(TOOL_PROTOCOL_END)
    assert "conversation body:" in compiled
    assert "EVIDENCE RULE:" in compiled


def test_incomplete_protocol_suffix_is_rebuilt_not_duplicated() -> None:
    from fancy_gpt.gateway import (
        TOOL_PROTOCOL_BEGIN,
        TOOL_PROTOCOL_END,
        _append_tool_protocol,
    )

    turn = _review_turn()
    broken = f"BASE BODY\n\n{TOOL_PROTOCOL_BEGIN}\nBROKEN"
    compiled = _append_tool_protocol(broken, turn)

    assert compiled.startswith("BASE BODY")
    assert compiled.count(TOOL_PROTOCOL_BEGIN) == 1
    assert compiled.count(TOOL_PROTOCOL_END) == 1
    assert compiled.rstrip().endswith(TOOL_PROTOCOL_END)


def test_content_added_after_old_protocol_is_preserved_before_reappend() -> None:
    from fancy_gpt.gateway import (
        TOOL_PROTOCOL_BEGIN,
        TOOL_PROTOCOL_END,
        _append_tool_protocol,
    )

    turn = _review_turn()
    first = _append_tool_protocol("BASE BODY", turn)
    extended = first + "\nNEW CONTINUATION CONTENT"
    compiled = _append_tool_protocol(extended, turn)

    assert "BASE BODY" in compiled
    assert "NEW CONTINUATION CONTENT" in compiled
    assert compiled.index("NEW CONTINUATION CONTENT") < compiled.index(TOOL_PROTOCOL_BEGIN)
    assert compiled.count(TOOL_PROTOCOL_BEGIN) == 1
    assert compiled.count(TOOL_PROTOCOL_END) == 1
    assert compiled.rstrip().endswith(TOOL_PROTOCOL_END)


def test_large_message_that_fits_browser_budget_is_not_truncated() -> None:
    from fancy_gpt.gateway import TOOL_PROTOCOL_END

    head = "HEAD_MARKER_4ac1"
    middle = "MIDDLE_MARKER_7f31"
    tail = "TAIL_MARKER_2b88"
    text = head + ("A" * 90_000) + middle + ("B" * 90_000) + tail
    turn = _review_turn().model_copy(update={
        "messages": [GatewayContent(role="tool", text=text, tool_result_ids=["call_long_evidence"])],
    })

    prompt = _gateway_prompt(turn, include_history=True)

    assert head in prompt
    assert middle in prompt
    assert tail in prompt
    assert "message:" not in prompt
    assert prompt.rstrip().endswith(TOOL_PROTOCOL_END)


def test_large_system_instructions_are_not_capped_at_two_thousand_chars() -> None:
    marker = "SYSTEM_MIDDLE_MARKER_aa91"
    instructions = ("I" * 25_000) + marker + ("J" * 25_000)
    turn = _review_turn().model_copy(update={"instructions": instructions})

    prompt = _gateway_prompt(turn, include_history=True)

    assert marker in prompt
    assert instructions in prompt


def test_six_hundred_k_message_keeps_middle_evidence() -> None:
    from fancy_gpt.gateway import BROWSER_PROMPT_MAX_CHARS

    marker = "MIDDLE_600K_MARKER_31fa"
    text = ("A" * 300_000) + marker + ("B" * 300_000)
    turn = _review_turn().model_copy(update={
        "messages": [GatewayContent(role="tool", text=text, tool_result_ids=["call_600k"])],
    })
    prompt = _gateway_prompt(turn, include_history=True)

    assert BROWSER_PROMPT_MAX_CHARS >= 700_000
    assert marker in prompt
    assert text in prompt
