#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WITH_PLAYWRIGHT=1
WITH_BROWSER_DEPS=0
SKIP_VERIFY=0
PRESET=""
PRESET_BROWSER="edge"
REGISTER_MCP=1
CONFIGURE_GATEWAY=1
START_SERVICES=1

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Install and configure FancyGPT as a ready-to-use isolated user CLI.

Options:
  --with-playwright     Install Playwright Chromium (default; retained for compatibility).
  --without-playwright  Skip the local Playwright Chromium fallback.
  --with-browser-deps   Install Playwright Linux system dependencies too (implies --with-playwright).
  --skip-verify         Skip post-install offline self-test.
  --preset remote-extension
                        Install a portable browser/SSH bundle and register Codex MCP.
  --preset local-extension
                        Install a same-machine Native Messaging bundle and config.
  --preset remote-edge Backward-compatible alias for --preset remote-extension --browser edge.
  --browser NAME        Browser for remote-extension: chrome, edge, or firefox (default: edge).
  --no-mcp-register     Do not auto-register detected Codex/Claude Code MCP clients.
  --no-gateway-config   Do not configure global Codex/Claude/Gemini model gateway access.
  --no-services         Do not install/start user-level bridge and gateway services.
  -h, --help            Show this help.
EOF
}

while (($#)); do
  case "$1" in
    --with-playwright) WITH_PLAYWRIGHT=1 ;;
    --without-playwright) WITH_PLAYWRIGHT=0 ;;
    --with-browser-deps) WITH_PLAYWRIGHT=1; WITH_BROWSER_DEPS=1 ;;
    --skip-verify) SKIP_VERIFY=1 ;;
    --preset)
      shift
      case "${1:-}" in
        remote-extension|local-extension) PRESET="$1" ;;
        remote-edge) PRESET="remote-extension"; PRESET_BROWSER="edge" ;;
        *) echo "Supported presets: remote-extension, remote-edge" >&2; exit 2 ;;
      esac
      ;;
    --browser)
      shift
      PRESET_BROWSER="${1:-}"
      [[ "$PRESET_BROWSER" =~ ^(chrome|edge|firefox)$ ]] || { echo "--browser must be chrome, edge, or firefox" >&2; exit 2; }
      ;;
    --no-mcp-register) REGISTER_MCP=0 ;;
    --no-gateway-config) CONFIGURE_GATEWAY=0 ;;
    --no-services) START_SERVICES=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ -n "${FANCY_GPT_UV_BIN:-}" ]]; then
  UV="$FANCY_GPT_UV_BIN"
elif command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
else
  echo "[fancy-gpt] uv not found; bootstrapping uv..."
  command -v curl >/dev/null 2>&1 || { echo "curl is required to bootstrap uv" >&2; exit 1; }
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="${HOME}/.local/bin/uv"
  [[ -x "$UV" ]] || UV="$(command -v uv || true)"
  [[ -n "$UV" && -x "$UV" ]] || { echo "uv installation did not produce an executable" >&2; exit 1; }
fi

# Release bundles may provide a wheel. A source checkout is also directly
# installable, so contributors and first-time users do not need a separate build.
WHEEL="${FANCY_GPT_INSTALL_SOURCE:-}"
if [[ -z "$WHEEL" ]]; then
  WHEEL="$(find "$ROOT/dist" -maxdepth 1 -type f -name 'fancy_gpt-*.whl' -printf '%T@ %p\n' \
    | sort -n | tail -n 1 | cut -d' ' -f2- || true)"
fi
if [[ -z "$WHEEL" ]]; then
  WHEEL="$ROOT"
elif [[ ! -e "$WHEEL" ]]; then
  echo "FancyGPT install source does not exist: $WHEEL" >&2
  exit 1
fi

echo "[fancy-gpt] Installing isolated user CLI from: $WHEEL"
if (( WITH_PLAYWRIGHT )); then
  "$UV" tool install --force --with "playwright>=1.55,<2" "$WHEEL"
else
  "$UV" tool install --force "$WHEEL"
fi
BIN_DIR="$("$UV" tool dir --bin 2>/dev/null || true)"
[[ -n "$BIN_DIR" ]] || BIN_DIR="${HOME}/.local/bin"
FG="$BIN_DIR/fancy-gpt"
[[ -x "$FG" ]] || FG="$(command -v fancy-gpt || true)"
[[ -n "$FG" && -x "$FG" ]] || { echo "fancy-gpt executable not found after install" >&2; exit 1; }
"$UV" tool update-shell >/dev/null 2>&1 || true

if (( WITH_PLAYWRIGHT )); then
  echo "[fancy-gpt] Installing Playwright Chromium fallback..."
  if (( WITH_BROWSER_DEPS )); then "$FG" browser-setup --with-deps; else "$FG" browser-setup; fi
