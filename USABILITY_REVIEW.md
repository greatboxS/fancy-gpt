# Independent usability review — fancy-gpt v0.7.0

## Verdict

**PASS** for install-once + runtime-selectable tunnel workflow.

## Normal user journey

Remote development host:

```bash
./install.sh
fancy-gpt bridge init
fancy-gpt bridge serve
```

Local workstation browser:

1. Load exported FancyGPT extension for Chrome/Edge/Firefox.
2. Configure the pair token.
3. Use `ws://127.0.0.1:8765` through SSH LocalForward.

Then from any remote project:

```bash
fancy-gpt init
fancy-gpt run fancy-gpt-request.yaml --tunnel chrome-extension-ws-remote
```

The user does not need to activate a Python environment or install a second browser for the default extension path.

## Runtime diagnostics

```bash
fancy-gpt tunnels components
fancy-gpt tunnels list
fancy-gpt tunnels explain <id>
fancy-gpt tunnels health
fancy-gpt tunnels select --policy prefer-remote
```

This exposes layer contracts and resolver decisions before a model request is sent.

## Fallbacks

- Extension + Native Messaging: same-host browser/tool.
- Extension + WebSocket/SSH: recommended remote workflow.
- CDP: advanced explicit attach.
- Playwright: optional local fallback, not default install path.
- Interactive/manual: last-resort compatibility.

## Failure behavior

Invalid compositions fail at catalog load. Missing extension/SSH/native host/CDP/Playwright runtime makes only that tunnel unavailable. Resolver can select another healthy tunnel. Bridge refuses non-loopback exposure by default.
