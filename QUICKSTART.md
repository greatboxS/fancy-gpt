# fancy-gpt 0.8.0 Quick Start

## 1. Install

```bash
./install.sh
```

The installer keeps Playwright optional and attempts to register detected Codex/Claude Code MCP clients.

```bash
fancy-gpt test
fancy-gpt clients list
fancy-gpt tunnels list
```

## 2. Bring up Edge on Windows → FancyGPT in Ubuntu/VM

Ubuntu/remote host:

```bash
fancy-gpt bridge init
fancy-gpt bridge serve
```

Windows/local host:

```text
ssh -L 8765:127.0.0.1:8765 <remote-host>
```

Load the exported Edge extension and configure:

```text
Endpoint: ws://127.0.0.1:8765
Pair token: <token from bridge init>
Tunnel: edge-extension-ws-remote
```

Verify from Ubuntu:

```bash
fancy-gpt tunnels health
fancy-gpt tunnels inspect edge-extension-ws-remote
```

## 3. Ask a narrow technical question

```bash
fancy-gpt ask "Does this ADC input require software control?" --intent focused
```

FancyGPT preserves the semantic scope and expands only for material correctness/risk/decision reasons.

## 4. Run the classic independent review path

```bash
fancy-gpt init
# edit fancy-gpt-request.yaml
fancy-gpt run fancy-gpt-request.yaml --tunnel edge-extension-ws-remote
```

If a run fails:

```bash
fancy-gpt execution recent
fancy-gpt execution status <execution-id>
```

## 5. Create a persistent engineering project

```bash
fancy-gpt project init demo \
  --target "Ship a verified feature" \
  --acceptance "Runtime tests pass"

fancy-gpt project bootstrap demo
fancy-gpt project status demo
fancy-gpt project continue demo
```

Execute the next model-backed step:

```bash
fancy-gpt project run-next demo --tunnel edge-extension-ws-remote
```

When an implementer step requires local repository mutation, FancyGPT returns an external-agent assignment instead of pretending the browser model edited files. Codex/Claude can execute that assignment through MCP and return a structured outcome.

## 6. Inspect persistent state

```bash
fancy-gpt project sessions demo
fancy-gpt project history demo
fancy-gpt project context demo
```

The context command shows reduced relevant state, not raw conversation history.

## 7. MCP

```bash
fancy-gpt mcp
```

Manual registration if needed:

```bash
fancy-gpt clients register codex
fancy-gpt clients register claude-code
```
