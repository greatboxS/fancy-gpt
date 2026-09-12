#!/usr/bin/env bash
set -Eeuo pipefail
PURGE=0
[[ "${1:-}" == "--purge-data" ]] && PURGE=1
if command -v uv >/dev/null 2>&1; then
  uv tool uninstall fancy-gpt || true
else
  echo "uv not found; remove the fancy-gpt tool environment manually if installed by uv." >&2
fi
if (( PURGE )); then
  DATA="${FANCY_GPT_DATA_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/fancy-gpt}"
  rm -rf -- "$DATA"
  echo "Removed persistent data: $DATA"
else
  echo "Persistent browser profile retained. Use --purge-data to remove it."
fi
