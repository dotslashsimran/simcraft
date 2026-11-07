#!/bin/sh
# One-time setup: a Python env next to this skill with numpy, matplotlib and pypdf.
# Run: sh setup.sh          (numpy only builds, the default)
#      sh setup.sh torch    (also installs CPU PyTorch, only if the paper needs autograd at scale)
set -e
cd "$(dirname "$0")"
PKGS="numpy matplotlib pypdf"
[ "$1" = "torch" ] && PKGS="$PKGS torch"
if command -v uv >/dev/null; then
  [ -d .venv ] || uv venv -q --python 3.12 .venv
  uv pip install -q --python .venv/bin/python $PKGS
else
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q $PKGS
fi
command -v ffmpeg >/dev/null || [ -n "$FFMPEG" ] || \
  echo "no ffmpeg found: brew install ffmpeg, or npm i ffmpeg-static and export FFMPEG=<its path>"
echo "ready: $(pwd)/.venv/bin/python"
