# v0.7.0 summary

FancyGPT v0.7.0 retains the two-pass independent reasoning engine and introduces runtime-selectable browser **Tunnels**. Browser connectivity is no longer a single Playwright path: Site, Runtime and Transport are separate layers that compose into validated tunnel specs. Chrome, Edge and Firefox extension modes support local Native Messaging and remote WebSocket/SSH. CDP and Playwright remain explicit alternatives. CLI and MCP can inspect and select tunnels per request.
