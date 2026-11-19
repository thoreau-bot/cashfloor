# Changelog

Entries are written for users. Each `## x.y.z` heading pushed to `main`
becomes a tagged GitHub release with a downloadable zip.

## 0.2.0

Projection accuracy: no more double-counted card payments, no more silently ignored cards.

**New**
- **Pays account** on the Scheduled page: link a detected or manual outflow to the card, loan or utility it pays. That account's own due-date event is then dropped from the projection instead of being counted on top of the autopay. The dropdown pre-selects a suggestion from the payee name (marked *suggested*) until you save it. CLI: `cashfloor items pays ITEM [ACCOUNT]`, `cashfloor items suggest-links [--apply]`.
- **Assumed card payoff**: a credit card with no known due date and no linked payment is now assumed to be paid in full once a month, on the account's *pay day* (Accounts page) or the default day in Settings. Shown as *(assumed payoff)* in the day-by-day events. Turn it off in Settings → *Assume card payoff*.
- **Manual accounts**: add an account no provider supplies (insurance, a utility, a card the Bridge can't see) from the bottom of the Accounts page or with `cashfloor accounts add`. You maintain its balance and due figures by hand; sync never touches it.

**Fixed**
- Saving an Accounts row with a four-figure statement balance or minimum (shown as `1,234.56`) failed; amounts typed with commas, `$` or parentheses are now accepted everywhere.

**Upgrading**
- Unzip over your install and restart `scripts\serve.bat` / `scripts/serve.sh`; the database upgrades itself on first start.
- Open the Scheduled page once: anything marked *suggested* in the new **Pays** column is worth a look.

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
