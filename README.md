# fancy-gpt 0.8.0

Use [QUICKSTART.md](QUICKSTART.md) to choose among the three built-in browser tunnels.
Detailed setup and verification for every connection type is in
[docs/TUNNEL_SETUP.md](docs/TUNNEL_SETUP.md).

Install-once independent technical reasoning toolkit with MCP, an online pre-request planner, and **runtime-selectable browser tunnels** for ChatGPT and Gemini Web without requiring their public model APIs.

Development is organized around two stable, independent integration surfaces:

- **MCP tools** let an existing agent delegate bounded work to FancyGPT.
- **Model Gateway** will let Codex, Claude Code, and Gemini-compatible clients use a FancyGPT web model as their direct model provider.

The current delivery order and acceptance gates are tracked in [ROADMAP.md](docs/ROADMAP.md). The planned gateway protocols and context model are specified in [MODEL_GATEWAY.md](docs/MODEL_GATEWAY.md).

## Install once

```bash
./install.sh
```

The default install is lightweight: it installs the CLI/MCP/bridge and extension assets, but does **not** download a private Chromium runtime. Playwright remains an optional fallback:

```bash
./install.sh --with-playwright
```

After installation:

```bash
fancy-gpt test
fancy-gpt doctor
fancy-gpt tunnels list
fancy-gpt sites list
fancy-gpt tunnels components
```

## Core reasoning model

FancyGPT deliberately keeps reasoning and browser connectivity orthogonal:

- **6 Skills** = reasoning primitives.
- **4 Workflows** = orchestration profiles composed from skills.
- **13 Domains** = technical source/evidence policy.
- **Focus** = request-specific technical lens.
- **Tunnel** = runtime-selected online connection composition.

Skills:

`technical-review`, `independent-design`, `technical-consult`, `root-cause-investigation`, `evidence-verification`, `technical-writing`.

Workflows:

`deep-design-review`, `change-impact-assessment`, `production-readiness`, `conformance-audit`.

## Two-pass online reasoning

```text
Request
  ↓ deterministic routing/capability resolution
Online Pre-Request Planner
  ↓ ResearchManifest
Local ContextBuilder
  ↓ ContextPack
Final online model
  ↓ structured FinalReport
Evidence/research validation
```

The planner researches **what must be investigated, which online/local tools are needed, which sources should be preferred, and what evidence the final answer must contain**. It is explicitly forbidden from deciding the final technical answer.

## Tunnel architecture

A tunnel is a validated composition:

```text
Tunnel = Site + Runtime + Transport + Browser + Scope + Endpoint + Policy
```

Layers are independent and contract-tested:

```text
Reasoning Core
    ↓
Web Model Provider
    ↓
Site Adapter        (ChatGPT semantics only)
    ↓
Browser Runtime     (extension / Playwright / CDP / interactive)
    ↓
Transport           (Native Messaging / WebSocket+SSH / local process / CDP / human)
    ↓
Tunnel Composition  (runtime-selectable tuple + health + priority)
    ↓
CLI / MCP
```

Built-in tunnel families:

- Chrome / Edge / Firefox extension + Native Messaging (local).
- Chrome / Edge / Firefox extension + WebSocket, normally carried through SSH (remote).
- Chrome CDP (advanced local attach).
- Playwright Chromium / Firefox (fallback local runtime).
- Interactive/manual fallback.

Inspect them:

```bash
fancy-gpt tunnels list
fancy-gpt sites list
fancy-gpt tunnels components
fancy-gpt tunnels explain chrome-remote
fancy-gpt tunnels health
fancy-gpt tunnels select --policy prefer-remote
```

Choose one at runtime:

```bash
fancy-gpt run request.yaml --tunnel firefox-remote
```

or encode it in the request:

```yaml
tunnel: chrome-remote
```

Policy selection is also available:

```yaml
tunnel_policy: prefer-extension
```

Custom tunnel catalogs can be added explicitly with `FANCY_GPT_TUNNELS_FILE`.

## Recommended remote development topology

For a local Windows/macOS/Linux workstation running Chrome/Edge/Firefox and a remote VS Code/SSH host containing the code/repo:

```text
LOCAL WORKSTATION                         REMOTE HOST
Browser + FancyGPT extension              FancyGPT bridge + MCP + repo
       │ ws://127.0.0.1:8765                  ▲
       └──────── SSH LocalForward ────────────┘
```

On the remote host:

```bash
fancy-gpt bridge init
fancy-gpt bridge serve
```

