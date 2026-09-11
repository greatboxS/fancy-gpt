# fancy-gpt 0.7.0 Quick Start

## Choose the connection first

| Situation | Setup command | Tunnel |
|---|---|---|
| Chrome on another Windows/Linux machine | `./install.sh --preset remote-extension --browser chrome` | `chrome-extension-ws-remote` |
| Edge on another Windows/Linux machine | `./install.sh --preset remote-extension --browser edge` | `edge-extension-ws-remote` |
| Firefox on another Windows/Linux machine | `./install.sh --preset remote-extension --browser firefox` | `firefox-extension-ws-remote` |
| Chrome on the same machine | `./install.sh --preset local-extension --browser chrome` | `chrome-extension-native-local` |
| Edge on the same machine | `./install.sh --preset local-extension --browser edge` | `edge-extension-native-local` |
| Firefox on the same machine | `./install.sh --preset local-extension --browser firefox` | `firefox-extension-native-local` |
| Managed local Chromium | `./install.sh --with-playwright` | `playwright-chromium-local` |
| Managed local Firefox | install Playwright extra and Firefox runtime | `playwright-firefox-local` |
| Existing debug-enabled Chrome | configure loopback CDP | `chrome-cdp-local` |
| Manual copy/paste fallback | no setup | `interactive-manual` |

Remote extension means WebSocket over localhost/SSH. Local extension means
Native Messaging on the same OS; its host still uses the loopback bridge.

## Fastest path: browser workstation + remote Ubuntu/Linux

Run once on Ubuntu:

```bash
./install.sh --preset remote-extension --browser edge
```

Replace `edge` with `chrome` or `firefox`. The browser workstation may run
Windows or Linux. The preset installs the CLI and MCP server, registers
`fancy-gpt` with Codex when available, and creates a private load-unpacked bundle
under `~/.local/share/fancy-gpt/remote-<browser>`. Endpoint, exact tunnel ID, and pair
token are kept in `PAIRING.txt` with mode `0600`; do not commit or publish it.

Then start the loopback-only bridge:

```bash
fancy-gpt bridge serve
```

Copy only the generated `extension` directory to the browser workstation,
establish SSH LocalForward port 8765, and use the browser's Load unpacked flow.
Restart Codex after the first MCP registration. See
[`docs/TUNNEL_SETUP.md`](docs/TUNNEL_SETUP.md) for every built-in tunnel.

## Same-machine extension

```bash
./install.sh --preset local-extension --browser edge
```

Replace `edge` with `chrome` or `firefox`. Load the generated extension, copy
the ID shown by the browser, and run the exact `extension native-manifest`
command printed by the installer. Then start `fancy-gpt bridge serve`. No SSH is
used.

## 1. Install once on the host that runs FancyGPT

```bash
./install.sh
```

No Playwright browser is downloaded by default. To also install the local Playwright fallback:

```bash
./install.sh --with-playwright
```

Verify:

```bash
fancy-gpt test
fancy-gpt tunnels list
```

## 2. Recommended: local browser + remote development host

On the remote host:

```bash
fancy-gpt bridge init
fancy-gpt bridge serve
```

Copy the printed pair token.

On your local machine, forward the remote bridge through SSH:

```bash
ssh -L 8765:127.0.0.1:8765 <remote-host>
```

or put the equivalent `LocalForward` in the SSH host used by VS Code Remote-SSH.

Export/load the extension for your browser (`chrome`, `edge`, `firefox`), then configure:

```text
Transport: WebSocket / SSH
Endpoint: ws://127.0.0.1:8765
Pair token: <token from remote bridge init>
Tunnel ID: chrome-extension-ws-remote   # adapt browser name
```

Check from remote:

```bash
fancy-gpt tunnels health
fancy-gpt tunnels select --policy prefer-remote
```

## 3. Run from any project

```bash
cd my-project
fancy-gpt init
# edit fancy-gpt-request.yaml
fancy-gpt run fancy-gpt-request.yaml --tunnel chrome-extension-ws-remote
```

## 4. MCP

```bash
fancy-gpt mcp
```

The MCP caller may pass `tunnel_id` or `tunnel_policy` on each automatic request.

## 5. Useful tunnel diagnostics

```bash
fancy-gpt tunnels components
fancy-gpt tunnels list
fancy-gpt tunnels explain chrome-extension-ws-remote
fancy-gpt tunnels health
```
