# veolia-water

A **Home Assistant add-on** that pulls daily water-usage from the **Veolia
(ex-SUEZ) New Jersey** customer portal (`mywater.veolia.us`) and imports it into
Home Assistant as long-term statistics — so it shows up in the Energy dashboard
**Water** section.

There is no official API and no HA integration for US Veolia/SUEZ (the existing
`suez_water` / Veolia integrations are France-only). The portal also sits behind
a **Cloudflare JS challenge**, which a pure-Python integration can't clear — so
this runs a real headless browser (Playwright/Chromium) inside an add-on
container. Add-on updates never restart HA Core.

## Why an add-on (not a custom integration)

- The portal's Cloudflare challenge requires executing JS → needs a real browser.
  HA Core integrations can only make plain HTTP requests, so they can't get in.
- An add-on is a full container → it can run Chromium.
- Updating the add-on restarts only its own container, never HA Core.

## How it works

1. **Session reuse** — a saved browser session (`/data/state.json`, persistent)
   is reused every run, so normal runs need **no login and no OTP**.
2. **Re-auth when expired** — logs in with email+password, requests the **email**
   one-time code, reads it from Gmail over IMAP, submits it, re-saves the session.
3. **Data** — fetches `/cp-vna-suez-water-usage/data/<start>/<end>/DAILY/csv`
   inside the authenticated browser context.
4. **Import** — pushes daily gallons into HA via `recorder/import_statistics`
   (statistic `veolia:water_consumption`) using the add-on's Supervisor token.

## Install

1. In HA: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add
   `https://github.com/kunalkhosla/veolia-water`.
2. Install **Veolia Water → HA**, open **Configuration**, fill in the options
   (copy values from 1Password), **Save**, then **Start**.
3. First run: set `dry_run: true` to verify scraping in the add-on log before
   writing to HA; then turn it off.

## Options

| Option | Meaning |
|---|---|
| `veolia_username` / `veolia_password` | portal login (`op://Private/mywater.veolia.us`) |
| `gmail_username` / `gmail_app_password` | Gmail + app password for reading the OTP (`op://hestia/Google app password — veolia-hestia`) |
| `account_number` | the number in `/water-usage/<n>` |
| `days_back` | how many days of history to pull each run (default 45) |
| `run_interval_hours` | how often to run (default 24) |
| `run_on_start` / `dry_run` / `log_level` | behavior toggles |

The Gmail app password is named **"Veolia HA Integration"** on the Google
account — revoke it there to cut access. No secrets are stored in this repo.
