# Persistent Engineering Team Runtime

## Goal

FancyGPT coordinates independent AI teammates, tools, model sessions, and browser tunnels around a long-lived engineering target until completion is supported by evidence.

```text
Target
  ↓
Discover / Research
  ↓
Design
  ↓
Plan
  ↓
Implement
  ↓
Build / Test
  ↓
Review
  ↓
Fix
  ↓
Verify
  ↓
Complete
```

The lifecycle is a work graph, not a mandatory monolithic pipeline. Dependencies decide which work item is ready.

## Roles

Initial roles are:

- `planner`
- `researcher`
- `designer`
- `implementer`
- `reviewer`
- `verifier`

A role is a behavioral contract, not a model identity. Tunnel/model selection remains independent.

Local mutation is not faked. Implementer work defaults to `external-agent` when repository mutation is required, so Codex/Claude or another local tool can receive a structured assignment and return a structured outcome.

## Durable handoff protocol

A teammate returns an `AgentOutcome` containing only durable project information:

- summary
- decisions
- evidence
- findings
- artifacts
- criterion assessments
- next actions
- confidence
- relevance assessment

This prevents agent-to-agent communication from becoming long narrative transcripts.

## Session history

Raw chat is not replayed as project memory. FancyGPT persists project events and reduces them into `RelevantProjectContext` containing only material target/work/decision/evidence/finding/artifact state.

A resumed model session may also bind to a ChatGPT Web conversation URL, but project truth remains in FancyGPT state rather than depending on browser history.

## Completion rule

`DONE` is evidence-driven. A project cannot become complete merely because an agent says it is complete.

Required completion signals include:

1. required work items completed/verified;
2. acceptance criteria satisfied;
3. required criterion evidence IDs exist in durable state;
4. no material open finding prevents completion.

## CLI

```bash
fancy-gpt project init Demo --target "Ship verified feature" --acceptance "Runtime tests pass"
fancy-gpt project bootstrap <project-id>
fancy-gpt project status <project-id>
fancy-gpt project continue <project-id>
fancy-gpt project run-next <project-id> --tunnel edge-remote
fancy-gpt project sessions <project-id>
fancy-gpt project history <project-id>
fancy-gpt project context <project-id>
```

External agents can receive and complete assignments through `project assign`, `project start-session`, and `project submit-outcome` or the equivalent MCP tools.

## Focused questions

A small question does not need a two-pass review report:

```bash
fancy-gpt ask "Does this ADC input require software control?" --intent focused
```

The same semantic relevance policy applies: sufficiently complete for the current intent, not exhaustively broad.
