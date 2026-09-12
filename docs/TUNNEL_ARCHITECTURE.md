# Tunnel architecture — v0.8.0

A **Tunnel** is a runtime-selectable composition, not a browser-specific code path:

```text
Tunnel = Site + Runtime + Transport + Browser + Scope + Endpoint + Policy
```

The core review engine never branches on Chrome, Firefox, Playwright, CDP, WebSocket or Native Messaging.

## Layers

```text
L0  Reasoning Core
    Skills / Workflows / Domains / Planner / Context / Final validation
            ↓ ModelProvider

L1  Web Provider
    model.turn orchestration, prompt-size and response-binding contract
            ↓ BrowserDriver

L2  Site Layer
    ChatGPT semantics: fresh conversation, composer, assistant turn identity
            ↓

L3  Runtime Layer
    extension | Playwright | CDP | interactive
            ↓

L4  Transport Layer
    Native Messaging | WebSocket(+SSH) | local process | CDP | human
            ↓

L5  Tunnel Composition
    validated tuple + endpoint + token source + priority + health
            ↓

L6  CLI / MCP
    list, inspect, probe, select, override per request
```

Every layer has an independent contract/registry and tests. A tunnel is rejected at registry load when its tuple is invalid, for example `playwright + native-messaging`.

## Built-in tunnels

| Tunnel | Runtime | Transport | Scope | Browser |
|---|---|---|---|---|
| `chrome-extension-native-local` | extension | Native Messaging | local | Chrome |
| `edge-extension-native-local` | extension | Native Messaging | local | Edge |
| `firefox-extension-native-local` | extension | Native Messaging | local | Firefox |
| `chrome-extension-ws-remote` | extension | WebSocket/SSH | remote | Chrome |
| `edge-extension-ws-remote` | extension | WebSocket/SSH | remote | Edge |
| `firefox-extension-ws-remote` | extension | WebSocket/SSH | remote | Firefox |
| `chrome-cdp-local` | CDP | CDP | local | Chrome |
| `playwright-chromium-local` | Playwright | local process | local | Chromium |
| `playwright-firefox-local` | Playwright | local process | local | Firefox |
| `interactive-manual` | interactive | human | any | any |

Custom tunnel catalogs can be added explicitly with `FANCY_GPT_TUNNELS_FILE`. Built-ins cannot be silently overridden.

## Runtime selection

A request may set:

```yaml
tunnel: chrome-extension-ws-remote
```

or a policy:

```yaml
tunnel_policy: prefer-remote
```

CLI may override either:

```bash
fancy-gpt run request.yaml --tunnel firefox-extension-ws-remote
fancy-gpt run request.yaml --tunnel-policy prefer-extension
```

MCP exposes the same runtime selection and introspection surface.

## Remote workstation → remote development host

Recommended topology for VS Code Remote-SSH:

```text
LOCAL WORKSTATION                         REMOTE HOST
Chrome/Edge/Firefox                      FancyGPT bridge + MCP + repo
      │ extension                              ▲
      │ ws://127.0.0.1:8765                    │
      └──────── SSH LocalForward ───────────────┘
```

Remote:

```bash
fancy-gpt bridge init
fancy-gpt bridge serve
```

Local SSH config:

```sshconfig
Host datalink-build
    HostName <remote-host>
    User <user>
    LocalForward 127.0.0.1:8765 127.0.0.1:8765
```

Export the extension locally, load it in the browser, configure the pair token from `bridge init`, endpoint `ws://127.0.0.1:8765`, and the browser-specific `*-extension-ws-remote` tunnel ID.

The remote bridge binds loopback by default. Non-loopback exposure is rejected unless explicitly enabled.

## Per-tunnel endpoints/tokens

Multiple tunnels may coexist. Per-tunnel overrides avoid one global endpoint/token bottleneck:

```text
FANCY_GPT_TUNNEL_CHROME_EXTENSION_WS_REMOTE_ENDPOINT
FANCY_GPT_TUNNEL_CHROME_EXTENSION_WS_REMOTE_TOKEN_FILE
```

The normalized pattern applies to every tunnel ID.

## Cross-browser extension layering

The extension is one codebase with browser manifests:

```text
bridge_transport.js  ← WebSocket / Native Messaging only
background.js        ← extension runtime + tab/job lifecycle
site_chatgpt.js      ← ChatGPT DOM + response binding only
content.js           ← site dispatcher only
```

Chrome/Edge use a Manifest V3 service worker; Firefox uses background scripts. The build script produces deterministic Chromium and Firefox assets and release-gate verifies the packaged copies are synchronized.

## Failure model

- Site selector/UI drift → site adapter unhealthy; no prompt resend.
- Extension disconnected → tunnel unavailable; resolver may choose another tunnel.
- SSH tunnel missing → WebSocket tunnel unavailable.
- Native host missing → native tunnel unavailable.
- Playwright browser binary missing → Playwright tunnel unavailable.
- CDP endpoint missing → CDP tunnel unavailable.
- Invalid tuple → rejected before runtime.
- Explicitly disabled tunnel → rejected even when requested by ID.

No browser cookie, OAuth token, or ChatGPT private API token is transported through FancyGPT.