fi

if (( ! SKIP_VERIFY )); then
  echo "[fancy-gpt] Running standalone offline self-test..."
  "$FG" test
  "$FG" verify
fi

if (( REGISTER_MCP )); then
  MCP_BIN="$BIN_DIR/fancy-gpt-mcp"
  if [[ -x "$MCP_BIN" ]]; then
    echo "[fancy-gpt] Auto-registering detected MCP clients (Codex / Claude Code / Gemini CLI)..."
    if ! "$FG" clients register-detected --server "$MCP_BIN"; then
      echo "[fancy-gpt] WARNING: one or more detected MCP clients could not be registered; core installation is still valid." >&2
      echo "[fancy-gpt] Run '$FG clients list' and '$FG clients register <client>' for diagnostics." >&2
    fi
  fi
fi

if (( CONFIGURE_GATEWAY )); then
  echo "[fancy-gpt] Configuring global model gateway access (Codex / Claude Code / Gemini CLI)..."
  "$FG" gateway configure-clients
  if command -v claude >/dev/null 2>&1; then
    CLAUDE_BIN="$(command -v claude)"
    CLAUDE_SETTINGS="${HOME}/.claude/fancy-gpt-settings.json"
    python3 - "$BIN_DIR/fancy-claude" "$CLAUDE_BIN" "$CLAUDE_SETTINGS" <<'PY'
from pathlib import Path
import shlex, sys
target, claude, settings = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
target.write_text(
    "#!/usr/bin/env bash\nexec " + shlex.quote(claude) + " --settings " + shlex.quote(settings) + ' "$@"\n',
    encoding="utf-8",
)
target.chmod(0o755)
PY
  fi
  if command -v gemini >/dev/null 2>&1; then
    GEMINI_BIN="$(command -v gemini)"
    GEMINI_ENV="${HOME}/.gemini/fancy-gpt.env"
    python3 - "$BIN_DIR/fancy-gemini-cli" "$GEMINI_BIN" "$GEMINI_ENV" <<'PY'
from pathlib import Path
import shlex, sys
target, gemini, env_file = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
lines = []
for raw in env_file.read_text(encoding="utf-8").splitlines():
    if raw and not raw.startswith("#") and "=" in raw:
        key, value = raw.split("=", 1)
        lines.append(f"export {key}={shlex.quote(value)}")
target.write_text(
    "#!/usr/bin/env bash\n" + "\n".join(lines) + "\nexec " + shlex.quote(gemini) + ' "$@"\n',
    encoding="utf-8",
)
target.chmod(0o755)
PY
  fi
fi

TOKEN_INFO="$($FG bridge init)"

SERVICE_STATUS="not requested"
if (( START_SERVICES )); then
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    SERVICE_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
    GATEWAY_STATE_DIR="${FANCY_GPT_DATA_HOME:-${XDG_DATA_HOME:-${HOME}/.local/share}/fancy-gpt}/gateway"
    mkdir -p "$SERVICE_DIR"
    mkdir -p "$GATEWAY_STATE_DIR"
    python3 - "$SERVICE_DIR" "$FG" "$GATEWAY_STATE_DIR" <<'PY'
from pathlib import Path
import sys

directory, executable, state_dir = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
escaped = executable.replace("\\", "\\\\").replace('"', '\\"')
escaped_state = state_dir.replace("\\", "\\\\").replace('"', '\\"')
units = {
    "fancy-gpt-bridge.service": f'''[Unit]
Description=FancyGPT browser bridge

[Service]
ExecStart="{escaped}" bridge serve
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
''',
    "fancy-gpt-gateway.service": f'''[Unit]
Description=FancyGPT multi-site model gateway
After=fancy-gpt-bridge.service
Wants=fancy-gpt-bridge.service

[Service]
ExecStart="{escaped}" gateway serve --host 127.0.0.1 --port 8787 --workdir "{escaped_state}"
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
''',
}
directory.mkdir(parents=True, exist_ok=True)
for name, content in units.items():
    (directory / name).write_text(content, encoding="utf-8")
PY
    systemctl --user daemon-reload
    if systemctl --user enable --now fancy-gpt-bridge.service fancy-gpt-gateway.service >/dev/null; then
      SERVICE_STATUS="running (systemd user services)"
    else
      SERVICE_STATUS="configured but failed to start; inspect: journalctl --user -u fancy-gpt-gateway"
    fi
  else
    SERVICE_STATUS="not started (systemd user session unavailable)"
  fi
fi

