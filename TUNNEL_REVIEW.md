# Independent tunnel architecture review — fancy-gpt v0.7.0

**Result: PASS.**

The tunnel subsystem is separated into independently testable contracts:

1. **Site** — ChatGPT semantics and DOM/turn behavior only.
2. **Runtime** — extension, Playwright, CDP, interactive, or fake browser execution.
3. **Transport** — Native Messaging, WebSocket/SSH, local process, CDP, human, or in-memory.
4. **Composition** — validates which Site × Runtime × Transport × Browser × Scope tuples are legal.
5. **Registry / Resolver / Health** — exposes available tunnels and selects one at runtime by explicit ID or policy.
6. **Bridge** — routes jobs only to browser workers that registered the requested tunnel IDs.
7. **CLI / MCP** — inspects and selects tunnels per request without changing ReviewEngine.

Static review result:

- Built-in tunnels: 10
- Extension tunnels: 6
- Invalid built-in compositions: 0
- Extension source/build/package drift: 0
- Bridge default bind: loopback
- Worker routing: exact tunnel IDs only; wildcard registration rejected
- Worker lifecycle: connected/stale/disconnected state with WebSocket and Native Messaging heartbeats
- Timeout policy: request timeout propagated but capped by bridge server policy
- Remote extension mode: WebSocket designed to be carried through SSH forwarding
- Per-tunnel endpoint/token overrides: supported
- Timeout propagation controller → bridge → extension: supported
- Chrome/Edge/Firefox extension packaging: supported from one shared source base with platform-specific manifests

Live browser/site compatibility remains an environment smoke-test item and is not conflated with the static tunnel architecture gate.
