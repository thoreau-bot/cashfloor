#!/usr/bin/env bash
# Update an existing install from a release zip (see update.bat for the Windows twin).
#   scripts/update.sh [path/to/cashfloor-<version>.zip]   (default: newest cashfloor-*.zip here)
set -euo pipefail
cd "$(dirname "$0")/.."
ZIP="${1:-$(ls -t cashfloor-*.zip 2>/dev/null | head -1 || true)}"
[ -n "$ZIP" ] && [ -f "$ZIP" ] || { echo "No cashfloor-*.zip found; drop the release zip here or pass its path."; exit 1; }
TMPD="$(mktemp -d)"
unzip -q "$ZIP" -d "$TMPD"
SRC="$TMPD"; [ -f "$TMPD/cashfloor/pyproject.toml" ] && SRC="$TMPD/cashfloor"
[ -f "$SRC/pyproject.toml" ] || { echo "That zip does not look like a cashfloor release."; rm -rf "$TMPD"; exit 1; }
# copy everything except the user's own files (plain tar: no rsync/cpio dependency)
tar -C "$SRC" --exclude=./config.toml --exclude=./.venv --exclude=./data --exclude=./statements \
    --exclude=./inbox --exclude=./browser-profiles --exclude=./.git -cf - . | tar -xf -
rm -rf "$TMPD"
if [ -x .venv/bin/pip ]; then .venv/bin/pip install -q -e ".[dev,scrape]"; else echo "No .venv yet - run scripts/setup.sh next."; fi
echo "Installed $(grep -m1 '^version' pyproject.toml | cut -d'"' -f2). Start scripts/serve.sh; the version shows top-right."
