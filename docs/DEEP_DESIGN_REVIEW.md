# Design review result — v0.7.0

## Verdict

PASS after refactoring browser connectivity into independently testable Site, Runtime, Transport and Tunnel Composition layers.

## Key decisions

1. Tunnel identity is configuration/data, not branching in ReviewEngine.
2. ChatGPT DOM assumptions live only in the Site layer.
3. Browser lifecycle is a Runtime concern.
4. Remote/local connectivity is a Transport concern.
5. Invalid runtime/transport/browser tuples fail at registry load.
6. Bridge endpoints/tokens are per tunnel so multiple browser paths can coexist.
7. Extension remote mode uses loopback + SSH forwarding by default.
8. Playwright is a fallback, not the required primary path.
9. CLI/MCP expose tunnel introspection and runtime selection.
10. Live ChatGPT selectors are outside offline release claims.
