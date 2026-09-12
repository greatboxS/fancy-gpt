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

Start the local gateway after the browser bridge worker is connected:

```bash
fancy-gpt gateway serve --host 127.0.0.1 --port 8787
```

Loopback access needs no token. Binding to another interface requires `--token`.
The supported aliases are `chatgpt-web`, `gemini-web`, and `claude-web`; aliases
select the site, while `x-fancy-tunnel-id` is an optional, independent browser
route hint.

Codex configuration:

```toml
model = "gemini-web"
model_provider = "fancy-local"

[model_providers.fancy-local]
name = "FancyGPT Local"
base_url = "http://127.0.0.1:8787/v1"
env_key = "FANCY_GPT_LOCAL_KEY"
wire_api = "responses"
```

Claude Code and Gemini CLI can be launched directly against the same process:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ANTHROPIC_API_KEY=local \
  claude --bare --model chatgpt-web

GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8787 GEMINI_API_KEY=local \
  gemini --model gemini-web
```

`FANCY_GPT_GATEWAY_MAX_INPUT_UNITS` sets the conservative character-based input
budget (default `200000`). Oversized inputs are rejected before browser dispatch.
Each turn is visible through `fancy-gpt requests list/inspect/raw` and persists a
context ledger without browser authentication state.

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
GatewayService
  - request lifecycle and inspection
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

The current context ledger stores:

- stable gateway session and turn IDs;
- client protocol and request/instruction digests;
- system/developer instruction digest and version;
- model alias, resolved site/model profile, and tunnel used;
- provider conversation ID and predecessor response ID;
- emitted tool call IDs;
- estimated tokens/characters before dispatch and observed output size;
- compaction generation;
- request lifecycle, route, provider, response artifact, and failure state.

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

Provider turns are serialized so two client turns cannot concurrently mutate one
browser conversation. Codex continuation uses `previous_response_id`; clients that
send a complete transcript are reconstructed safely, while callers may provide
`x-fancy-session-id` to reuse the latest recorded provider binding.

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


## Production hardening

### Turn lifecycle

Every turn is a durable `TurnRecord` under `gateway/state-turns/` moving through
an explicit state machine:

```
queued -> submitting -> submitted -> observing -> completed
                    \-> uncertain-submit        \-> failed / cancelled
