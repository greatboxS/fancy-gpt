# Architecture — v0.7.0

## Product layers

```text
Agent / CLI / MCP caller
        ↓
Reasoning Engine
  RequestClassifier
  CapabilityResolver
  Online Planner
  ContextBuilder
  Final Request Builder
  Report Validator
        ↓
ModelProvider
        ↓
Tunnel Manager
        ↓
Site Adapter
        ↓
Browser Runtime
        ↓
Transport
```

The only layer allowed to know repository/project context is the reasoning engine. Browser layers only receive the compiled prompt plus request/stage identity.

For detailed tunnel composition and remote/local topologies see `TUNNEL_ARCHITECTURE.md`.
