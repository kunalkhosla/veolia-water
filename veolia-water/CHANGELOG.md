# Changelog

## 0.1.0

- Initial release. Headless-Chromium scraper for the Veolia (ex-SUEZ) NJ portal
  with session reuse, email-OTP login via Gmail IMAP, daily-CSV pull, and import
  into HA long-term statistics (`veolia:water_consumption`).
- `dry_run` option to validate scraping before writing to HA.
