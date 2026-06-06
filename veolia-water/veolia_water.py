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
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes
from email.utils import parsedate_to_datetime
import urllib.request
from zoneinfo import ZoneInfo

# patchright is a stealth-patched drop-in for Playwright; plain Playwright (even
# real Chrome) gets fingerprinted and stuck on the portal's Cloudflare Turnstile.
from patchright.sync_api import TimeoutError as PWTimeout
from patchright.sync_api import sync_playwright
import websocket  # websocket-client

PORTAL = "https://mywater.veolia.us"
DATA_DIR = os.environ.get("VEOLIA_DATA_DIR", "/data")  # override for local testing
OPTIONS_PATH = os.path.join(DATA_DIR, "options.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")

STAT_ID = "veolia:water_consumption"
STAT_SOURCE = "veolia"
STAT_NAME = "Veolia Water Consumption"
UNIT = "gal"
STAT_COST_ID = "veolia:water_cost"
STAT_COST_NAME = "Veolia Water Cost"
COST_UNIT = "USD"
TZ = ZoneInfo("America/New_York")  # portal renders usage in service-area local time

# Headless does not pass Cloudflare Turnstile; run headed (under Xvfb in the
# add-on). Override only for experiments.
HEADLESS = os.environ.get("HEADLESS", "false").strip().lower() in ("1", "true", "yes")

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

    def _bool(s):
        return str(s).strip().lower() in ("1", "true", "yes", "on")
    for key, cast in (("days_back", int), ("run_interval_hours", int),
                      ("run_on_start", _bool), ("dry_run", _bool),
                      ("log_level", str)):
        v = os.environ.get(key.upper())
        if v is not None:
            opts[key] = cast(v)
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


TRANSIENT_NAV = ("ERR_NETWORK_CHANGED", "ERR_NETWORK_IO_SUSPENDED",
                 "ERR_CONNECTION_RESET", "ERR_TIMED_OUT", "Timeout")


def goto(page, url, attempts=4):
    """Navigate with retries on transient container-network errors.

    Uses domcontentloaded (not networkidle): the portal keeps analytics/New
    Relic sockets open, so networkidle waits needlessly and is fragile to blips.
    """
    last = None
    for i in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            wait_cf_clear(page)
            return
        except PWTimeout as e:
            last = e
        except Exception as e:
            last = e
            if not any(t in str(e) for t in TRANSIENT_NAV):
                raise
        log.warning("goto %s failed (attempt %d/%d): %s", url, i, attempts, last)
        time.sleep(5)
    raise last


