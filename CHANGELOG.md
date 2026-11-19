# Changelog

Entries are written for users. Each `## x.y.z` heading pushed to `main`
becomes a tagged GitHub release with a downloadable zip.

## 0.2.0

Projection accuracy: no more double-counted card payments, no more silently ignored cards.

**New**
- **Pays account** on the Scheduled page: link a detected or manual outflow to the card, loan or utility it pays. That account's own due-date event is then dropped from the projection instead of being counted on top of the autopay. The dropdown pre-selects a suggestion from the payee name (marked *suggested*) until you save it. CLI: `cashfloor items pays ITEM [ACCOUNT]`, `cashfloor items suggest-links [--apply]`.
- **Assumed card payoff**: a credit card with no known due date and no linked payment is now assumed to be paid in full once a month, on the account's *pay day* (Accounts page) or the default day in Settings. Shown as *(assumed payoff)* in the day-by-day events. Turn it off in Settings → *Assume card payoff*.
- **Split paychecks are one paycheck.** When the same income shows up under two payee descriptors (a one-cent change in net pay, a reworded deposit line), each used to be its own series with gaps where the other's hits were. Inflow series on one account whose amounts are within 10% (Settings → *Income merge tolerance*), whose dates never collide, and which together form a steadier cadence than either alone are now merged; the series with the most hits before merging supplies the projected amount. `cashfloor items detect` shows the folded series as *merged into …*. On sync, the retired half's scheduled item is removed and the surviving item inherits its confirmation, pays-link and weekend rule — the same clean-up also catches an item proposed by an earlier version under a payee key the detector no longer produces (same cadence, amount and next date → one item).
- **"Every 2 weeks" vs "twice a month."** Both get called biweekly; the Scheduled page and `items list` now say which is meant (`biweekly` → *every 2 weeks*, `semimonthly` → *twice a month*). Config, CLI arguments and the API keep the old values.
- **Investments tab** (also replaces the old Holdings page): retirement & brokerage accounts in one section, healthcare spending accounts in another, holdings underneath. New account types **hsa** and **fsa** — SimpleFIN accounts are typed from their names, and existing accounts named HSA/FSA that were filed as *investment* are re-typed once on upgrade. Every section has a total line; the healthcare one also shows how much FSA money is at risk. An FSA's year-end forfeiture is shown on the tab, and the dashboard and `cashfloor floor` warn from two months before Dec 31 (Settings → *FSA warn days*) while it still holds money.
- **Manual accounts**: add an account no provider supplies (insurance, a utility, a card the Bridge can't see) from the bottom of the Accounts page or with `cashfloor accounts add`. You maintain its balance and due figures by hand; sync never touches it.

**Fixed**
- A scheduled item whose occurrence had already posted (this morning's payroll, a bill drafted a day early) was added to the projection on top of a balance that already included it. An occurrence due within 3 days is now skipped when a matching posting (same account, sign, amount within tolerance, payee when known) landed in the last 4 days, and sync rolls the item's next date past it.
- Saving an Accounts row with a four-figure statement balance or minimum (shown as `1,234.56`) failed; amounts typed with commas, `$` or parentheses are now accepted everywhere.

**Upgrading**
- New: close the dashboard, drop the zip into your install folder, run `scripts\update.bat` (or `scripts/update.sh`), start `serve` again. The installed version now shows top-right in the dashboard. The database upgrades itself on first start.
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
