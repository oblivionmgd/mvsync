#!/bin/sh
# Creates the worker's Python environment (.venv next to this script) for MVSync.
# Needs: uv (https://docs.astral.sh/uv/) or python3.12, plus ffmpeg and node (Homebrew: brew install ffmpeg node).
set -e
cd "$(dirname "$0")"
if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -r worker/requirements.txt
else
  PY="$(command -v python3.12 || true)"
  [ -n "$PY" ] || { echo "Python 3.12 が必要です（uv か python3.12 をインストールしてください）"; exit 1; }
  "$PY" -m venv .venv
  .venv/bin/python -m pip install -U pip
  .venv/bin/python -m pip install -r worker/requirements.txt
fi
for t in ffmpeg node; do
  command -v $t >/dev/null 2>&1 || [ -x /opt/homebrew/bin/$t ] || [ -x /usr/local/bin/$t ] || echo "注意: $t が見つかりません（brew install $t）"
done
echo "OK: $(pwd)/.venv"
