# Test report — fancy-gpt v0.7.0

## Release result

**PASS for source, layered tunnel architecture, extension packaging, wheel artifact, installer contract, and offline end-to-end execution.**

Environment-dependent live checks intentionally remain outside this offline release build: real ChatGPT Web UI/session compatibility using a Plus account and real MCP SDK import/startup in this container. The container does not have the `mcp` package installed, so the direct MCP import test is skipped; mocked MCP registration/contract tests pass.

## Source suite

- Python compileall: PASS
- Tests passed: **67**
- Tests skipped: **1** (`tests/test_mcp_import.py`, package `mcp` unavailable in container)
- Tests failed: **0**
- Functional reasoning review: PASS
- Tunnel architecture review: PASS
- Release gate: PASS
- Deterministic extension build check: PASS
- Shell syntax (`install.sh`, `uninstall.sh`): PASS

## Catalog and layered architecture

- Agent Skills: **6**
- Workflow profiles: **4**
- Domains: **13**
- Built-in Tunnels: **10**
- Sites: **1** (`chatgpt`)
- Browser runtimes: **5** (`extension`, `playwright`, `cdp`, `interactive`, `fake`)
- Transports: **6** (`native-messaging`, `websocket`, `local-process`, `cdp`, `human`, `in-memory`)
- Extension tunnels: **6** (Chrome/Edge/Firefox × local native / remote WebSocket)
- Static composition failures: **0**

Layer contract coverage includes Site, Runtime, Transport, Tunnel composition, resolver/health, bridge routing, browser response binding, extension packaging, CLI tunnel selection, and MCP tunnel introspection contracts.

## Install-once / usability checks

- Bundled-wheel installer path: PASS
- `uv tool install` isolated user-tool contract: PASS
- Playwright browser download remains explicit opt-in: PASS
- Installer runs standalone offline self-test: PASS
- Installer runs runtime verify: PASS
- Installer prints bridge pair-token information after initialization: PASS
- `extension native-config --browser edge/firefox` derives the matching browser-specific native tunnel by default: PASS
- `fancy-gpt version`: PASS (`0.7.0`)
- `fancy-gpt test`: PASS without browser/network/account
- `fancy-gpt init`: PASS
- `fancy-gpt tunnels list/components/explain/select`: covered

## Wheel checks

Artifact: `dist/fancy_gpt-0.7.0-py3-none-any.whl`

SHA-256: `6fbbc0997cd77723ff9b41647557108f0fa2268fc15661fcd26f0730cab91cc5`

- Packaged Agent Skills: **6**
- Packaged catalog YAMLs: **4** (`skills`, `workflows`, `domains`, `tunnels`)
- Packaged Chromium extension assets: **7**
- Packaged Firefox extension assets: **7**
- Console entrypoints: `fancy-gpt`, `fancy-gpt-mcp`, `fancy-gpt-native-host`
- Wheel-extracted `fancy-gpt test`: PASS
- Wheel-extracted tunnel component inspection: PASS
- Offline two-pass self-test from wheel: COMPLETE

## Browser / tunnel scope

Implemented and offline-tested tunnel families:

- Chrome / Edge / Firefox extension + Native Messaging (local)
- Chrome / Edge / Firefox extension + WebSocket, designed for SSH forwarding (remote)
- Chrome CDP local attach (advanced)
- Playwright Chromium / Firefox local fallback
- Interactive/manual fallback

The release does **not** claim live ChatGPT Web selector/session verification in this container. ChatGPT can change its UI independently; the Site layer is isolated specifically so UI drift can be diagnosed and updated without changing Runtime/Transport/Reasoning layers.

## MCP scope

The stdio MCP server, tunnel selection arguments, and tunnel introspection tools are implemented and covered by mocked MCP contract tests. A real `mcp` import/startup smoke must be run after normal installation on the target machine because this offline build container does not contain the MCP SDK.
