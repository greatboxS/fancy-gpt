# Roadmap

This roadmap is ordered by dependency. A later phase may be prototyped early, but
it is not considered supported until the preceding exit gate remains green.

## Phase 1: MCP and web-tool stability

Status: **in progress**

Deliverables:

- Preserve the separation of `site` from browser `tunnel` across CLI, MCP, bridge,
  extension, sessions, and persisted request state.
- Complete ChatGPT and Gemini adapter parity for submit, progress, final extraction,
  conversation continuation, temporary/new chat, timeout, cancellation, and errors.
- Complete Grok and Microsoft Copilot as vertical site slices: measured capture,
  DOM adapter, runtime decoder, core registration, gateway alias, and live evidence.
- Harden DOM interaction around Playwright-style role/label semantics, explicit
  adapter capabilities, bounded waits, and recorded DOM fixtures.
- Validate every MCP tool schema and return shape through an in-process MCP client.
- Cover session/chat affinity, concurrent requests, bridge reconnect, route loss,
  parser failures, malformed model output, and restart recovery.
- Keep install, doctor, extension build/export, and remote Edge/Chrome/Firefox bridge
  smoke tests reproducible.

Exit gate:

```bash
python scripts/build_extension.py --check
python -m compileall -q src tests scripts
PYTHONPATH=src pytest -ra
PYTHONPATH=src python scripts/functional_review.py
PYTHONPATH=src python scripts/tunnel_review.py
PYTHONPATH=src python scripts/release_gate.py
```

Additionally, live authenticated smoke tests must pass for ChatGPT and Gemini on at
least one Chromium tunnel. Browser-specific release claims require the same smoke
test on that browser; offline fixtures alone do not establish live UI compatibility.
Grok and Copilot become release-supported only when they satisfy the per-site
definition of done in `SITE_INTEGRATION_HANDOFF.md`; until then discovery must
report their actual layer readiness rather than a blanket supported flag.

## Phase 2: Gateway core and context ledger

Status: **implemented (hardening continues)**

Deliverables:

- Introduce protocol-neutral normalized messages, content parts, tools, events,
  errors, finish reasons, and usage types.
- Implement durable gateway sessions, turns, provider bindings, idempotency, and
  the context ledger specified in [MODEL_GATEWAY.md](MODEL_GATEWAY.md).
- Implement continuation, reconstruction, compaction, budget reservation, and
  conversation serialization before exposing public compatibility endpoints.
- Reuse existing provider/site/tunnel services without importing MCP concerns.
- Add deterministic fake-provider and replay fixtures for all context decisions.

Exit gate:

- Unit tests cover context watermark mismatch, instruction changes, tool-call pairs,
  oversized input, compaction provenance, retries, cancellation, and concurrent turns.
- A restart can resume a gateway session without relying on a browser tab ID.
- No gateway persistence contains browser credentials or raw authentication state.

## Phase 3: Codex support

Status: **protocol and tool-loop conformance implemented**

Deliverables:

- Implement OpenAI Responses-compatible create and streaming endpoints.
- Map instructions, input items, function tools, tool outputs, status/error events,
  usage, cancellation, and idempotency into the normalized core.
- Publish a tested Codex `model_providers` configuration and health check.

Exit gate:

- Codex completes a multi-turn repository task through `fancy-gemini` and
  `fancy-chatgpt`, including at least two tool calls and one failed tool result.
- Streaming, cancellation, malformed tool arguments, context reconstruction, and
  process restart pass recorded conformance tests.

## Phase 4: Claude Code support

Status: **protocol and tool-loop conformance implemented**

Deliverables:

- Implement Anthropic Messages-compatible create and streaming endpoints.
- Preserve ordered content blocks, `tool_use`/`tool_result` IDs, stop reasons,
  system instructions, errors, and usage semantics.
- Publish tested `ANTHROPIC_BASE_URL` and model-alias configuration.

Exit gate:

- Claude Code passes the same agent-loop and context-recovery scenarios as Codex.
- Protocol-specific fixtures verify SSE event ordering and content-block indexes.

## Phase 5: Gemini client support

Status: **protocol and tool-loop conformance implemented**

Deliverables:

- Implement Gemini `generateContent` and streaming compatibility over the same core.
- Map contents/parts, system instructions, function declarations/calls/responses,
  safety/finish metadata, and errors without changing site or tunnel ownership.
- Publish tested client configuration and examples.

Exit gate:

- A Gemini-compatible SDK/client completes the shared conformance scenarios.
- Cross-protocol tests prove one normalized conversation can be reconstructed without
  silently changing instruction priority or breaking tool-call pairing.

## Phase 6: Hardening and release

Status: **largely delivered**

Delivered:

- `max_tool_loop_iterations` enforced per session: a client answering tool calls
  with tool calls is bounded, a final message resets the budget, and the refusal
  happens before anything reaches the browser.
- Legal turn-state transitions enforced as a compare-and-set against the record
  on disk, so a stale copy cannot resurrect a finished turn. `uncertain-submit`
  is deliberately not terminal - it can still be resolved once we learn what the
  browser did.
