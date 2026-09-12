# Tunnel setup guide

FancyGPT exposes exactly three browser routes:

| Tunnel | Browser |
|---|---|
| `chrome-remote` | Chrome |
| `edge-remote` | Edge |
| `firefox-remote` | Firefox |

The requested model site is a separate CLI/MCP field and does not change the
tunnel ID.

## Remote extension

On the FancyGPT host:

```bash
./install.sh --preset remote-extension --browser edge
fancy-gpt bridge serve
```

The private bundle is written below
`~/.local/share/fancy-gpt/remote-<browser>/`. Move its `extension/` directory to
the browser workstation and keep `PAIRING.txt` private.

Forward the bridge from the browser workstation:

```bash
ssh -o ExitOnForwardFailure=yes -N -L 8765:127.0.0.1:8765 <fancy-gpt-host>
```

Load the unpacked extension, enter the endpoint and pair token, then verify the
browser route:

```bash
fancy-gpt tunnels explain edge-remote
```

Use the same tunnel with either supported site:

```bash
fancy-gpt ask "Who are you?" --tunnel edge-remote --site chatgpt
fancy-gpt ask "Who are you?" --tunnel edge-remote --site gemini
```

## Local Native Messaging

When browser and FancyGPT run on the same operating system:

```bash
./install.sh --preset local-extension --browser edge
fancy-gpt extension native-manifest --browser edge --extension-id <id>
```

Native Messaging is another transport for the same `edge-remote` browser route;
it does not create a second tunnel ID.
