#!/usr/bin/env bash
# Start the cashfloor dashboard and open it in your browser.  Ctrl-C to stop.
cd "$(dirname "$0")/.." || exit 1
[ -x .venv/bin/cashfloor ] || { echo "Run scripts/setup.sh first."; exit 1; }
exec .venv/bin/cashfloor serve --open "$@"
