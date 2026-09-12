#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WITH_PLAYWRIGHT=0
WITH_BROWSER_DEPS=0
SKIP_VERIFY=0
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
  --no-mcp-register     Do not auto-register detected Codex/Claude Code MCP clients.
  -h, --help            Show this help.
EOF
}

while (($#)); do
  case "$1" in
    --with-playwright) WITH_PLAYWRIGHT=1 ;;
    --with-browser-deps) WITH_PLAYWRIGHT=1; WITH_BROWSER_DEPS=1 ;;
    --skip-verify) SKIP_VERIFY=1 ;;
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

WHEEL="${FANCY_GPT_INSTALL_SOURCE:-}"
if [[ -z "$WHEEL" ]]; then
  WHEEL="$(find "$ROOT/dist" -maxdepth 1 -type f -name 'fancy_gpt-0.8.0-*.whl' | sort | tail -n 1 || true)"
fi
[[ -n "$WHEEL" && -f "$WHEEL" ]] || { echo "Bundled fancy-gpt 0.8.0 wheel not found under $ROOT/dist" >&2; exit 1; }

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
echo
echo "$TOKEN_INFO"
echo
echo "FancyGPT 0.8.0 installed successfully."
echo "Command: $FG"
echo "Default architecture: browser extension tunnel; Playwright is only a fallback."
echo "Next: $FG extension export chrome ~/.local/share/fancy-gpt/extension-chrome"
echo "      $FG bridge serve"
echo "For remote VS Code/SSH development, forward remote port 8765 to local 127.0.0.1:8765."
echo "Run '$FG tunnels list' to inspect all runtime-selectable tunnel compositions."
