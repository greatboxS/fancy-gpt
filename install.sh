#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WITH_PLAYWRIGHT=0
WITH_BROWSER_DEPS=0
SKIP_VERIFY=0
PRESET=""

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Install FancyGPT once as an isolated user CLI. v0.7 defaults to the lightweight
extension/bridge tunnel path and doesn't download a private Chromium runtime.

Options:
  --with-playwright     Also install Playwright Chromium as a local fallback tunnel.
  --with-browser-deps   Install Playwright Linux system dependencies too (implies --with-playwright).
  --skip-verify         Skip post-install offline self-test.
  --preset remote-edge Install the Edge/SSH deployment bundle and register Codex MCP.
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
      [[ "${1:-}" == "remote-edge" ]] || { echo "Supported preset: remote-edge" >&2; exit 2; }
      PRESET="$1"
      ;;
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

WHEEL="${FANCY_GPT_INSTALL_SOURCE:-}"
if [[ -z "$WHEEL" ]]; then
  WHEEL="$(find "$ROOT/dist" -maxdepth 1 -type f -name 'fancy_gpt-0.7.0-*.whl' | sort | tail -n 1 || true)"
fi
[[ -n "$WHEEL" && -f "$WHEEL" ]] || { echo "Bundled fancy-gpt 0.7.0 wheel not found under $ROOT/dist" >&2; exit 1; }

echo "[fancy-gpt] Installing isolated user CLI from: $WHEEL"
if (( WITH_PLAYWRIGHT )); then
  "$UV" tool install --force --with 'playwright>=1.55,<2' "$WHEEL"
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

TOKEN_INFO="$($FG bridge init)"

if [[ "$PRESET" == "remote-edge" ]]; then
  BUNDLE_DIR="${FANCY_GPT_EDGE_BUNDLE_DIR:-${HOME}/.local/share/fancy-gpt/remote-edge}"
  "$FG" extension export edge "$BUNDLE_DIR/extension" >/dev/null
  python3 - "$TOKEN_INFO" "$BUNDLE_DIR/PAIRING.txt" <<'PY'
import json, os, pathlib, sys
info = json.loads(sys.argv[1])
target = pathlib.Path(sys.argv[2])
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(
    "Endpoint: ws://127.0.0.1:8765\n"
    "Tunnel ID: edge-extension-ws-remote\n"
    "Browser: edge\n"
    f"Pair token: {info['pair_token']}\n",
    encoding="utf-8",
)
os.chmod(target, 0o600)
PY
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
else
  echo "Bridge pairing token written to the private preset bundle."
fi
echo
echo "FancyGPT 0.7.0 installed successfully."
echo "Command: $FG"
echo "Default architecture: browser extension tunnel; Playwright is only a fallback."
echo "Next: $FG extension export chrome ~/.local/share/fancy-gpt/extension-chrome"
echo "      $FG bridge serve"
echo "For remote VS Code/SSH development, forward remote port 8765 to local 127.0.0.1:8765."
echo "Run '$FG tunnels list' to inspect all runtime-selectable tunnel compositions."
if [[ "$PRESET" == "remote-edge" ]]; then
  echo
  echo "Remote Edge preset ready."
  echo "Private bundle: $BUNDLE_DIR"
  echo "Pairing details: $BUNDLE_DIR/PAIRING.txt (mode 0600; do not commit/share)"
  echo "Codex MCP: $MCP_STATUS"
  echo "Start bridge: $FG bridge serve"
  echo "Windows: copy $BUNDLE_DIR/extension, establish SSH LocalForward 8765, then Load unpacked in Edge."
fi
