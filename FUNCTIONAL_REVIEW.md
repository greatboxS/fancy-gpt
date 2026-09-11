# Independent functional review — fancy-gpt v0.7.0

Scope: reasoning pipeline plus tunnel composition/selection. Live ChatGPT DOM compatibility is excluded.

## Result

**PASS — no offline functional blocker found.**

## Reasoning model

- PASS: 6 canonical reasoning skills.
- PASS: 4 workflows remain orchestration profiles, not skills.
- PASS: 13 domains carry source/evidence policies.
- PASS: deterministic routing/capability resolution precedes any online request.
- PASS: planner plans research/evidence but does not decide the final technical answer.
- PASS: independent-design candidate isolation is code-enforced.
- PASS: final report accounts for research tasks and evidence requirements.
- PASS: consult/design/investigate/verify/write mode contracts are structurally enforced.
- PASS: independent-design removes candidate artifacts plus acquisition paths and free-form notes from both online prompts.
- PASS: offline two-pass execution reaches semantic validation without browser or network access.
- PASS: consult requires distinct options with trade-offs; verify verdicts are bounded; reusable deliverables reject placeholder content.

## Tunnel model

- PASS: Tunnel = Site + Runtime + Transport + Browser + Scope + Endpoint + Policy.
- PASS: Site, Runtime and Transport have independent registries/contracts.
- PASS: incompatible compositions fail before runtime.
- PASS: 10 built-in tunnels cover three extension browsers, Native Messaging, remote WebSocket/SSH, CDP, Playwright and interactive fallback.
- PASS: CLI/request/MCP can explicitly select tunnel at runtime or use a selection policy.
- PASS: MCP exposes tunnel components, static layer health, dynamic health and resolver selection.
- PASS: per-tunnel endpoint/token overrides support multiple simultaneous bridges.
- PASS: bridge worker registers exact tunnel IDs; controller jobs route only to a matching worker.
- PASS: wildcard worker registration is rejected and stale workers are excluded from routing.
- PASS: controller job timeouts are bounded by bridge policy; Native Messaging workers maintain heartbeat state.
- PASS: bridge worker probing is snapshot/cached so auto-selection does not accumulate N× connection timeouts.
- PASS: configured timeout propagates controller → bridge → extension site adapter.
- PASS: bridge binds loopback by default.

## Cross-browser extension model

- PASS: common codebase for Chrome/Edge/Firefox.
- PASS: transport, extension runtime, site adapter and content dispatcher are separate JS layers.
- PASS: Chrome/Edge use MV3 service worker; Firefox uses background scripts.
- PASS: browser-specific export patches default tunnel/browser identity.
- PASS: Native Messaging manifest shape differs correctly for Chromium vs Firefox.
- PASS: deterministic extension build/check prevents packaged assets from drifting from source.