def wait_cf_clear(page, timeout=45):
    """Wait for the Cloudflare Turnstile interstitial ('Just a moment...') to
    auto-clear. patchright + a headed/persistent browser passes it in ~5-10s."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if "just a moment" not in (page.title() or "").lower():
                return True
        except Exception:
            pass
        time.sleep(2)
    log.warning("Cloudflare challenge did not clear within %ss", timeout)
    return False


def settle(page):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass


def dismiss_cookie_banner(page):
    """The EU cookie-compliance banner overlays the page and intercepts clicks.
    Decline non-essential (privacy-preserving), then hard-remove it so it can't
    block form clicks. Clicking 'Dismiss all' sets a cookie so it stays gone."""
    btn = _first_visible(page, [
        'button:has-text("Dismiss all")', '.eu-cookie-compliance-dismiss-button',
        'button:has-text("Decline")', 'button:has-text("Necessary")',
        'button:has-text("Reject")'])
    if btn:
        try:
            btn.click(timeout=5000)
            log.info("dismissed cookie banner")
        except Exception:
            pass
    try:
        page.evaluate("() => { const e = document.querySelector('#sliding-popup');"
                      " if (e) e.remove(); }")
    except Exception:
        pass


def dump_page(page, label):
    try:
        txt = page.inner_text("body")[:1500]
    except Exception:
        txt = "<no body>"
    log.info("[%s] url=%s\n----- page text -----\n%s\n---------------------", label, page.url, txt)
    try:
        path = os.path.join(DATA_DIR, f"debug-{label}.png")
        page.screenshot(path=path, full_page=True)
        log.info("saved screenshot %s", path)
    except Exception as e:
        log.debug("screenshot failed: %s", e)


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
    """Authenticated pages carry a logout link; the anonymous 'page has moved /
    Sign In' page and the login form do not. (uid in drupalSettings proved
    unreliable on this site.)"""
    try:
        if page.locator('a[href*="/user/logout"], a[href*="logout"]').count() > 0:
            return True
    except Exception:
        pass
    try:
        uid = page.evaluate(
            "() => (window.drupalSettings && window.drupalSettings.user "
            "&& window.drupalSettings.user.uid) || 0")
        return int(uid) > 0
    except Exception:
        return False


OTP_DETECT_SELS = ['input[autocomplete="one-time-code"]', 'input[name*="code" i]',
                   'input[name*="otp" i]', 'input[id*="code" i]', 'input[type="tel"]']
OTP_FILL_SELS = OTP_DETECT_SELS + ['input[type="text"]']


def login(page, opts):
    log.info("session invalid -> logging in")
    goto(page, f"{PORTAL}/user/login")
    # the form isn't instantly present after Cloudflare clears — wait for it
    try:
        page.wait_for_selector('input[name="name"], input[type="email"], #edit-name',
                               state="visible", timeout=45000)
    except PWTimeout:
        dump_page(page, "no-login-form")
        raise RuntimeError("login form never appeared (Cloudflare not cleared?)")

    dismiss_cookie_banner(page)
    fill_first(page, ['input[name="name"]', 'input[type="email"]', '#edit-name',
                      'input[name="mail"]'], opts["veolia_username"], "email")
    fill_first(page, ['input[name="pass"]', 'input[type="password"]', '#edit-pass'],
               opts["veolia_password"], "password")
    click_first(page, ['#edit-submit', 'button[type="submit"]', 'input[type="submit"]',
                       'button:has-text("Sign In")', 'text=Sign In'], "sign in")

    # After submit we either land logged-in (risk-based MFA may be skipped) or
    # hit an MFA/OTP step. Poll instead of guessing with a fixed delay.
    state = _wait_post_submit(page, timeout=35)
    log.info("post-submit state: %s", state)
    if state == "mfa":
        log.info("MFA step: choosing email delivery")
        dismiss_cookie_banner(page)
        click_first(page, ['input[type="radio"][value*="email" i]',
                           'label:has-text("Email")', 'button:has-text("Email")',
                           'text=/email/i'], "email delivery option", required=False)
        otp_requested_at = datetime.now(timezone.utc)
        click_first(page, ['button:has-text("Send")', 'button:has-text("Continue")',
                           '#edit-submit', 'button[type="submit"]', 'input[type="submit"]'],
                    "send code", required=False)
        try:
            page.wait_for_selector(", ".join(OTP_DETECT_SELS), state="visible", timeout=30000)
        except PWTimeout:
            pass
        code = fetch_otp(opts, otp_requested_at)
        fill_first(page, OTP_FILL_SELS, code, "otp code")
        click_first(page, ['button:has-text("Verify")', 'button:has-text("Submit")',
                           '#edit-submit', 'button[type="submit"]', 'input[type="submit"]'],
                    "verify code", required=False)

    # Post-login / post-OTP the portal can spin for many seconds — wait for the
    # authenticated state to actually appear rather than closing mid-load.
    if not wait_logged_in(page, timeout=75):
        dump_page(page, "login-failed")
        raise RuntimeError("login did not complete")
    log.info("login OK")


def _has_otp_input(page):
    return _first_visible(page, OTP_DETECT_SELS) is not None


def _looks_like_mfa(page):
    try:
        body = page.inner_text("body").lower()
    except Exception:
        return False
    return any(s in body for s in
               ("one-time", "verification code", "send a code", "security code",
                "two-step", "passcode", "we sent", "choose how"))


def _wait_post_submit(page, timeout=35):
    """After credential submit: 'in' (logged in), 'mfa' (OTP step), or 'timeout'."""
    end = time.time() + timeout
    while time.time() < end:
        if is_logged_in(page):
            return "in"
        if _has_otp_input(page) or _looks_like_mfa(page):
            return "mfa"
        time.sleep(1.5)
    return "timeout"


def wait_logged_in(page, timeout=75):
    end = time.time() + timeout
    while time.time() < end:
        if is_logged_in(page):
            return True
        time.sleep(2)
    return False


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
BILL_RE_BAL = re.compile(r"Balance Due:\s*\$([\d,]+\.\d{2})", re.I)
BILL_RE_DUE = re.compile(r"Due Date:\s*([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})", re.I)
BILL_RE_STMT = re.compile(
    r"(\d{2}/\d{2}/\d{2})\s+(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})\s+\$([\d,]+\.\d{2})")


def _usdate(s):       # "06/02/26" -> "2026-06-02"
    return datetime.strptime(s, "%m/%d/%y").date().isoformat()


def _duedate(s):      # "Jun 17, 2026" -> "2026-06-17"
    for f in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s.strip(), f).date().isoformat()
        except ValueError:
            continue
    return None


def scrape_account(page):
    """Parse the (already-loaded) account-summary page for billing info +
    statement history. The latest statement's service dates also give us a
    guaranteed-valid window for the usage CSV endpoint."""
    body = page.inner_text("body")
    bal = BILL_RE_BAL.search(body)
    due = BILL_RE_DUE.search(body)
    stmts = []
    for bd, ss, se, amt in BILL_RE_STMT.findall(body):
        stmts.append({"bill_date": _usdate(bd), "service_start": _usdate(ss),
                      "service_end": _usdate(se),
                      "amount": float(amt.replace(",", ""))})
    acct = {
        "balance_due": float(bal.group(1).replace(",", "")) if bal else None,
        "due_date": _duedate(due.group(1)) if due else None,
        "statements": stmts,
    }
    log.info("billing: balance=%s due=%s statements=%d",
             acct["balance_due"], acct["due_date"], len(stmts))
    return acct


def fetch_usage_csv(page, window, granularity="HOURLY"):
    """window = (service_start ISO, service_end ISO). The CSV endpoint serves
    HTML for ranges outside available data, so we anchor to the real service
    start and prefer through-today (open period), falling back to the bill end.
    HOURLY is the finest resolution the portal exposes."""
    today = datetime.now(TZ).date()
    start = date.fromisoformat(window[0])
    bill_end = date.fromisoformat(window[1])
    for end in (today, bill_end):
        if end < start:
            continue
        url = (f"/cp-vna-suez-water-usage/data/{start.isoformat()}/"
               f"{end.isoformat()}/{granularity}/csv")
        log.info("fetching usage CSV: %s", url)
        text = page.evaluate(
            "async (u) => { const r = await fetch(u); return await r.text(); }", url)
        if text and text.lstrip().lower().startswith("meter"):
            return text
        log.warning("range %s..%s non-CSV (%d bytes)", start, end, len(text or ""))
    dump_page(page, "csv-unexpected")
    raise RuntimeError("CSV fetch returned non-CSV for all candidate ranges")


def parse_rows(text):
    """Parse the usage CSV into [(naive local datetime, gallons)] per interval,
    ascending. Columns: Meter,Start Date,End Date,Water Consumption Gallons,Data Flag.
    For HOURLY the Start Date carries the hour; we keep full resolution."""
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    log.info("CSV header: %s (%d data rows)", header, len(rows) - 1)

    def find(cands, default):
        for i, h in enumerate(header):
            if any(c in h for c in cands):
                return i
        return default

    di = find(("start date", "date", "time", "read"), 1)
    vi = find(("gallon", "consum", "usage", "volume"), 3)

    out = []
    for r in rows[1:]:
        if max(di, vi) >= len(r):
            continue
        dt = _parse_dt(r[di].strip())
        v = _parse_float(r[vi])
        if dt is None or v is None:
            continue
        out.append((dt, v))
    out.sort(key=lambda x: x[0])
    return out


def aggregate_daily(rows):
    agg = {}
    for dt, v in rows:
        agg[dt.date().isoformat()] = agg.get(dt.date().isoformat(), 0.0) + v
    return sorted(agg.items())


def _parse_dt(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
                "%m/%d/%Y %H:%M", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt)
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


def last_sum_before(ws, stat_id, start_iso):
    r = ha_call(ws, {
        "type": "recorder/statistics_during_period",
        "start_time": (datetime.fromisoformat(start_iso)
                       - timedelta(days=800)).isoformat(),
        "end_time": start_iso,
        "statistic_ids": [stat_id],
        "period": "day",
    })
    pts = (r.get("result") or {}).get(stat_id) or []
    return pts[-1].get("sum", 0.0) if pts else 0.0


def _import(ws, stat_id, name, unit, points):
    """points = [(tz-aware hour-aligned datetime, value)] ascending. Builds a
    cumulative sum continuing from whatever HA already has before the window."""
    first_start = points[0][0].isoformat()
    baseline = last_sum_before(ws, stat_id, first_start)
    stats, running = [], baseline
    for start, val in points:
        running += val
        stats.append({"start": start.isoformat(), "state": running, "sum": running})
    r = ha_call(ws, {
        "type": "recorder/import_statistics",
        "metadata": {"has_mean": False, "has_sum": True, "name": name,
                     "source": STAT_SOURCE, "statistic_id": stat_id,
                     "unit_of_measurement": unit},
        "stats": stats,
    })
    if not r.get("success"):
        raise RuntimeError(f"import_statistics({stat_id}) failed: {r.get('error')}")
    log.info("imported %d points into %s (last sum=%.2f, baseline=%.2f)",
             len(stats), stat_id, running, baseline)


def import_usage(ws, rows):
    """rows = [(naive local datetime, gallons)] hourly, ascending."""
    pts = [(dt.replace(minute=0, second=0, microsecond=0, tzinfo=TZ), g)
           for dt, g in rows]
    _import(ws, STAT_ID, STAT_NAME, UNIT, pts)


def import_cost(ws, statements):
    """statements newest-first; import each monthly bill amount as cumulative cost."""
    pts = []
    for s in sorted(statements, key=lambda x: x["bill_date"]):
        start = datetime.fromisoformat(s["bill_date"]).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=TZ)
        pts.append((start, s["amount"]))
    if pts:
        _import(ws, STAT_COST_ID, STAT_COST_NAME, COST_UNIT, pts)


# --------------------------------------------------------------------------- #
# HA sensors (set via the Core REST API using the add-on's Supervisor token)
# --------------------------------------------------------------------------- #
def ha_set_state(entity_id, state, attributes):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        log.warning("no SUPERVISOR_TOKEN; skipping sensor %s", entity_id)
        return
    body = json.dumps({"state": state, "attributes": attributes}).encode()
    req = urllib.request.Request(
        f"http://supervisor/core/api/states/{entity_id}", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
        log.info("set %s = %s", entity_id, state)
    except Exception as e:
        log.warning("failed to set %s: %s", entity_id, e)


def push_billing_sensors(account):
    if account.get("balance_due") is not None:
        ha_set_state("sensor.veolia_water_balance_due", account["balance_due"],
                     {"unit_of_measurement": "USD", "device_class": "monetary",
                      "friendly_name": "Veolia Water Balance Due", "icon": "mdi:cash"})
    if account.get("due_date"):
        ha_set_state("sensor.veolia_water_due_date", account["due_date"],
                     {"device_class": "date",
                      "friendly_name": "Veolia Water Due Date", "icon": "mdi:calendar-clock"})
    if account.get("statements"):
        s = account["statements"][0]
        ha_set_state("sensor.veolia_water_last_bill", s["amount"],
                     {"unit_of_measurement": "USD", "device_class": "monetary",
                      "friendly_name": "Veolia Water Last Bill", "icon": "mdi:receipt",
                      "bill_date": s["bill_date"], "service_start": s["service_start"],
                      "service_end": s["service_end"]})


def push_usage_sensor(rows):
    """Latest hourly reading + recent-window attributes for leak automations."""
    if not rows:
        return
    last_dt, last_gal = rows[-1]
    last24 = rows[-24:]
    overnight = [g for dt, g in rows[-30:] if 1 <= dt.hour <= 5]
    ha_set_state("sensor.veolia_water_last_hour", round(last_gal, 2), {
        "unit_of_measurement": "gal", "device_class": "water",
        "state_class": "measurement", "friendly_name": "Veolia Water Last Hour",
        "icon": "mdi:water",
        "reading_time": last_dt.replace(tzinfo=TZ).isoformat(),
        "last_24h_total_gal": round(sum(g for _, g in last24), 2),
        "last_24h_gal": [round(g, 2) for _, g in last24],
        "recent_overnight_min_gal": round(min(overnight), 2) if overnight else None,
    })


# --------------------------------------------------------------------------- #
# one cycle
# --------------------------------------------------------------------------- #
def run_once(opts):
    require(opts, "veolia_username", "veolia_password", "account_number",
            "gmail_username", "gmail_app_password")
    profile_dir = os.path.join(DATA_DIR, "chrome-profile")
    with sync_playwright() as p:
        # Persistent context (a real on-disk profile) keeps the Cloudflare
        # clearance + login session across runs, and is far stealthier than a
        # fresh context. Headed only — headless does not pass Turnstile.
        ctx = p.chromium.launch_persistent_context(
            profile_dir, headless=HEADLESS, no_viewport=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(45000)
        try:
            goto(page, f"{PORTAL}/account-summary")
            if not is_logged_in(page):
                login(page, opts)
                goto(page, f"{PORTAL}/account-summary")

            account = scrape_account(page)
            if not account["statements"]:
                dump_page(page, "no-statements")
                raise RuntimeError("no billing statements found on account summary")
            latest = account["statements"][0]
            window = (latest["service_start"], latest["service_end"])

            rows = parse_rows(fetch_usage_csv(page, window, "HOURLY"))
            if not rows:
                raise RuntimeError("no usage rows parsed from CSV")
            daily = aggregate_daily(rows)
            log.info("parsed %d hourly rows (%s .. %s); %d days",
                     len(rows), rows[0][0], rows[-1][0], len(daily))

            if opts["dry_run"]:
                log.info("DRY RUN — not writing to HA.")
                log.info("  balance due: $%s (due %s)",
                         account["balance_due"], account["due_date"])
                for s in account["statements"][:6]:
                    log.info("  bill %s  %s..%s  $%.2f", s["bill_date"],
                             s["service_start"], s["service_end"], s["amount"])
                for d, g in daily[-5:]:
                    log.info("  daily %s  %.2f gal", d, g)
                for dt, g in rows[-6:]:
                    log.info("  hourly %s  %.2f gal", dt, g)
                return

            ws = ha_ws()
            try:
                import_usage(ws, rows)
                import_cost(ws, account["statements"])
            finally:
                ws.close()
            push_billing_sensors(account)
            push_usage_sensor(rows)
        finally:
            ctx.close()


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

    once = "--once" in sys.argv  # local testing: run a single cycle and exit
    retries = 1 if once else 3
    while True:
        ok = False
        for attempt in range(1, retries + 1):
            try:
                run_once(opts)
                ok = True
                break
            except Exception as e:
                log.exception("run failed (attempt %d/%d): %s", attempt, retries, e)
                if attempt < retries:
                    time.sleep(60)
        if once:
            sys.exit(0 if ok else 1)
        nap = interval if ok else min(3600, interval)  # retry sooner on failure
        log.info("sleeping %.1fh until next run", nap / 3600)
        time.sleep(nap)
        opts = load_options()  # pick up config changes between runs


if __name__ == "__main__":
    main()
