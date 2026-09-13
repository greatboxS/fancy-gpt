# Architecture — v0.8.0

FancyGPT is a persistent engineering-team runtime. Independent technical review remains a first-class capability, but it is no longer the product boundary.

## Product layers

```text
Project / Target / Acceptance Criteria
              ↓
        Team Orchestrator
              ↓
   Work Items / Agent Roles
              ↓
      Persistent Sessions
              ↓
    Reduced Project Context
              ↓
  Reasoning / Focused / Team Engines
              ↓
       ExecutionCoordinator
              ↓
       Model / Tool Provider
              ↓
          Tunnel Manager
              ↓
 Site Adapter → Browser Runtime → Transport
```

## Integration surfaces

MCP and the planned Model Gateway are sibling adapters over the same application
services. A gateway request never travels through MCP, and MCP does not select or
replace the model used by its client.

```text
Codex CLI -------- OpenAI Responses API ---\
Claude Code ------ Anthropic Messages API --+--> Model Gateway
Gemini clients --- Gemini API compatibility /
                                                   |
MCP clients ------ MCP tools ----------------------+--> Application services
                                                   |
                                             Model provider
                                                   |
request.site --------------------------------> Site adapter
request.tunnel ------------------------------> Browser route
```

The browser tunnel remains a transport concern. `site=gemini`, `site=grok`, or
`site=copilot` may use any healthy Edge, Chrome, or Firefox tunnel whose worker
reports that site as ready, and selecting a tunnel never implies a site. The
target site set is ChatGPT, Gemini, Grok, and Microsoft Copilot; readiness is
tracked per layer in [SITE_INTEGRATION_HANDOFF.md](SITE_INTEGRATION_HANDOFF.md),
so an observed domain is not mistaken for an end-to-end supported route.

## Separation rules

- **Project state** owns durable target, work graph, sessions, decisions, evidence, findings, artifacts, and completion criteria.
- **Team orchestration** decides which bounded work item is ready and which role should act next.
- **Reasoning engines** solve one bounded task; they do not own browser lifecycle or project persistence.
- **ExecutionCoordinator** creates diagnostic state before tunnel/provider work can fail.
- **Tunnel** selects how a model interaction executes; it is not an agent role and not project state.
- **Site/Runtime/Transport** stay independent and contract-tested.
- **Protocol adapters** own wire compatibility only; they do not own browser state,
  project state, or context reduction policy.
- **Context management** separates client transcript, provider conversation state,
  and durable reduced project memory. Raw browser history is never project truth.

## Context boundaries

FancyGPT has three related but distinct context layers:

1. **Client context** is the ordered request supplied by Codex, Claude Code, or a
   Gemini client, including system instructions, tool definitions, tool results,
   and model-visible history.
2. **Provider context** is the conversation bound to a ChatGPT or Gemini web thread.
   It is an optimization and continuity mechanism, not the authoritative transcript.
3. **Durable context** is reduced FancyGPT project/session state: decisions,
   evidence, findings, artifacts, and next actions.

Every gateway turn will persist a context ledger containing a stable session key,
client message identity, provider conversation binding, request digest, model/site,
estimated input/output usage, compaction generation, and tool-loop state. Retry and
resume decisions use this ledger rather than guessing from the active browser tab.

Detailed invariants and protocol mappings are in [MODEL_GATEWAY.md](MODEL_GATEWAY.md).

## Project state model

Project history is append-only. FancyGPT materializes current state from durable events instead of replaying raw conversations into every prompt.

```text
Project
├── Target
├── Acceptance Criteria
├── Work Items
├── Sessions
├── Decisions
├── Evidence
├── Findings
├── Artifacts
└── Next Actions
```

A project is complete only when required work is complete, acceptance criteria are evidence-backed, and no material unresolved finding blocks completion.

## Relevance and sufficiency

Every session carries a semantic policy:

> Expand only when expansion changes correctness, decision quality, material risk, confidence, a blocking unknown, or the next required action. Otherwise preserve the current scope.

This is intentionally not implemented as a word-count rule. `ScopeContract` and `RelevanceSufficiencyPolicy` are compiled into planner/final/team/focused prompts, and model output can record justified scope expansions.

## ChatGPT Web state

ChatGPT Web is treated as a stateful reasoning workspace where useful:

- `fresh` — independent work; temporary ChatGPT thread by default.
- `resume` — continue a persisted ChatGPT conversation binding.
- `fork` — start a new persistent thread while carrying only reduced project state.

Conversation bindings are URL/thread identities, not browser tab IDs.

For detailed persistent-team behavior see `TEAM_RUNTIME.md`. For tunnel composition and remote/local topologies see `TUNNEL_ARCHITECTURE.md`.
