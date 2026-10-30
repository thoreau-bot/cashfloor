#!/usr/bin/env bash
# One-time setup on Linux/macOS: virtualenv, install, Playwright browser, tests, config.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v python3 >/dev/null || { echo "python3 (3.11+) not found"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev,scrape]"
playwright install chromium || echo "playwright browser install failed (headless server? try: playwright install --with-deps chromium)"
pytest
[ -f config.toml ] || cashfloor init
echo
echo "Setup complete. Next: edit config.toml, then: cashfloor simplefin setup <TOKEN>"
echo "Use scripts/shell.sh to open a shell with the environment active."
