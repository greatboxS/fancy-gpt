#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WITH_PLAYWRIGHT=0
WITH_BROWSER_DEPS=0
SKIP_VERIFY=0
PRESET=""
PRESET_BROWSER="edge"
REGISTER_MCP=1

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Install FancyGPT once as an isolated user CLI. v0.8 defaults to the lightweight
extension/bridge tunnel path and doesn't download a private Chromium runtime.

Options:
  --with-playwright     Also install Playwright Chromium as a local fallback tunnel.
  --with-browser-deps   Install Playwright Linux system dependencies too (implies --with-playwright).
  --skip-verify         Skip post-install offline self-test.
  --preset remote-extension
                        Install a portable browser/SSH bundle and register Codex MCP.
  --preset local-extension
                        Install a same-machine Native Messaging bundle and config.
  --preset remote-edge Backward-compatible alias for --preset remote-extension --browser edge.
  --browser NAME        Browser for remote-extension: chrome, edge, or firefox (default: edge).
  --no-mcp-register     Do not auto-register detected Codex/Claude Code MCP clients.
  -h, --help            Show this help.
EOF
}

while (($#)); do
  case "$1" in
    --with-playwright) WITH_PLAYWRIGHT=1 ;;
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

# The installer installs whatever wheel the build produced; it deliberately
# knows no version of its own, so a bump needs no change here.
WHEEL="${FANCY_GPT_INSTALL_SOURCE:-}"
if [[ -z "$WHEEL" ]]; then
  WHEEL="$(find "$ROOT/dist" -maxdepth 1 -type f -name 'fancy_gpt-*.whl' -printf '%T@ %p\n' \
    | sort -n | tail -n 1 | cut -d' ' -f2- || true)"
fi
if [[ -z "$WHEEL" || ! -f "$WHEEL" ]]; then
  echo "No fancy-gpt wheel found under $ROOT/dist" >&2
  echo "Build it first:  uv build --wheel" >&2
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
  echo "[fancy-gpt] Installing optional Playwright Chromium fallback..."
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
    echo "[fancy-gpt] Auto-registering detected MCP clients (Codex / Claude Code)..."
    if ! "$FG" clients register-detected --server "$MCP_BIN"; then
      echo "[fancy-gpt] WARNING: one or more detected MCP clients could not be registered; core installation is still valid." >&2
      echo "[fancy-gpt] Run '$FG clients list' and '$FG clients register <client>' for diagnostics." >&2
    fi
  fi
fi

TOKEN_INFO="$($FG bridge init)"

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
    f"Tunnel ID: {browser}-extension-ws-remote\n"
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
echo "Default architecture: browser extension tunnel; Playwright is only a fallback."
echo "Next: $FG extension export chrome ~/.local/share/fancy-gpt/extension-chrome"
echo "      $FG bridge serve"
echo "For remote VS Code/SSH development, forward remote port 8765 to local 127.0.0.1:8765."
echo "Run '$FG tunnels list' to inspect all runtime-selectable tunnel compositions."
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
