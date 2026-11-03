# Privacy and data handling

cashfloor is local-first. Everything it stores lives on the machine that
runs it, and the only outbound connections are the ones you configure.

## What leaves your machine

| Connection | When | What is sent | What comes back |
|---|---|---|---|
| **SimpleFIN Bridge** (`bridge.simplefin.org`) | each sync, if configured | your access URL (HTTP Basic credentials embedded in it), a start date | balances, transactions, holdings, connection errors |
| **Institution portals** via Playwright plugins | each sync, if configured | whatever that bank's website needs: your session cookies, the page's own API headers | statement PDFs, document lists |
| **Package registries** | install time only | nothing personal | Python packages, the Playwright browser |

There is no telemetry, no crash reporting, no update check, and no
cashfloor-operated server of any kind. SimpleFIN is a third party you
subscribe to directly; its data practices are its own.

## What is stored locally, and where

| Data | Location | Notes |
|---|---|---|
| SimpleFIN access URL, scraper logins | **OS keyring** (Windows Credential Manager, macOS Keychain, Secret Service) | never in config, DB, or logs |
| Accounts, transactions, scheduled items, statement index, settings, payee aliases | `data/cashfloor.sqlite` | unencrypted SQLite; protect with disk encryption if the machine is shared |
| Statement PDFs and their `.meta.json` sidecars | `statements/` | organised archive |
| Downloaded-but-not-yet-filed PDFs | `data/downloads/<provider>/` | plus a manifest of what was fetched |
| Raw provider responses | `data/raw/` | last 30 JSON files per provider, for inspection; disable in Settings |
| Browser profiles for scrapers | `data/browser-profiles/` | contain logged-in sessions — treat like passwords |
| Config | `config.toml` | no secrets by design |

All of the above is git-ignored. `git status` in a clean checkout never
shows them.

## Network exposure

The web dashboard binds to `127.0.0.1` and has **no authentication**. Do
not bind it to a LAN or public address without putting an authenticating
reverse proxy in front (see `docs/DEPLOY_VM.md`).

## Attach-to-browser mode

When a plugin attaches to your own browser over the DevTools port
(`cdp_url`), any local process can drive that browser while the port is
open. Use a dedicated profile and a separate shortcut, and close it when
the sync is done.

## Contributing without leaking

* Tests use synthetic fixtures; never commit real statements, exports,
  account numbers, document ids, or your own paths.
* Commit under a pseudonymous identity if you care about that; the
  project's own commits use a GitHub noreply address.
* Redact person-to-person counterparties (`cashfloor items detect --redact`)
  before pasting detector output into an issue.