if [[ "$PRESET" == "remote-extension" ]]; then
  BUNDLE_DIR="${FANCY_GPT_EXTENSION_BUNDLE_DIR:-${FANCY_GPT_EDGE_BUNDLE_DIR:-${HOME}/.local/share/fancy-gpt/remote-${PRESET_BROWSER}}}"
  "$FG" extension export "$PRESET_BROWSER" "$BUNDLE_DIR/extension" >/dev/null
  python3 - "$TOKEN_INFO" "$BUNDLE_DIR/PAIRING.txt" "$PRESET_BROWSER" <<'PY'
import json, os, pathlib, sys
info = json.loads(sys.argv[1])
target = pathlib.Path(sys.argv[2])
browser = sys.argv[3]
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(
    "Endpoint: ws://127.0.0.1:8765\n"
    f"Tunnel ID: {browser}-remote\n"
    f"Browser: {browser}\n"
    f"Pair token: {info['pair_token']}\n",
    encoding="utf-8",
)
os.chmod(target, 0o600)
PY
fi

if [[ "$PRESET" == "local-extension" ]]; then
  BUNDLE_DIR="${FANCY_GPT_EXTENSION_BUNDLE_DIR:-${HOME}/.local/share/fancy-gpt/local-${PRESET_BROWSER}}"
  "$FG" extension export "$PRESET_BROWSER" "$BUNDLE_DIR/extension" >/dev/null
  "$FG" extension native-config --browser "$PRESET_BROWSER" >/dev/null
fi

if [[ -n "$PRESET" ]]; then
  if command -v codex >/dev/null 2>&1; then
    if codex mcp get fancy-gpt >/dev/null 2>&1; then
      MCP_STATUS="already registered"
    else
      codex mcp add fancy-gpt -- "$BIN_DIR/fancy-gpt-mcp" >/dev/null
      MCP_STATUS="registered (restart Codex to load it)"
    fi
  else
    MCP_STATUS="Codex CLI not found; run: codex mcp add fancy-gpt -- $BIN_DIR/fancy-gpt-mcp"
  fi
fi
echo
if [[ -z "$PRESET" ]]; then
  echo "$TOKEN_INFO"
elif [[ "$PRESET" == "remote-extension" ]]; then
  echo "Bridge pairing token written to the private preset bundle."
else
  echo "Bridge pairing token stored in the private native-host configuration."
fi
echo
echo "FancyGPT $("$FG" version) installed successfully."
echo "Command: $FG"
echo "Runtime fallback: $(if (( WITH_PLAYWRIGHT )); then echo 'Playwright Chromium installed'; else echo 'not installed'; fi)"
echo "Services: $SERVICE_STATUS"
echo "Bridge endpoint: ws://127.0.0.1:8765"
echo "Gateway endpoint: http://127.0.0.1:8787"
echo "Models: fancy-chatgpt, fancy-gemini, fancy-claude"
if (( CONFIGURE_GATEWAY )); then
  echo "Gateway profiles: codex --profile fancy-chatgpt|fancy-gemini|fancy-claude"
  echo "Claude native:    claude (unchanged auth and default models)"
  echo "Claude FancyGPT:  fancy-claude, then run /model"
  echo "Gemini native:    gemini (unchanged auth and default model)"
  echo "Gemini FancyGPT:  fancy-gemini-cli --model fancy-gemini"
  echo "Codex:            start a fancy-* profile, then use the model selector"
fi
if (( WITH_PLAYWRIGHT )); then
  echo "Browser auth:     REQUIRED once: $FG browser login"
else
  echo "Browser tunnel:   REQUIRED: connect a paired extension before using fancy-* models"
fi
echo "Diagnostics:      $FG verify && $FG clients list && $FG tunnels list"
if [[ "$PRESET" == "remote-extension" ]]; then
  echo
  echo "Remote $PRESET_BROWSER extension preset ready (Windows/Linux browser workstation)."
  echo "Private bundle: $BUNDLE_DIR"
  echo "Pairing details: $BUNDLE_DIR/PAIRING.txt (mode 0600; do not commit/share)"
  echo "Codex MCP: $MCP_STATUS"
  echo "Start bridge: $FG bridge serve"
  echo "Windows: copy $BUNDLE_DIR/extension, establish SSH LocalForward 8765, then Load unpacked in Edge."
fi
if [[ "$PRESET" == "local-extension" ]]; then
  echo
  echo "Local $PRESET_BROWSER extension preset ready."
  echo "Load unpacked: $BUNDLE_DIR/extension"
  echo "Codex MCP: $MCP_STATUS"
  echo "After loading, finalize the native host with:"
  echo "  $FG extension native-manifest --browser $PRESET_BROWSER --extension-id <ID_FROM_BROWSER>"
  echo "Then start the loopback-only bridge: $FG bridge serve"
fi
