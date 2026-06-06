#!/usr/bin/env python3
"""
Veolia (ex-SUEZ) NJ water-usage scraper -> Home Assistant statistics.

Runs as an HA add-on. Reuses a saved browser session so normal runs need no
login; when the session expires it logs in (email+password), gets the EMAIL
one-time code from Gmail over IMAP, and re-saves the session. Daily usage is
pulled from the portal's own CSV export and imported into HA as the long-term
statistic `veolia:water_consumption` (shows up in the Energy > Water section).

Selectors for the login / OTP pages are best-effort with fallbacks; the first
real login may need tuning — run with dry_run + log_level=debug and read the
add-on log, which dumps page text when a step can't be located.
"""
import csv
import imaplib
import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright
import websocket  # websocket-client

PORTAL = "https://mywater.veolia.us"
DATA_DIR = "/data"
OPTIONS_PATH = os.path.join(DATA_DIR, "options.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")

STAT_ID = "veolia:water_consumption"
STAT_SOURCE = "veolia"
STAT_NAME = "Veolia Water Consumption"
UNIT = "gal"
TZ = ZoneInfo("America/New_York")  # portal renders usage in service-area local time

IMAP_HOST = "imap.gmail.com"
OTP_FROM_HINTS = ("veolia", "mywater", "suez")
OTP_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

log = logging.getLogger("veolia")


# --------------------------------------------------------------------------- #
# options
# --------------------------------------------------------------------------- #
def load_options() -> dict:
    opts = {}
    try:
        with open(OPTIONS_PATH) as f:
            opts = json.load(f)
    except FileNotFoundError:
        pass
    # allow env overrides (handy for local testing outside the add-on)
    for k in ("veolia_username", "veolia_password", "gmail_username",
              "gmail_app_password", "account_number"):
        env = os.environ.get(k.upper())
        if env:
            opts[k] = env
    opts.setdefault("days_back", 45)
    opts.setdefault("run_interval_hours", 24)
    opts.setdefault("run_on_start", True)
    opts.setdefault("dry_run", False)
    opts.setdefault("log_level", "info")
    opts.setdefault("gmail_username", opts.get("veolia_username", ""))
    return opts


def require(opts, *keys):
    missing = [k for k in keys if not opts.get(k)]
    if missing:
        raise RuntimeError(f"missing required option(s): {', '.join(missing)}")


# --------------------------------------------------------------------------- #
# small DOM helpers (resilient to selector drift)
# --------------------------------------------------------------------------- #
def _first_visible(page, selectors):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def fill_first(page, selectors, value, what):
    loc = _first_visible(page, selectors)
    if not loc:
        raise RuntimeError(f"could not find field: {what}")
    loc.fill(value)
    log.debug("filled %s", what)


def click_first(page, selectors, what, required=True):
    loc = _first_visible(page, selectors)
    if not loc:
        if required:
            raise RuntimeError(f"could not find control: {what}")
        return False
    loc.click()
    log.debug("clicked %s", what)
    return True


def dump_page(page, label):
    try:
        txt = page.inner_text("body")[:1500]
    except Exception:
        txt = "<no body>"
    log.info("[%s] url=%s\n----- page text -----\n%s\n---------------------", label, page.url, txt)


