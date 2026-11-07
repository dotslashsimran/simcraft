#!/bin/sh
# One-time setup: a Python env with Whisper next to this skill. Run: sh setup.sh
set -e
cd "$(dirname "$0")"
if [ "$(uname -s)-$(uname -m)" = "Darwin-arm64" ]; then PKGS="mlx-whisper numpy"; else PKGS="faster-whisper numpy"; fi
if command -v uv >/dev/null; then
  uv venv -q --python 3.12 .venv && uv pip install -q --python .venv/bin/python $PKGS
else
  python3 -m venv .venv && .venv/bin/pip install -q $PKGS
fi
command -v ffmpeg >/dev/null || [ -n "$FFMPEG" ] || \
  echo "no ffmpeg found: brew install ffmpeg, or npm i ffmpeg-static and export FFMPEG=<its path>"
echo "ready: $(pwd)/.venv/bin/python"
