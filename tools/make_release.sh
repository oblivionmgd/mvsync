#!/bin/sh
# Packs dist/mvsync-<version>.zip from the repository (run tools/build_tox.py in TD first).
# Usage: tools/make_release.sh 1.0.0
set -e
VER="${1:?version, e.g. 1.0.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$ROOT/dist/mvsync.tox" ] || { echo "dist/mvsync.tox がありません（TD で tools/build_tox.py を実行）"; exit 1; }
STAGE="$ROOT/dist/stage/mvsync-$VER"
rm -rf "$ROOT/dist/stage"; mkdir -p "$STAGE"
cp "$ROOT/dist/mvsync.tox" "$STAGE/"
mkdir -p "$STAGE/worker" "$STAGE/td"
cp "$ROOT"/worker/*.py "$ROOT/worker/requirements.txt" "$STAGE/worker/"
cp "$ROOT"/td/*.py "$STAGE/td/"
cp "$ROOT/README.md" "$ROOT/LICENSE" "$STAGE/" 2>/dev/null || true
cp "$ROOT/setup.sh" "$ROOT/setup.bat" "$STAGE/"
(cd "$ROOT/dist/stage" && rm -f "../mvsync-$VER.zip" && zip -qr -X "../mvsync-$VER.zip" "mvsync-$VER")
rm -rf "$ROOT/dist/stage"
echo "$ROOT/dist/mvsync-$VER.zip"
