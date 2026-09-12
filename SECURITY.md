# Security and independence model — v0.7.0

## Filesystem boundary

- `ReviewEngine` checks `repo_root` against configured `allowed_roots` **before** any online planner interaction.
- Explicit absolute/`..` paths outside the repository fail closed.
- Broad scan/glob/search does not follow symlinks and prunes generated/cache trees.
- The real host path is redacted as `<local-repo-root>` in online prompts/ContextPack.
- Required context is re-checked after security, independence and budget filtering.

## Independent-design boundary

- Candidate designs marked `role: candidate-solution` are removed before planner metadata, ContextPack and final prompt creation.
- Git diff/include paths are suppressed in design mode when they can reveal the candidate implementation.
- Planner sees artifact metadata only, not artifact bodies.

## Prompt-injection boundary

Local artifacts and web content are treated as untrusted evidence. Planner/final prompts forbid following embedded instructions found inside source code, logs, documents, issue comments or web pages.

## Browser/session boundary

- Core/MCP never reads browser cookies, local storage, OAuth/access/session tokens or private ChatGPT endpoints.
- Existing-user-session access is performed by a browser runtime (extension/CDP) rather than copying credential state.
- Playwright uses a FancyGPT-owned persistent profile and cross-process profile lock.
- Every automatic turn submits once and binds to one logical assistant response identity; ambiguity, UI drift, auth failure and timeout fail closed.

## Tunnel/bridge boundary

- Tunnel composition is validated before registration: Site, Runtime, Transport, Browser and Scope must be compatible.
- Bridge binds loopback by default. Non-loopback bind requires explicit `--allow-non-loopback`.
- Remote mode is intended through SSH LocalForward, so bridge traffic and pair tokens are not exposed directly on LAN/WAN.
- Bridge pair token is generated randomly and stored in a per-user token file with mode `0600` where supported.
- Per-tunnel endpoint/token overrides allow multiple independent remote bridges without credential/config collision.
- Explicitly disabled tunnels cannot be selected even by ID.
- WebSocket/Native Messaging transport code is separate from ChatGPT DOM/site code.

## Native Messaging

- Chrome/Edge host manifests use `allowed_origins`.
- Firefox host manifests use `allowed_extensions` and an explicit Gecko extension ID.
- Linux/macOS use browser-defined manifest locations.
- Windows uses per-user NativeMessagingHosts registry keys when automatic installation is requested.

## Evidence integrity

- High/critical findings require concrete evidence.
- Every planned online research task must be represented in `research_trace`; completed tasks require source URLs.
- Every planner evidence requirement must be represented in `evidence_coverage`; `satisfied` requires concrete evidence.
- Research-trace URLs must also appear in the structured `sources` list.

## Live-site caveat

Offline tests verify tunnel contracts, bridge protocol, extension packaging, site/runtime separation and response binding. They do **not** claim current ChatGPT DOM selectors or a real Plus session are live-compatible; that remains a local smoke test because the site UI is outside this repository's control.

## v0.8 outbound context boundary

- Online eligibility is separate from filesystem readability: `SecretPolicy` can deny secret-bearing files or redact high-confidence credentials before ContextPack creation.
- Required P0 context that is denied by secret policy fails closed rather than silently marking the request complete.
- Broad content acquisition has explicit file/byte/time budgets and prunes common Yocto/generated trees including `downloads`, `sstate-cache`, `build-*`, and `tmp*`.
- Git revision/diff subprocesses have wall-time and output bounds; textconv is disabled for diff collection.

## Persistent project/session boundary

- Project history is an append-only event journal; raw browser chat is not the durable source of project truth.
- Model sessions receive reduced project state rather than unrestricted replay of historical conversations.
- ChatGPT conversation bindings are stored as thread URLs only; no browser credential material is copied into project state.
- Project completion requires evidence-backed acceptance plus required work completion and must not rely solely on an agent's claim of completion.
