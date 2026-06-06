# Changelog

## 0.2.3

- Dismiss the EU cookie-consent banner (privacy-preserving / decline) before
  interacting with the login form — on a fresh profile it overlaid the form and
  intercepted the Sign In click, timing out login.

## 0.2.2

- Replace `xvfb-run` with a manual `Xvfb :99` start (via run.sh) + `DISPLAY=:99`,
  removing the `xauth` dependency that crash-looped the add-on.

## 0.2.1

- Fix: install `xauth` alongside `xvfb` (xvfb-run needs it; without it the add-on
  crash-looped with "xauth command not found").

## 0.2.0

- **Cloudflare bypass:** switch from Playwright to **patchright** and run a
  headed Chromium under **Xvfb** — plain Playwright (headless or headed, Chromium
  or real Chrome) gets stuck on the portal's Turnstile; patchright headed clears
  it in seconds. Uses a persistent profile (`/data/chrome-profile`) so the
  Cloudflare clearance + login session survive across runs (login/OTP only on
  expiry).
- **Email-OTP login** via Gmail IMAP, with robust waits (waits for the post-OTP
  page to settle instead of closing mid-load).
- **Hourly usage** imported as `veolia:water_consumption` (finest resolution the
  portal exposes; HA rolls it up to daily/monthly for the Energy dashboard).
- **Monthly bill cost** imported as `veolia:water_cost` from the statement
  history.
- **Billing sensors:** `sensor.veolia_water_balance_due`,
  `sensor.veolia_water_due_date`, `sensor.veolia_water_last_bill`.
- **Leak helper:** `sensor.veolia_water_last_hour` (latest hourly gallons +
  last-24h list + recent overnight-min) for leak-detection automations.
- Service window now derived from the account-summary statement dates (the CSV
  endpoint serves HTML for out-of-range requests).

## 0.1.2

- Robust navigation: drop `networkidle` (the portal's analytics/New Relic sockets
  never go idle) in favor of `domcontentloaded`, and retry transient container
  network errors (`ERR_NETWORK_CHANGED` etc.) instead of failing the run.
- On a failed run, retry up to 3× and then re-attempt in ≤1h rather than sleeping
  the full 24h interval.
- Chromium launched with background-networking/sync/translate disabled.

## 0.1.1

- Fix add-on build: hardcode the Debian/glibc base image (`python:3.12-slim-bookworm`)
  in the Dockerfile instead of relying on `BUILD_FROM`/`build.yaml`, which
  Supervisor was not applying — the build fell back to the Alpine base (no `pip`,
  can't run Chromium). Removed the unused `build.yaml`.

## 0.1.0

- Initial release. Headless-Chromium scraper for the Veolia (ex-SUEZ) NJ portal
  with session reuse, email-OTP login via Gmail IMAP, daily-CSV pull, and import
  into HA long-term statistics (`veolia:water_consumption`).
- `dry_run` option to validate scraping before writing to HA.
