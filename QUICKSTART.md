# fancy-gpt 0.7.0 Quick Start

This guide takes you from a fresh installation to your first FancyGPT request.
The recommended setup keeps ChatGPT in your local browser while FancyGPT runs
on a remote Ubuntu/Linux development host.

## Before you begin

This guide uses two machines:

- **Development host**: the Ubuntu/Linux machine where you run FancyGPT and
  Codex. It may be a remote server reached through SSH.
- **Browser workstation**: the Windows or Linux machine where Chrome, Edge, or
  Firefox is installed and where you are already signed in to ChatGPT.

Choose one browser name and use it consistently throughout the guide:
`chrome`, `edge`, or `firefox`.

The browser extension talks to the remote FancyGPT bridge through an SSH local
port forward. The bridge remains bound to `127.0.0.1`; do not expose port 8765
directly to a LAN or the internet.

## 1. Install FancyGPT on the development host

Clone or open the FancyGPT repository, then run the preset for your browser.
This example uses Edge:

```bash
./install.sh --preset remote-extension --browser edge
```

For Chrome or Firefox, replace `edge` with `chrome` or `firefox`.

The installer:

1. installs the FancyGPT CLI and MCP server;
2. registers the MCP server with Codex when Codex is available;
3. creates a browser-specific extension bundle;
4. creates or reuses a private bridge pair token; and
5. writes the extension connection values to `PAIRING.txt`.

The generated bundle is stored at:

```text
~/.local/share/fancy-gpt/remote-<browser>/
├── extension/     # copy this directory to the browser workstation
└── PAIRING.txt    # private connection values; do not copy this file
```

For example, the Edge bundle is:

```text
~/.local/share/fancy-gpt/remote-edge/
```

If the installer registered FancyGPT with Codex for the first time, restart
Codex before using the MCP tools.

## 2. Read and copy the connection values

On the development host, display `PAIRING.txt`. For Edge:

```bash
cat ~/.local/share/fancy-gpt/remote-edge/PAIRING.txt
```

Use `remote-chrome` or `remote-firefox` for the other browsers. The output has
this form:

```text
Endpoint: ws://127.0.0.1:8765
Tunnel ID: edge-remote
Browser: edge
Pair token: <secret-token>
```

Keep this terminal open or copy the four values to a secure temporary note.
For the pair token, copy only the value after `Pair token:`—do not include the
label or whitespace. Treat it as a password: do not commit, publish, or send
`PAIRING.txt` to another person.

If you installed without a preset, generate and display the same information
with:

```bash
fancy-gpt bridge init
```

That command prints JSON. Copy only the string inside `pair_token`, without the
quotation marks:

```json
{
  "endpoint": "ws://127.0.0.1:8765",
  "token_file": "/home/<user>/.local/share/fancy-gpt/bridge-token",
  "pair_token": "<copy-only-this-value>"
}
```

## 3. Copy the extension to the browser workstation

Copy only the generated `extension/` directory from the development host to a
permanent directory on the browser workstation. Do not copy `PAIRING.txt`.

For example, with `scp` from a Linux browser workstation:

```bash
scp -r <user>@<development-host>:~/.local/share/fancy-gpt/remote-edge/extension ./fancy-gpt-extension
```

On Windows, you may use the VS Code file explorer, WinSCP, or another SSH file
transfer tool. Keep the copied directory after loading it: Chrome and Edge use
its files directly.

## 4. Load the extension in the browser

### Chrome

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Select the copied `extension/` directory.
5. Pin **FancyGPT Tunnel** to the browser toolbar if desired.

### Edge

1. Open `edge://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Select the copied `extension/` directory.
5. Pin **FancyGPT Tunnel** to the browser toolbar if desired.

### Firefox

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on**.
3. Select `manifest.json` inside the copied `extension/` directory.

Firefox removes a temporary add-on when the browser exits, so repeat these
steps after restarting Firefox unless you install a signed package.

## 5. Enter the endpoint and pair token

Click the **FancyGPT Tunnel** icon in the browser toolbar. Fill in every field
using the values from `PAIRING.txt`:

| Field | Value for Edge | Chrome/Firefox |
|---|---|---|
| Transport | `WebSocket / SSH` | same |
| Bridge endpoint | `ws://127.0.0.1:8765` | same |
| Pair token | value after `Pair token:` | same |
| Tunnel ID | `edge-remote` | replace `edge` with the browser name |
| Browser name | `edge` | `chrome` or `firefox` |