```

`uncertain-submit` is the load-bearing state. If the provider call raises after
the prompt was handed to the browser, the turn may or may not have landed in the
chat, so it is parked rather than retried. A later turn on that session is
refused with `UncertainSubmitError` instead of blind-submitting a duplicate, and
that refusal survives a process restart. Failure handling never downgrades an
uncertain turn to `failed`.

### Session binding

A logical gateway session binds to exactly one provider conversation, recorded as
a `SessionBinding` under `gateway/bindings/`. The binding is attached *inside*
the conversation lock, immediately after the provider answers, so a turn waiting
on the same session cannot observe an unbound session and open a second chat.
Rebinding happens only for a recorded reason: no binding, binding untrusted,
site changed, or context cannot continue safely.

If the provider answers on a conversation the session is not bound to, that is
treated as a correlation failure: the session is rebound as untrusted rather than
silently adopting the new chat.

### Correlation is ownership-scoped

A response id is a bearer reference, so continuation proves ownership:

| Input | Rule |
|---|---|
| `previous_response_id` | Must belong to the declaring session, else `403 permission_error` |
| `x-fancy-session-id` | Authoritative when supplied |
| Full transcript (Claude/Gemini) | Matched only within the declared session |
| Ambiguous transcript | `409` rather than guessing which session to continue |

### Idempotency

Keys are accepted from `Idempotency-Key`, `X-Idempotency-Key`, `X-Request-Id`, or
`X-Fancy-Idempotency-Key`; the store is keyed by value, never by which header
carried it. Same key plus same payload digest replays the stored result without
re-submitting. Same key plus a different payload is rejected. A claim is
published by hard-linking a fully written temporary file, so exactly one
concurrent caller wins and a loser never reads a half-written claim.

### Concurrency

The global gateway lock is gone. `ConversationLocks` holds a reference-counted
mutex per `site:conversation_id` (falling back to `session:<id>` while unbound).
Same conversation serializes absolutely; different conversations run in parallel.

### Compaction

Compaction operates on protocol-semantic units, not characters. The budget
reserves output space, tool-loop space, instructions and tool schemas. Pinned
content is system/developer instructions, the current user intent, and any
unresolved tool call. A tool call and its result share a group id and are kept or
dropped together, never split. The replacement summary is injected with a
`context` role and is never promoted into the system instruction position.
Provenance (generation, source range, source digest, kept indexes, dropped
manifest, summary provenance) is written to `gateway-compaction.json`. When the
pinned content alone does not fit, the turn is rejected with
`CompactionImpossible` rather than silently losing the caller's intent.

### Capability negotiation

Advertised capability is the intersection of protocol, model alias, site and
browser adapter. Both supported sites automate a text composer and have no
attachment-upload path, so image, audio, file and video content is **rejected
before submit**, never dropped and never turned into a text placeholder reported
as success. `/v1/models`, `/health` and `/v1/capabilities` report the resolved
intersection, so the Gemini protocol's video support is not advertised.

### Transport and resource management

The gateway runs as an ASGI app on **Starlette + uvicorn**, with SSE through
**sse-starlette**. These arrive with the `mcp` dependency already, so they are
declared directly rather than relied on transitively.

The scarce resource is **browser tabs**, not CPU or threads: one turn occupies
one automation tab for as long as the model takes to answer. So an
`anyio.CapacityLimiter` bounds how many turns may hold a tab at once, and every
turn runs on the worker thread pool through that limiter:

```
FANCY_GPT_GATEWAY_BROWSER_SLOTS   # default 4
```

`GatewayService` stays synchronous. The browser driver blocks in a socket read,
so running the turn in a worker thread is what it actually is, and keeping the
core sync leaves the state, compaction and capability layers untouched.

A caller that disconnects cancels its turn *while the browser call is still in
flight*, via a task that polls `request.is_disconnected()` and trips the
`CancelToken`. The turn releases its conversation lock at its next checkpoint.

Cancellation reaches the browser. Because the provider call blocks, a watcher
carries the cancellation out of band: it names the in-flight bridge turn on its
own short-lived connection, the extension routes it to the tab serving that job,
and the site adapter clicks the site's **own stop control**. Generation ends, the
content script finishes, and the blocked call unwinds normally. The controller
then sees `job_cancelled` with whatever partial text existed - distinct from a
turn that actually answered.

A cancel carries `(job_id, generation_epoch)`, so a cancel aimed at a previous
occupant of a recycled tab is discarded rather than stopping the turn currently
using it. If the driver has no cancel path at all, `provider.cancel()` reports
`False` rather than pretending it worked.

SSE events are emitted one at a time with a disconnect checkpoint between them.
Note what this does and does not mean: the browser backend returns a whole
answer, so these events are protocol *framing*, not token-by-token generation.
What is genuinely incremental is the transport.

### Concurrency through the bridge

One browser worker serves many turns at once. The worker's lock is held only to
register a job's reply queue and touch counters, never across the wait. Holding
it for the whole job made a single worker strictly one-job-at-a-time, and worse:
a second caller could not even register its queue, so its reply arrived, found no
destination, and was dropped - after which that caller waited out its full
timeout for a response that had already come and gone.

Measured on four jobs of 0.3s each: ~1.2s serialized before, ~0.3s after.

### Backpressure and limits

| Limit | Default | Env override |
|---|---|---|
| Global active turns | 8 | `FANCY_GPT_GATEWAY_MAX_ACTIVE` |
| Active turns per client | 4 | `FANCY_GPT_GATEWAY_MAX_ACTIVE_PER_CLIENT` |
| Queue depth | 32 | `FANCY_GPT_GATEWAY_MAX_QUEUE` |
| Request body bytes | 10 MiB | `FANCY_GPT_GATEWAY_MAX_BODY_BYTES` |
| Tool count | 128 | `FANCY_GPT_GATEWAY_MAX_TOOLS` |
| Tool schema bytes | 64 KiB | `FANCY_GPT_GATEWAY_MAX_TOOL_SCHEMA_BYTES` |
| Tool argument bytes | 256 KiB | `FANCY_GPT_GATEWAY_MAX_TOOL_ARG_BYTES` |
| Output bytes | 4 MiB | `FANCY_GPT_GATEWAY_MAX_OUTPUT_BYTES` |

Nothing queues without a bound. Request size is checked from `Content-Length`
*before* the body is read or parsed.

### Error mapping

| Condition | OpenAI | Anthropic | Gemini |
|---|---|---|---|
| Overloaded | `429 rate_limit_error` | `529 overloaded_error` | `429 RESOURCE_EXHAUSTED` |
| Cancelled | `499 request_cancelled` | `499 request_cancelled` | `499 CANCELLED` |
| Cross-session | `403 permission_error` | `403` | `403` |
| Ambiguous / idempotency conflict | `409` | `409` | `409 ABORTED` |
| Unsupported modality | `400 invalid_request_error` | `400` | `400 INVALID_ARGUMENT` |

All error bodies pass through credential scrubbing, so a bearer token or cookie
quoted in an exception never reaches the client, the ledger, or the logs.

### Observability

`/health` reports metrics, active conversation count, resolved capabilities and
effective limits. `/metrics` reports queued, running, completed, rejected,
cancelled and failed counters plus the active conversation keys.

## Compatibility matrix

| Client | Protocol | Endpoint | Sites | Text | Tools | Streaming | Attachments |
|---|---|---|---|---|---|---|---|
| Codex | OpenAI Responses | `POST /v1/responses` | chatgpt, gemini | yes | yes | SSE, sequenced | rejected |
| Claude Code | Anthropic Messages | `POST /v1/messages` | chatgpt, gemini | yes | yes | SSE, ordered blocks | rejected |
| Gemini CLI | Gemini generateContent | `POST /v1beta/models/{model}:generateContent` | chatgpt, gemini | yes | yes | `:streamGenerateContent` | rejected |

Model alias to site (the alias selects the **website**, never the browser route):

| Alias | Site | Notes |
|---|---|---|
| `chatgpt-web` | chatgpt | |
| `claude-web` | chatgpt | Served by the ChatGPT site adapter |
| `gemini-web` | gemini | |

The browser route is chosen independently by `x-fancy-tunnel-id` (for example
`edge-remote`). Site and tunnel are never coupled: a single `edge-remote` route
serves both sites.

## Non-goals


- MCP sampling is not used to replace the MCP client's model.
- Browser tunnel selection is not encoded into model names.
- A browser tab ID is not a conversation identity.
- Nominal API compatibility is not claimed until streaming, errors, tool loops,
  cancellation, and context reconstruction pass their conformance suites.
