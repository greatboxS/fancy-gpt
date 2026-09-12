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

## Separation rules

- **Project state** owns durable target, work graph, sessions, decisions, evidence, findings, artifacts, and completion criteria.
- **Team orchestration** decides which bounded work item is ready and which role should act next.
- **Reasoning engines** solve one bounded task; they do not own browser lifecycle or project persistence.
- **ExecutionCoordinator** creates diagnostic state before tunnel/provider work can fail.
- **Tunnel** selects how a model interaction executes; it is not an agent role and not project state.
- **Site/Runtime/Transport** stay independent and contract-tested.

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
