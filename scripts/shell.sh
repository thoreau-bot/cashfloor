#!/usr/bin/env bash
# Open an interactive shell in the repo with the virtualenv activated.
cd "$(dirname "$0")/.." || exit 1
[ -f .venv/bin/activate ] || { echo "No virtualenv found. Run scripts/setup.sh first."; exit 1; }
exec "${SHELL:-bash}" --rcfile <(cat ~/.bashrc 2>/dev/null; echo 'source .venv/bin/activate') -i
