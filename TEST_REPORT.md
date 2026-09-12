# Test report — fancy-gpt v0.8.0

## Result

PASS for the offline/source release boundary. Live ChatGPT Web UI/account compatibility remains an explicit operator-controlled smoke because the UI and account session are external. The current build container also lacks the `mcp` Python package, so the direct MCP import test is skipped here; mocked MCP contracts remain covered.

## Source suite

- Tests collected: **111**
- Tests passed: **110**
- Tests skipped: **1** (`tests/test_mcp_import.py`, `mcp` unavailable in this container)
- Tests failed: **0**
- Python compileall: PASS
- Functional reasoning review: PASS
- Tunnel architecture review: PASS
- Deterministic extension build consistency: PASS
- Hardened release gate: PASS

## v0.8 runtime coverage

Covered by tests and offline self-test:

- semantic Relevance/Sufficiency policy in planner/final/focused/team paths;
- persistent Project/Target/AcceptanceCriterion/WorkItem/Session state;
- append-only project journal and restart/reload behavior;
- reduced relevant project context instead of raw chat replay;
- agent roles and structured assignments/outcomes;
- evidence-gated project completion;
- verifier-created evidence bound to acceptance criteria in the same outcome;
- review findings and structured finding resolution by implementer outcomes;
- conditional skip of unnecessary fix work when review is clean;
- `fresh` / `resume` / `fork` ChatGPT conversation strategies;
- ExecutionCoordinator coverage for review, focused answer, and team-agent turns;
- outbound secret/DLP redaction/deny behavior;
- bounded context acquisition and bounded Git execution;
- exact tunnel IDs with wildcard worker rejection;
- browser worker heartbeat/stale behavior and bridge timeout bounds;
- extension site-readiness round trip;
- Windows Native Messaging framing constraints;
- Chrome/Edge/Firefox extension package consistency;
- Codex/Claude Code MCP client registration contracts.

## Catalog / tunnel invariants

- Skills: **6**
- Workflow profiles: **4**
- Domains: **13**
- Built-in tunnels: **10**
- Site adapters: **1** (`chatgpt`)
- Browser runtimes: **5**
- Transport contracts: **6**

## Release gate

`scripts/release_gate.py` now verifies more than file counts. It runs:

1. source pytest suite;
2. functional review;
3. tunnel architecture review;
4. deterministic extension source/build consistency;
5. wheel build without dependency resolution;
6. source ↔ wheel package parity;
7. installed-wheel offline self-test;
8. catalog, schema, skill bundle, and documentation invariants.

The generated `release-gate.json` is the machine-readable result for the final tree.

## Environment-dependent checks

Not claimed by this offline gate:

- live ChatGPT Web selector/account compatibility;
- a real Edge/Chrome/Firefox Plus-account turn;
- real MCP import/startup in this build container when `mcp` is absent;
- GitHub push/network reachability from this container.

## Final wheel artifact

- Path: `dist/fancy_gpt-0.8.0-py3-none-any.whl`
- SHA-256: `535043bea3a8ee44b59e5c0c1d8fdb56b21a01dc69a9045199da390c3c943e67`
- Source ↔ wheel package parity: PASS
- Installed-wheel offline self-test: PASS
- Playwright remains optional (`automation` extra), not a default dependency.