Click **Save & reconnect**. The extension stores these values in its local
extension storage. You normally enter the token only once; enter it again if
you remove the extension or clear its storage.

At this point the connection may still show as disconnected. That is expected
until both the SSH port forward and bridge are running.

## 6. Start the SSH local port forward

On the browser workstation, open a terminal and connect to the development
host with local port 8765 forwarded:

```bash
ssh -o ExitOnForwardFailure=yes -N -L 8765:127.0.0.1:8765 <user>@<development-host>
```

Keep this terminal running while using FancyGPT. The command intentionally
shows no prompt after connecting.

If you use VS Code Remote-SSH, you can instead add the forward to the SSH host
entry on the browser workstation:

```sshconfig
Host my-development-host
    HostName <development-host>
    User <user>
    LocalForward 127.0.0.1:8765 127.0.0.1:8765
```

Reconnect the SSH or VS Code session after changing this file.

## 7. Start the FancyGPT bridge

On the development host, open a separate terminal and run:

```bash
fancy-gpt bridge serve
```

Keep the bridge running while using FancyGPT. Return to the browser, open the
**FancyGPT Tunnel** popup, and click **Save & reconnect** again if it does not
reconnect automatically.

## 8. Verify the connection

On the development host, run:

```bash
fancy-gpt tunnels health
fancy-gpt tunnels explain edge-remote
```

Replace `edge` with your browser name. If the tunnel is healthy, select the
remote-extension policy:

```bash
fancy-gpt tunnels select --policy prefer-remote
```

If verification fails, check these items in order:

1. `fancy-gpt bridge serve` is still running on the development host;
2. the SSH port-forward command is still running on the browser workstation;
3. the extension's endpoint is exactly `ws://127.0.0.1:8765`;
4. the pair token contains only the value, with no `Pair token:` label;
5. the Tunnel ID and Browser name match the browser loaded with the extension.

Useful diagnostics:

```bash
fancy-gpt sites list
fancy-gpt tunnels components
fancy-gpt tunnels list
fancy-gpt tunnels health
```

## 9. Run the first request

On the development host, change to the project you want to work on:

```bash
cd my-project
fancy-gpt init
```

Edit the generated `fancy-gpt-request.yaml`, then run it through the exact
browser tunnel:

```bash
fancy-gpt run fancy-gpt-request.yaml --tunnel edge-remote
```

Replace `edge` with `chrome` or `firefox` when applicable. Keep the browser
open and signed in to ChatGPT until the request completes.

## 10. Use FancyGPT through MCP

To start the MCP server directly:

```bash
fancy-gpt mcp
```

An MCP caller may pass `tunnel_id` for an exact tunnel or `tunnel_policy` to
let FancyGPT choose a compatible tunnel. If the installer registered the MCP
server automatically, restart Codex once and use the registered FancyGPT tools.

## Same-machine browser alternative

Use Native Messaging when FancyGPT and the browser run on the same operating
system. SSH is not needed.

```bash
./install.sh --preset local-extension --browser edge
```

The extension is created at:

```text
~/.local/share/fancy-gpt/local-edge/extension/
```

Replace `edge` with your browser name, then:

1. load the extension using the browser steps above;
2. copy the extension ID shown on the browser's extension page;
3. run the exact command printed by the installer:

   ```bash
   fancy-gpt extension native-manifest --browser edge --extension-id <extension-id>
   ```

4. run `fancy-gpt bridge serve`;
5. open **FancyGPT Tunnel**, select `Native Messaging`, confirm the browser
   name and `<browser>-remote` tunnel ID, then click
   **Save & reconnect**.

Windows native-host registration is platform-specific and is not automated by
the Linux installer.

The three tunnel IDs are `chrome-remote`, `edge-remote`, and `firefox-remote`.
Site selection is independent, for example `--site gemini --tunnel edge-remote`.

See [`docs/TUNNEL_SETUP.md`](docs/TUNNEL_SETUP.md) for full tunnel setup and
security details.
