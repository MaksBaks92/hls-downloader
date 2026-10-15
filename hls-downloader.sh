#!/usr/bin/env bash
# Launch the packaged app, or fall back to running from source.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PACKAGED="$ROOT/dist/HLS Downloader/HLS Downloader"
if [[ -x "$PACKAGED" ]]; then
  exec "$PACKAGED" "$@"
fi

HERE="$ROOT/HLS Downloader"
if [[ -x "$HERE" ]]; then
  exec "$HERE" "$@"
fi

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  exec "$ROOT/.venv/bin/python" "$ROOT/hls_downloader.py" "$@"
fi

exec python3 "$ROOT/hls_downloader.py" "$@"
