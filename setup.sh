#!/bin/sh
# MVSync setup (macOS): creates .venv next to this script with everything the worker needs
# (Python 3.12, numpy/scipy, ShazamIO, yt-dlp, ffmpeg, Deno). Nothing else to install.
# If uv is not installed, it is downloaded into .tools/ (nothing is written outside this folder
# except uv's download cache). The TouchDesigner "Install" button runs this same script.
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"
export UV_PYTHON_INSTALL_DIR="$ROOT/.tools/python"
UV="$(command -v uv || true)"
if [ -z "$UV" ] && [ -x "$ROOT/.tools/uv/uv" ]; then UV="$ROOT/.tools/uv/uv"; fi
if [ -z "$UV" ]; then
  echo "[1/3] downloading uv"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$ROOT/.tools/uv" UV_NO_MODIFY_PATH=1 sh
  UV="$ROOT/.tools/uv/uv"
fi
echo "[2/3] Python 3.12 environment (.venv)"
"$UV" venv --allow-existing --python 3.12 .venv
echo "[3/3] packages"
"$UV" pip install --python .venv/bin/python -U -r worker/requirements.txt
.venv/bin/python -c "import numpy, scipy, sounddevice, pythonosc, shazamio, yt_dlp, imageio_ffmpeg, deno; print('check ok')"
echo "OK: $ROOT/.venv"
