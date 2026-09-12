# FancyGPT Browser Tunnel Extension

One codebase supports Chrome, Edge, and Firefox. Chrome/Edge use `chromium/manifest.json`; Firefox uses `firefox/manifest.json`. The common content/background code never reads or exports ChatGPT cookies, OAuth tokens, or local storage credentials.

Two transports are supported:

- **WebSocket**: the extension connects to `ws://127.0.0.1:8765`. For a remote development host, forward that remote bridge port to the workstation with SSH/VS Code Remote-SSH and keep the browser local.
- **Native Messaging**: the browser launches `fancy-gpt-native-host`, which proxies browser messages to a local FancyGPT bridge. This is useful when FancyGPT and the browser run on the same workstation.

Use `fancy-gpt extension export <dir>` after installation to export ready-to-load bundles.
