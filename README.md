# fancy-gpt 0.8.0

FancyGPT is a **persistent engineering-team runtime** that coordinates focused technical reasoning, independent review, long-lived project state, AI teammates, MCP clients, and runtime-selectable ChatGPT Web tunnels without requiring an OpenAI API key.

Independent review remains a core capability; it is no longer the identity of the whole product.

## Install once

```bash
./install.sh
```

The default install is lightweight and does **not** download a private Chromium runtime. Playwright remains an explicit fallback:

```bash
./install.sh --with-playwright
```

The installer detects Codex and Claude Code and attempts idempotent user-level MCP registration. Disable this with:

```bash
./install.sh --no-mcp-register
```

Verify:

```bash
fancy-gpt version
fancy-gpt test
fancy-gpt doctor
fancy-gpt tunnels list
fancy-gpt clients list
```

## Product model

```text
Project / Mission
      ↓
Target + Acceptance Criteria
      ↓
Team Orchestrator
      ↓
Work Items + Agent Roles
      ↓
Persistent Sessions
      ↓
Reduced Project Context
      ↓
Reasoning / Focused / Team Engines
      ↓
ExecutionCoordinator
      ↓
Model Provider
      ↓
Runtime-selected Tunnel
      ↓
ChatGPT Web / browser runtime
```

The current team roles are `planner`, `researcher`, `designer`, `implementer`, `reviewer`, and `verifier`. Roles are independent from model/tunnel choice.

## Semantic relevance instead of hard verbosity limits

FancyGPT carries a semantic `RelevanceSufficiencyPolicy` across sessions:

> Expand only when expansion changes correctness, decision quality, material risk, confidence, a blocking unknown, or the next required action. Otherwise preserve the caller's scope.

Completeness means **sufficient for the current intent**, not exhaustive coverage of the surrounding topic. This policy is compiled into planner, final, focused-answer, and engineering-teammate requests and validated through structured relevance assessments.

For a narrow question use the focused one-pass path:

```bash
fancy-gpt ask "Does this pin need software control?" --intent focused
```

## Persistent engineering projects

```bash
fancy-gpt project init datalink-qos \
  --target "Deliver production-ready QoS for C2, telemetry, and media" \
  --acceptance "C2 remains responsive under media saturation" \
  --acceptance "Runtime evidence is recorded"

fancy-gpt project bootstrap datalink-qos
fancy-gpt project status datalink-qos
fancy-gpt project continue datalink-qos
fancy-gpt project run-next datalink-qos --tunnel edge-extension-ws-remote
```

Project state is append-only and persists targets, work items, sessions, decisions, evidence, findings, artifacts, and next actions. Model prompts receive a reduced relevant projection instead of raw historical chats.

Local mutation is not simulated: implementer work can be handed to Codex/Claude as an `external-agent` assignment and returned through a structured outcome contract.

See `docs/TEAM_RUNTIME.md`.

## Core reasoning capabilities

FancyGPT still exposes the existing two-pass reasoning stack:

- **6 Skills** — reasoning primitives.
- **4 Workflows** — orchestration profiles.
- **13 Domains** — technical source/evidence policy.
- **Focus** — request-specific technical lens.

Skills: `technical-review`, `independent-design`, `technical-consult`, `root-cause-investigation`, `evidence-verification`, `technical-writing`.

Workflows: `deep-design-review`, `change-impact-assessment`, `production-readiness`, `conformance-audit`.

## Two-pass independent reasoning

```text
Request
  ↓ deterministic routing/capability resolution
Online Pre-Request Planner
  ↓ ResearchManifest
Local ContextBuilder
  ↓ Secret/DLP filter + bounded acquisition
  ↓ ContextPack
Final online model
  ↓ structured FinalReport
Semantic relevance + evidence validation
```

Independent-design candidate metadata/content is removed before online planning/final prompts. Outbound context rejects intrinsically secret-bearing files and redacts high-confidence credential patterns.

## Tunnel architecture

A tunnel remains a runtime-selected composition:

```text
Tunnel = Site + Runtime + Transport + Browser + Scope + Endpoint + Policy
```

Built-in families include Chrome/Edge/Firefox extension tunnels (Native Messaging local or WebSocket/SSH remote), Chrome CDP, Playwright Chromium/Firefox fallback, and interactive mode.

Useful diagnostics:

```bash
fancy-gpt tunnels list
fancy-gpt tunnels components
fancy-gpt tunnels inspect edge-extension-ws-remote
fancy-gpt tunnels health
fancy-gpt tunnels select --policy prefer-remote
```

`inspect` performs deeper site readiness diagnostics for extension tunnels. Transport connectivity and ChatGPT site readiness are intentionally distinct states.

## Recommended Windows Edge + Ubuntu VM/remote topology

```text
WINDOWS HOST                                UBUNTU VM / REMOTE HOST
Edge + FancyGPT extension                   FancyGPT bridge + MCP + repo
       │ ws://127.0.0.1:8765                       ▲
       └──────────── SSH LocalForward ─────────────┘
```

Remote host:

```bash
fancy-gpt bridge init
fancy-gpt bridge serve
```

Windows/local workstation:

```text
ssh -L 8765:127.0.0.1:8765 <remote-host>
```

Export/load the Edge extension, configure `ws://127.0.0.1:8765`, the pair token, and tunnel `edge-extension-ws-remote`.

The bridge binds loopback by default; use SSH forwarding instead of exposing it on LAN/WAN.

## ChatGPT Web conversation policy

Persistent project sessions can choose:

- `fresh` — independent work in a temporary ChatGPT thread;
- `resume` — resume a saved ChatGPT conversation URL;
- `fork` — start a new persistent thread with only reduced relevant project state.

Conversation URLs are session bindings, not project truth. Durable project state remains local in FancyGPT.

## MCP

```bash
fancy-gpt mcp
```

FancyGPT can be registered explicitly:

```bash
fancy-gpt clients register codex
fancy-gpt clients register claude-code
```

MCP exposes review/tunnel tools plus project/team operations such as project creation/status/context/history, session assignments/outcomes, evidence/findings/artifacts, focused questions, project stepping, and execution diagnostics.

## Execution diagnostics

Automatic review requests create an execution record **before** tunnel selection, so failures can be localized to tunnel/provider/planner/context/validation phases.

```bash
fancy-gpt execution recent
fancy-gpt execution status <execution-id>
```

## Security boundaries

- Core/MCP never imports browser cookies, OAuth/access tokens, or private ChatGPT endpoints.
- Bridge binds loopback by default.
- Browser worker registration uses exact tunnel IDs; stale workers are excluded.
- Native Messaging enforces browser framing/size constraints and binary stdio on Windows.
- Filesystem context is confined to allowed roots.
- Outbound context has an independent secret/DLP policy.
- Repository scanning and git subprocesses are bounded.
- Local/web/project content is untrusted evidence, not instructions.

See `SECURITY.md`, `docs/ARCHITECTURE.md`, `docs/TEAM_RUNTIME.md`, and `docs/TUNNEL_ARCHITECTURE.md`.

## Offline verification

```bash
fancy-gpt test
```

From source:

```bash
python scripts/build_extension.py --check
python -m compileall -q src tests scripts
pytest -q
PYTHONPATH=src python scripts/functional_review.py
PYTHONPATH=src python scripts/tunnel_review.py
PYTHONPATH=src python scripts/release_gate.py
```

Live ChatGPT Web UI/account compatibility remains a user-controlled smoke test because the external site can change independently from FancyGPT.
