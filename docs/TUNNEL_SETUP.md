# Tunnel setup guide

All bridge-backed tunnels bind `127.0.0.1` by default. Never replace it with
`0.0.0.0`; use SSH LocalForward when browser and FancyGPT run on different hosts.

## Remote extension: Chrome, Edge, Firefox (recommended)

On the FancyGPT Linux host, choose one browser:

```bash
./install.sh --preset remote-extension --browser edge
fancy-gpt bridge serve
```

Valid browser values are `chrome`, `edge`, and `firefox`. The private bundle is
`~/.local/share/fancy-gpt/remote-<browser>/`. Copy its `extension/` directory to
the Windows or Linux browser workstation. Keep `PAIRING.txt` private.

On a remote workstation, keep this running:

```bash
ssh -o ExitOnForwardFailure=yes -N -L 8765:127.0.0.1:8765 <fancy-gpt-host>
```

Load `extension/` unpacked in `chrome://extensions`, `edge://extensions`, or
`about:debugging#/runtime/this-firefox`, then enter the values from
`PAIRING.txt`. Verify with:

```bash
fancy-gpt tunnels explain <browser>-extension-ws-remote
```

## Local Native Messaging: Chrome, Edge, Firefox

Use this only when the browser and FancyGPT run on the same operating system:

```bash
fancy-gpt extension export <browser> ~/.local/share/fancy-gpt/extension-<browser>
fancy-gpt extension native-config --browser <browser>
```

Load the extension, obtain its extension ID, then install the browser-specific
native manifest:

```bash
fancy-gpt extension native-manifest --browser <browser> --extension-id <id>
```

The exact tunnel is `<browser>-extension-native-local`. Windows native-host
registration is platform-specific and is not automated by the Linux installer.

## Playwright Chromium or Firefox

Playwright is optional:

```bash
./install.sh --with-playwright
fancy-gpt tunnels explain playwright-chromium-local
```

Use `playwright-firefox-local` only after installing the Firefox Playwright
browser in the same isolated tool environment. These tunnels own their browser
process and do not use the extension.

## Chrome/Chromium CDP

Launch a dedicated browser profile with remote debugging restricted to
localhost, then verify:

```bash
fancy-gpt tunnels explain chrome-cdp-local
```

Do not expose the CDP port to a network. Do not point CDP at a normal profile
containing unrelated browsing data.

## Interactive fallback

No browser integration is installed:

```bash
fancy-gpt tunnels select --tunnel interactive-manual
```

FancyGPT prints the prompt for manual copy/paste and validates the imported
response. This is the fallback when automated tunnels are unavailable.

## Built-in mapping

| Tunnel | Simplest setup |
|---|---|
| `chrome-extension-ws-remote` | `./install.sh --preset remote-extension --browser chrome` |
| `edge-extension-ws-remote` | `./install.sh --preset remote-extension --browser edge` |
| `firefox-extension-ws-remote` | `./install.sh --preset remote-extension --browser firefox` |
| `chrome-extension-native-local` | `extension native-config --browser chrome` |
| `edge-extension-native-local` | `extension native-config --browser edge` |
| `firefox-extension-native-local` | `extension native-config --browser firefox` |
| `playwright-chromium-local` | `./install.sh --with-playwright` |
| `playwright-firefox-local` | install optional Playwright Firefox runtime |
| `chrome-cdp-local` | launch a dedicated loopback-only CDP profile |
| `interactive-manual` | no setup |
