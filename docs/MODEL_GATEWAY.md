# Model Gateway architecture

## Goal

The Model Gateway makes a FancyGPT-backed online model usable as the direct model
provider for Codex, Claude Code, and Gemini-compatible clients. It is an additional
application adapter; the existing MCP server remains supported and independent.

## Protocol surface

| Client family | Gateway contract | Initial endpoint |
|---|---|---|
| Codex | OpenAI Responses API | `POST /v1/responses` |
| Claude Code | Anthropic Messages API | `POST /v1/messages` |
| Gemini clients | Gemini `generateContent` API | `POST /v1beta/models/{model}:generateContent` |

Streaming is part of the contract, not an optional presentation feature. Each
adapter maps its wire events into one internal turn/event model and maps internal
events back without leaking site-specific DOM details.

```text
HTTP protocol adapter
        |
NormalizedTurn
  - instructions
  - ordered messages/content parts
  - tools and tool choice
  - output constraints
  - context/session hints
        |
GatewayTurnService
  - idempotency and cancellation
  - context ledger
  - tool-loop state
  - usage accounting
        |
ModelProvider(site) --> SiteAdapter --> Tunnel(browser route)
```

## Model and route selection

A public model name resolves to an explicit site and site model profile. Browser
routing is resolved separately.

```text
model=gemini-web-pro  -> site=gemini, profile=pro
tunnel=edge-remote    -> browser route only
```

Requests may apply an authorized tunnel hint, but a model alias never embeds a
tunnel. Health-based tunnel failover must preserve the provider conversation
binding or start a deliberate reconstructed turn.

## Context model

### Authoritative input

The client request is authoritative for the current turn. A web conversation is
never assumed to contain messages merely because they were previously submitted.
The gateway records which normalized messages are represented in each provider
conversation and verifies the binding before using continuation mode.

### Context ledger

Each logical gateway session stores:

- stable gateway session and turn IDs;
- client protocol and client-provided conversation metadata;
- normalized message IDs and content digests;
- system/developer instruction digest and version;
- model alias, resolved site/model profile, and tunnel used;
- provider conversation ID/URL and represented-message watermark;
- tool definitions digest, pending tool calls, and received tool results;
- estimated tokens/characters before dispatch and observed output size;
- compaction generation, summary provenance, and dropped-content manifest;
- request idempotency key, retry lineage, status, and cancellation state.

The ledger contains no browser authentication material. Site cookies and tokens
remain inside the browser boundary.

### Continuation strategies

The gateway chooses one strategy for every turn:

| Strategy | Use when | Behavior |
|---|---|---|
| Continue | Binding and message watermark match | Send only the new delta |
| Reconstruct | Binding is absent or cannot be trusted | Start a new web thread with bounded normalized context |
| Compact | Input approaches the selected site's budget | Replace older context with a provenance-linked summary |
| Reject | Required instructions or tool state cannot fit safely | Return a protocol-native context error |

Compaction must preserve, in priority order: system/developer instructions,
unresolved tool calls and their results, current user intent, active constraints,
accepted decisions, cited evidence, and the recent interaction tail. Tool calls and
tool results are atomic pairs and cannot be split or summarized independently.

Summaries are data, never higher-priority instructions. Each summary records its
source message range and digest so a later reconstruction can be audited.

### Budgeting

Site adapters expose conservative context capabilities rather than claiming the
nominal context window of an underlying model. Budget calculation reserves output
space and tool-loop space before dispatch. Character-based estimates are permitted
for an initial web adapter, but the safety margin must be configurable and tested
against oversized prompts, Unicode, attachments, and large tool schemas.

### Concurrent turns and retries

Turns sharing one provider conversation are serialized. Different conversations
may run concurrently. An idempotent retry can reuse a completed result; an uncertain
browser submission cannot be blindly repeated. The adapter first determines whether
the prior turn reached the provider and either resumes observation or reconstructs
on a new conversation.

## Tool calling

The internal event model represents tool calls structurally:

```text
ToolCall(id, name, JSON arguments)
ToolResult(call_id, content, is_error)
```

Protocol adapters preserve call IDs and ordering. For sites without a public tool
calling channel, the site adapter uses a versioned structured-output envelope and
strict validation. Invalid or ambiguous envelopes are never executed. The gateway
may request one repair turn, then returns a protocol-native model error.

Tool execution remains the client's responsibility: Codex or Claude Code receives
the tool call, executes it under its own permission model, and sends the result in
the next request.

## Reliability and security gates

- Bound request size, generated output, tool count, tool argument size, and loop count.
- Stream heartbeats without fabricating model output.
- Propagate client disconnect and cancellation to browser observation where possible.
- Treat page content and model output as untrusted data.
- Never expose cookies, pair tokens, access tokens, or private browser endpoints.
- Emit protocol conformance, context-decision, and provider-binding diagnostics.
- Keep recorded fixtures scrubbed and deterministic.

## Non-goals

- MCP sampling is not used to replace the MCP client's model.
- Browser tunnel selection is not encoded into model names.
- A browser tab ID is not a conversation identity.
- Nominal API compatibility is not claimed until streaming, errors, tool loops,
  cancellation, and context reconstruction pass their conformance suites.