On the local workstation, forward local port 8765 to remote 127.0.0.1:8765. Example SSH config:

```sshconfig
Host datalink-build
    HostName <remote-host>
    User <user>
    LocalForward 127.0.0.1:8765 127.0.0.1:8765
```

Export the extension on a machine with FancyGPT installed, or copy the exported directory to the workstation:

```bash
fancy-gpt extension export chrome ./fancy-gpt-extension
# edge / firefox are also supported
```

Load the unpacked extension, configure the pair token printed by `fancy-gpt bridge init`, keep endpoint `ws://127.0.0.1:8765`, and select the browser route `chrome-remote`, `edge-remote`, or `firefox-remote`.

The remote bridge binds loopback by default. This is intentional: use SSH forwarding rather than exposing the bridge on LAN/WAN.

## Local Native Messaging mode

For browser and FancyGPT on the same machine:

```bash
fancy-gpt bridge init
fancy-gpt bridge serve
fancy-gpt extension export chrome ./fancy-gpt-extension
fancy-gpt extension native-config --browser chrome --tunnel chrome-remote
fancy-gpt extension native-manifest --browser chrome --extension-id <extension-id>
```

Chrome/Edge use `allowed_origins`; Firefox uses `allowed_extensions`. Linux/macOS manifest locations and Windows per-user registry registration are implemented by the native-manifest layer.

## Playwright fallback

Optional only:

```bash
./install.sh --with-playwright
fancy-gpt run request.yaml --tunnel chrome-remote
```

This path uses a FancyGPT-owned persistent browser profile. It is not the preferred mode for the local-browser/remote-repo workflow.

## MCP

```bash
fancy-gpt mcp
```

MCP exposes reasoning tools plus tunnel inspection/selection, including:

- `run_request_automatic(..., tunnel_id=..., tunnel_policy=...)`
- `list_tunnels`
- `probe_tunnels`
- `inspect_tunnel`
- `select_tunnel`
- `list_sites`
- `list_tunnel_runtimes`
- `list_tunnel_transports`
- `inspect_tunnel_layers`
- `create_session`, `list_sessions`, `get_session`, `close_session`
- `create_chat`, `list_chats`, `select_chat`, `archive_chat`
- `list_session_requests`, `session_capabilities`

Thus the caller can select the online tunnel **per request at runtime** without changing ReviewEngine.

## Sessions and conversations

Planner turns always use Temporary Chat. Final answers use a durable session
chat, so planning noise never appears in the main discussion and consecutive
requests retain context.

```yaml
mode: review
objective: Review the new routing design
repo_root: .
session_id: ses-0123456789ab   # omit for the repo-scoped default session
chat_policy: continue          # continue | new_chat | independent | temporary
```

Use `independent` for a separate review that must not replace the active main
chat. Use `new_chat` to branch into and select a new durable discussion. The
full lifecycle, storage, recovery behavior, schemas, and clean layer boundaries
are documented in [Session and chat architecture](docs/SESSION_ARCHITECTURE.md).

The same choices are available without editing YAML:

```bash
fancy-gpt run request.yaml --session ses-0123456789ab --chat-policy continue
fancy-gpt run review.yaml --session ses-0123456789ab --chat-policy independent
fancy-gpt sessions list
fancy-gpt chats list ses-0123456789ab
```

## Security boundaries

- Core/MCP never reads browser cookies, local storage, OAuth/access/session tokens, or private ChatGPT endpoints.
- Extension transport and ChatGPT DOM logic are separate layers.
- Bridge binds loopback by default; remote use is intended through SSH forwarding.
- Pair tokens are stored in a user-private file where supported.
- Filesystem context is constrained by allowed roots before online planning.
- Independent-design candidates are removed from planner metadata, ContextPack and final prompt.
- Local/web evidence is treated as untrusted input, not instructions.

See `SECURITY.md` and `docs/TUNNEL_ARCHITECTURE.md`.

## Offline verification

No browser/account/network required:

```bash
fancy-gpt test
```

From source:

```bash
python scripts/build_extension.py --check
python -m compileall -q src tests scripts
PYTHONPATH=src pytest -ra
PYTHONPATH=src python scripts/functional_review.py
PYTHONPATH=src python scripts/tunnel_review.py
PYTHONPATH=src python scripts/release_gate.py
```

Live ChatGPT Web UI compatibility remains a local smoke-test item because the site UI can change independently from FancyGPT.
