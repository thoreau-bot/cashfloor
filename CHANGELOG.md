# Changelog

Entries are written for users. Each `## x.y.z` heading pushed to `main`
becomes a tagged GitHub release with a downloadable zip.

## 0.1.0

First public release.

**What it does**
- Pulls balances and transactions from SimpleFIN Bridge (protocol v2, every field kept, raw responses viewable in Settings).
- Downloads every document USAA holds (statements, insurance, notices) through a plugin that attaches to your own browser, names them by title and files them per account.
- Reads the transaction tables inside bank-statement PDFs so the recurring-bill detector sees years of history, not just the last 60 days.
- Detects paychecks and bills (fixed, variable, and "paid in several pieces" card payments), proposes them, and projects your daily balance 90 days out.
- Shows the **Cash floor** (lowest projected balance) and **Discretionary now** (floor minus your safety buffer) on a local dashboard.

**Setup**
- `scripts\setup.bat` (Windows) or `scripts/setup.sh`, then `scripts\serve.bat` / `scripts/serve.sh`.
- Connect SimpleFIN on the Settings page; label accounts on the Accounts page; confirm detected bills on the Scheduled page.

**Privacy**
- Local-only; secrets in the OS keyring; no telemetry. See `docs/PRIVACY.md`.