- Real retry lineage: a retry is its own record linked to the one it supersedes,
  and `requests trace` shows the attempt chain and every state transition.

- Browser failure taxonomy: 17 distinct causes, each carrying whether a retry
  helps, whether a human must act, and its honest protocol status.
- Per-turn trace (`requests trace`, MCP `get_request_trace`) recording every
  stage with timings, and holding no prompt, reply or page URL by construction.
- Usage estimation that over-counts rather than under-counts, shared with
  compaction so the two cannot disagree.
- Transcript correlation anchored on content that survives the client's own
  compaction, partitioned by client so a low-entropy message cannot attach one
  caller's transcript to another's browser conversation.

- Incremental text streaming to all three protocols, converting cumulative
  page snapshots into append-only deltas under a monotonic prefix lock, with
  whitespace re-render realignment and path-resolved (not substring-matched)
  extraction of the streamable value.

- Turn lifecycle state machine with `uncertain-submit`, so a browser turn that may
  already have landed is never blind re-submitted, across process restart.
- Idempotency store with atomic claims, replay on same key plus payload, rejection
  on payload mismatch.
- Per-provider-conversation locking replacing the global gateway lock.
- Ownership-scoped correlation: `previous_response_id` cross-session use is refused
  and an ambiguous transcript is rejected rather than guessed.
- Real compaction over protocol-semantic units with atomic tool pairs, reserved
  budget, persisted provenance, and refusal when unsafe.
- Capability negotiation as the protocol/model/site/adapter intersection, with
  attachments rejected before submit instead of silently dropped.
- Bounded backpressure with protocol-native 429/529/RESOURCE_EXHAUSTED errors,
  credential scrubbing, constant-time token comparison, and `/health` + `/metrics`.
- Compatibility matrix published in `docs/MODEL_GATEWAY.md`.
- ASGI transport on Starlette/uvicorn with an `anyio.CapacityLimiter` bounding
  browser tabs, client-disconnect cancellation while a turn is in flight, and
  incremental SSE that stops when the caller goes away.
- Real end-to-end cancellation, verified live: a long turn that runs 145.5s to
  completion ends in 8.1s when cancelled, in the same second the cancel is
  issued. The site's own stop control is clicked and the turn ends as
  `job_cancelled`, fenced by `(job_id, generation_epoch)`.
- One bridge worker now serves concurrent jobs; its lock no longer spans the
  wait, which also fixes replies being dropped for an unregistered caller.
- `TurnExecution`/`TabLease`: the browser-tab permit is owned by the turn, not
  by the HTTP request, with single-terminal semantics and a reclaim deadline so
  a dead browser cannot starve capacity.

Remaining:

- End-to-end conformance runs driven by the real Codex, Claude Code and Gemini
  CLI binaries rather than protocol-level tests.
- Live closeout for the parallel browser surfaces specified in
  `BROWSER_PARALLELISM.md`: randomized Edge/Chrome soak, render/visibility
  evidence, leaked-surface accounting, and composer-edit detection.
- Join Grok and Copilot browser/decoder slices to the site registry, gateway
  model catalog, capabilities, CLI/MCP discovery, and cross-protocol tests.
- Keep the per-layer readiness matrix and live compatibility evidence current.

Deliverables:

- Load limits, backpressure, rate limiting, observability, scrubbed replay traces,
  upgrade/migration tests, and operator diagnostics.
- Capability discovery that reports supported protocol features and conservative
  per-site context budgets.
- Compatibility matrix by client version, protocol feature, site, and browser.

Release gate:

- No known context corruption, duplicate submission, cross-session leakage, or
  unbounded tool loop.
- Sustained end-to-end runs meet documented success/latency targets.
- Experimental browser-web limitations are stated separately from protocol support.
- Parallel turns complete with randomized ordering and zero cross-turn response,
  progress, cancellation, capture, or cleanup leakage.

### Parallel-runtime gate status

Implemented and covered by executable tests:

- exact per-turn surface leases and fenced progress/cancel/terminal handling;
- worker capacity negotiation, bounded queuing, and least-loaded routing that
  includes queued work and filters advertised site capabilities;
- FIFO serialization for an exact `(tunnel, site, conversation_id)` while
  unrelated conversations continue concurrently;
- cancellable capacity and conversation waiters: cancellation before browser
  submission is terminal locally and the prompt is never sent;
- focus held only through verified prompt acceptance;
- immediate failure of pending jobs on worker-generation loss;
- conservative MV3 restart quarantine with no blind prompt replay;
- self-reload accepted only while idle and verified by a new worker id plus the
  expected extension build id.

Still requiring live evidence before the parallel-runtime gate is closed:

- randomized Edge and Chrome soak after the current build is deployed;
- browser-visible throughput, latency, visibility, and leaked-surface evidence;
- measured Grok and Copilot adapters/decoders and their provider-specific live
  correlation fixtures.
