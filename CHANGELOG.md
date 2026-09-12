# Changelog

## 0.7.0

- Introduced runtime-selectable **Tunnel** abstraction: Site + Runtime + Transport + Browser + Scope + Endpoint + Policy.
- Added independent Site, Runtime, Transport and Composition contract registries.
- Added static composition validation; incompatible tuples fail at catalog load.
- Added 10 built-in tunnels covering Chrome/Edge/Firefox extension local/remote, CDP, Playwright and interactive fallback.
- Added per-request tunnel/tunnel-policy selection through CLI, request schema and MCP.
- Added MCP tunnel introspection: sites, runtimes, transports, layer health, probe and selection.
- Added extension bridge mode for remote workstation → remote host over SSH LocalForward.
- Split extension into transport, runtime, ChatGPT site adapter and content-dispatch layers.
- Added Chrome/Edge/Firefox extension packaging from one common codebase.
- Added Native Messaging manifests for Chrome/Edge/Firefox, including Windows per-user registration support.
- Added site/runtime separation in Python: ChatGPT site semantics compose with Playwright or CDP page runtimes.
- Added per-tunnel endpoint/token overrides and explicit custom tunnel catalogs via `FANCY_GPT_TUNNELS_FILE`.
- Added cached bridge worker snapshot probing to avoid N× timeout during auto selection.
- Playwright probe now checks the browser executable, not merely the Python package.
- Bridge binds loopback by default; non-loopback exposure requires explicit override.
- Propagated configured job timeout through bridge → extension → site adapter.
- Added deterministic extension build/check script and tunnel architecture release gate.
- Default installer now favors extension tunnels; Playwright browser installation is opt-in.

## 0.6.0

- Added install-once distribution via `./install.sh` and `uv tool install`.
- Added standalone package self-test and six canonical skills / four workflow profiles / thirteen domains.
