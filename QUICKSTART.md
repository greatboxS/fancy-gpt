# fancy-gpt 0.7.0 Quick Start

## Fastest path: Windows Edge + remote Ubuntu

Run once on Ubuntu:

```bash
./install.sh --preset remote-edge
```

The preset installs the CLI and MCP server, registers `fancy-gpt` with Codex
when the Codex CLI is available, and creates a private load-unpacked Edge bundle
under `~/.local/share/fancy-gpt/remote-edge`. Endpoint, exact tunnel ID, and pair
token are kept in `PAIRING.txt` with mode `0600`; do not commit or publish it.

Then start the loopback-only bridge:

```bash
fancy-gpt bridge serve
```

Copy only the generated `extension` directory to Windows, establish SSH
LocalForward port 8765, and load it from `edge://extensions`. Restart Codex after
the first MCP registration.

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
