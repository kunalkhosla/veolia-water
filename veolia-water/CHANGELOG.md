# Changelog

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