# --------------------------------------------------------------------------- #
# Gmail OTP
# --------------------------------------------------------------------------- #
def fetch_otp(opts, since_dt, timeout=150):
    """Poll Gmail for a 6-digit code from Veolia received after `since_dt`."""
    user = opts["gmail_username"]
    pw = str(opts["gmail_app_password"]).replace(" ", "")
    deadline = time.time() + timeout
    log.info("waiting for OTP email (up to %ss)...", timeout)
    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(IMAP_HOST)
            M.login(user, pw)
            M.select("INBOX")
            # search recent; filter by date+sender in python (robust across providers)
            since = since_dt.strftime("%d-%b-%Y")
            typ, data = M.search(None, f'(SINCE "{since}")')
            ids = data[0].split() if data and data[0] else []
            for mid in reversed(ids[-25:]):
                typ, msg_data = M.fetch(mid, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = message_from_bytes(msg_data[0][1])
                frm = (msg.get("From") or "").lower()
                subj = (msg.get("Subject") or "")
                try:
                    when = parsedate_to_datetime(msg.get("Date"))
                except Exception:
                    when = None
                if when and when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when and when < since_dt - timedelta(minutes=2):
                    continue
                if not any(h in frm for h in OTP_FROM_HINTS):
                    continue
                body = _msg_text(msg)
                m = OTP_RE.search(subj) or OTP_RE.search(body)
                if m:
                    log.info("got OTP from %r (subject=%r)", frm, subj[:60])
                    M.logout()
                    return m.group(1)
            M.logout()
        except Exception as e:
            log.warning("IMAP poll error: %s", e)
        time.sleep(6)
    raise RuntimeError("timed out waiting for OTP email")


def _msg_text(msg):
    out = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    out.append(part.get_payload(decode=True).decode(errors="ignore"))
                except Exception:
                    pass
    else:
        try:
            out.append(msg.get_payload(decode=True).decode(errors="ignore"))
        except Exception:
            pass
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #
def is_logged_in(page) -> bool:
    return "/user/login" not in page.url


def login(page, opts):
    log.info("session invalid -> logging in")
    page.goto(f"{PORTAL}/user/login", wait_until="networkidle")
    fill_first(page,
               ['input[type="email"]', 'input[name="name"]', '#edit-name',
                'input[name="mail"]'],
               opts["veolia_username"], "email")
    fill_first(page,
               ['input[type="password"]', 'input[name="pass"]', '#edit-pass'],
               opts["veolia_password"], "password")
    otp_requested_at = datetime.now(timezone.utc)
    click_first(page,
                ['#edit-submit', 'button[type="submit"]', 'input[type="submit"]',
                 'button:has-text("Sign In")', 'text=Sign In'],
                "sign in")
    page.wait_for_load_state("networkidle")

    # OTP delivery-method step (choose EMAIL), if present
    if _looks_like_mfa(page):
        log.info("MFA step detected; choosing email delivery")
        # pick the "email" option (radio/button/label), then continue/send
        click_first(page,
                    ['input[type="radio"][value*="email" i]',
                     'label:has-text("Email")', 'button:has-text("Email")',
                     'text=/email/i'],
                    "email delivery option", required=False)
        otp_requested_at = datetime.now(timezone.utc)
        click_first(page,
                    ['button:has-text("Send")', 'button:has-text("Continue")',
                     '#edit-submit', 'button[type="submit"]', 'input[type="submit"]'],
                    "send code", required=False)
        page.wait_for_load_state("networkidle")

        code = fetch_otp(opts, otp_requested_at)
        fill_first(page,
                   ['input[autocomplete="one-time-code"]', 'input[name*="code" i]',
                    'input[name*="otp" i]', 'input[type="tel"]', 'input[type="text"]'],
                   code, "otp code")
        click_first(page,
                    ['button:has-text("Verify")', 'button:has-text("Submit")',
                     '#edit-submit', 'button[type="submit"]', 'input[type="submit"]'],
                    "verify code", required=False)
        page.wait_for_load_state("networkidle")

    if not is_logged_in(page):
        dump_page(page, "login-failed")
        raise RuntimeError("login did not complete (still on login page)")
    log.info("login OK")


def _looks_like_mfa(page):
    try:
        body = page.inner_text("body").lower()
    except Exception:
        return False
    return any(s in body for s in
               ("one-time", "verification code", "send a code", "security code",
                "two-step", "passcode", "we sent", "choose how"))


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def fetch_daily_csv(page, opts):
    end = datetime.now(TZ).date()
    start = end - timedelta(days=int(opts["days_back"]))
    url = (f"/cp-vna-suez-water-usage/data/{start.isoformat()}/"
           f"{end.isoformat()}/DAILY/csv")
    log.info("fetching usage CSV: %s", url)
    text = page.evaluate(
        """async (u) => { const r = await fetch(u); return await r.text(); }""", url)
    if not text or "<html" in text[:200].lower():
        dump_page(page, "csv-unexpected")
        raise RuntimeError("CSV fetch returned non-CSV (session lost?)")
    return text


def parse_csv(text):
    """Return [(date(YYYY-MM-DD), gallons float)] aggregated per day."""
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    log.info("CSV header: %s (%d data rows)", header, len(rows) - 1)

    def find(cands):
        for i, h in enumerate(header):
            if any(c in h for c in cands):
                return i
        return None

    di = find(("date", "time", "day", "read"))
    vi = find(("gallon", "usage", "consum", "volume", "ccf"))
    if di is None or vi is None:
        # fall back to positional: first col date, last numeric col value
        di = 0 if di is None else di
        vi = len(header) - 1 if vi is None else vi
        log.warning("falling back to columns date=%d value=%d", di, vi)

    agg = {}
    for r in rows[1:]:
        if max(di, vi) >= len(r):
            continue
        d = _parse_date(r[di].strip())
        v = _parse_float(r[vi])
        if d is None or v is None:
            continue
        agg[d] = agg.get(d, 0.0) + v
    return sorted(agg.items())


def _parse_date(s):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%dT%H:%M:%S",
                "%m/%d/%Y %H:%M", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_float(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Home Assistant import
# --------------------------------------------------------------------------- #
def ha_ws():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN not set (need homeassistant_api: true)")
    ws = websocket.create_connection("ws://supervisor/core/websocket",
                                     timeout=30)
    hello = json.loads(ws.recv())
    if hello.get("type") != "auth_required":
        raise RuntimeError(f"unexpected HA hello: {hello}")
    ws.send(json.dumps({"type": "auth", "access_token": token}))
    if json.loads(ws.recv()).get("type") != "auth_ok":
        raise RuntimeError("HA auth failed")
    return ws


_msg_id = [0]


def ha_call(ws, msg):
    _msg_id[0] += 1
    msg["id"] = _msg_id[0]
    ws.send(json.dumps(msg))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == msg["id"] and r.get("type") == "result":
            return r


def last_sum_before(ws, start_iso):
    r = ha_call(ws, {
        "type": "recorder/statistics_during_period",
        "start_time": (datetime.fromisoformat(start_iso)
                       - timedelta(days=400)).isoformat(),
        "end_time": start_iso,
        "statistic_ids": [STAT_ID],
        "period": "day",
    })
    pts = (r.get("result") or {}).get(STAT_ID) or []
    return pts[-1].get("sum", 0.0) if pts else 0.0


def import_stats(ws, daily):
    """daily = [(YYYY-MM-DD, gallons)] ascending."""
    first_start = datetime.fromisoformat(daily[0][0]).replace(tzinfo=TZ).isoformat()
    baseline = last_sum_before(ws, first_start)
    log.info("baseline sum before window: %.2f gal", baseline)
    stats, running = [], baseline
    for d, gal in daily:
        running += gal
        start = datetime.fromisoformat(d).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=TZ)
        stats.append({"start": start.isoformat(), "state": running, "sum": running})
    r = ha_call(ws, {
        "type": "recorder/import_statistics",
        "metadata": {
            "has_mean": False, "has_sum": True, "name": STAT_NAME,
            "source": STAT_SOURCE, "statistic_id": STAT_ID,
            "unit_of_measurement": UNIT,
        },
        "stats": stats,
    })
    if not r.get("success"):
        raise RuntimeError(f"import_statistics failed: {r.get('error')}")
    log.info("imported %d daily points (last sum=%.2f gal)", len(stats), running)


# --------------------------------------------------------------------------- #
# one cycle
# --------------------------------------------------------------------------- #
def run_once(opts):
    require(opts, "veolia_username", "veolia_password", "account_number",
            "gmail_username", "gmail_app_password")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx_kw = {"user_agent": UA, "locale": "en-US"}
        if os.path.exists(STATE_PATH):
            ctx_kw["storage_state"] = STATE_PATH
        ctx = browser.new_context(**ctx_kw)
        page = ctx.new_page()
        page.set_default_timeout(45000)
        try:
            page.goto(f"{PORTAL}/water-usage/{opts['account_number']}",
                      wait_until="networkidle")
            if not is_logged_in(page):
                login(page, opts)
                page.goto(f"{PORTAL}/water-usage/{opts['account_number']}",
                          wait_until="networkidle")
            ctx.storage_state(path=STATE_PATH)  # persist refreshed session

            csv_text = fetch_daily_csv(page, opts)
            daily = parse_csv(csv_text)
            if not daily:
                raise RuntimeError("no usage rows parsed from CSV")
            log.info("parsed %d days: %s ... %s",
                     len(daily), daily[0], daily[-1])

            if opts["dry_run"]:
                log.info("DRY RUN: not writing to HA. Sample rows:")
                for d, g in daily[-7:]:
                    log.info("   %s  %.2f gal", d, g)
                return
            ws = ha_ws()
            try:
                import_stats(ws, daily)
            finally:
                ws.close()
        finally:
            ctx.close()
            browser.close()


def main():
    opts = load_options()
    logging.basicConfig(
        level=getattr(logging, str(opts["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout)
    interval = max(1, int(opts["run_interval_hours"])) * 3600

    if not opts["run_on_start"]:
        log.info("run_on_start=false; sleeping %ss before first run", interval)
        time.sleep(interval)

    while True:
        try:
            run_once(opts)
        except Exception as e:
            log.exception("run failed: %s", e)
        log.info("sleeping %.1fh until next run", interval / 3600)
        time.sleep(interval)
        opts = load_options()  # pick up config changes between runs


if __name__ == "__main__":
    main()
