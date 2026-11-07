#!/bin/sh
# Optional one-time setup: a Python env next to this skill with OpenCV (better motion tracking on
# fast moves). Add --whisper to also install a speech-to-text model for transcripts.
# The scripts also run on plain python3 + numpy without this; they just use the numpy tracker.
# Run: sh setup.sh [--whisper]
set -e
cd "$(dirname "$0")"
PKGS="numpy opencv-python-headless"
if [ "$1" = "--whisper" ]; then
  if [ "$(uname -s)-$(uname -m)" = "Darwin-arm64" ]; then PKGS="$PKGS mlx-whisper"; else PKGS="$PKGS faster-whisper"; fi
fi
if command -v uv >/dev/null; then
  uv venv -q --python 3.12 .venv && uv pip install -q --python .venv/bin/python $PKGS
else
  python3 -m venv .venv && .venv/bin/pip install -q $PKGS
fi
command -v ffmpeg >/dev/null || [ -n "$FFMPEG" ] || \
  echo "no ffmpeg found: brew install ffmpeg, or set FFMPEG=/path/to/ffmpeg"
command -v tesseract >/dev/null || echo "optional: install tesseract (brew install tesseract) to time on-screen text"
echo "ready: $(pwd)/.venv/bin/python"
