# Tunnel architecture

A tunnel is a route to one remote browser. It does not select a model site.

```text
request.site   -> site adapter (chatgpt, gemini, ...)
request.tunnel -> browser route (chrome-remote, edge-remote, firefox-remote)
```

The two request fields remain independent until the bridge dispatches the job to
the chosen browser. The browser extension then selects the requested site
adapter. Neither `TunnelSpec`, tunnel health, nor tunnel selection contains a
site default.

## Layers

```text
CLI / MCP request
  |-- site -----> web provider -----> site adapter
  `-- tunnel ---> bridge route -----> remote browser
                                      |
                                      `---- dispatch job(site)
```

Runtime (`extension`) and transport (`websocket`, optionally Native Messaging)
are implementation layers under a browser route. They do not create additional
tunnel identities.

## Built-in tunnels

| Tunnel | Browser |
|---|---|
| `chrome-remote` | Google Chrome |
| `edge-remote` | Microsoft Edge |
| `firefox-remote` | Firefox |

A request can select both dimensions independently:

```yaml
site: gemini
tunnel: edge-remote
```

or through CLI/MCP arguments:

```bash
fancy-gpt ask "Review this" --site gemini --tunnel edge-remote
```

## Remote topology

```text
LOCAL WORKSTATION                         REMOTE HOST
Chrome/Edge/Firefox                      FancyGPT bridge + MCP + repo
      | extension                              ^
      | ws://127.0.0.1:8765                    |
      `-------- SSH LocalForward --------------'
```

The bridge binds loopback by default. Use SSH forwarding rather than exposing
it directly to a network.

Per-route endpoint and token overrides use the normalized tunnel ID, for
example `FANCY_GPT_TUNNEL_EDGE_REMOTE_ENDPOINT` and
`FANCY_GPT_TUNNEL_EDGE_REMOTE_TOKEN_FILE`.
