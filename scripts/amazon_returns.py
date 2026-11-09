"""
Amazon Returns Tracker
======================
Scrapes your Amazon order history and writes every item that is still
returnable (with its return-by date) to data/returns.json for Home Assistant.

One script for Mac, Linux and Raspberry Pi.

First run (opens a visible browser so you can sign in):
    python scripts/amazon_returns.py --login

Daily run (headless, reuses the saved session):
    python scripts/amazon_returns.py

Always uses Brave Browser (found automatically on macOS and Linux/Pi).
Point at a different install with --browser-path or AMAZON_RETURNS_BROWSER.

Exit codes: 0 = ok, 2 = login required, 1 = other error.
The JSON file is written in every case, with a "status" field, so Home
Assistant can tell "nothing to return" apart from "the scraper is broken".
"""

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path

from dateutil import parser as dateparser

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT / "data"          # only this folder is served over HTTP
DEFAULT_SESSION_DIR = ROOT / "amazon_session"  # cookies: never serve this

BASE_URL = "https://www.amazon.com"
ORDERS_URL = BASE_URL + "/your-orders/orders?timeFilter={filter}&startIndex={start}"
ORDERS_PER_PAGE = 10

# URLs Amazon uses for the orders page (old and new).
ORDERS_URL_RE = re.compile(r"/(your-orders/orders|gp/your-account/order-history|gp/css/order-history)")
SIGNIN_URL_RE = re.compile(r"/(ap/signin|ap/mfa|ap/cvf|ap/challenge|ax/claim)")


# -------- Page extraction (runs inside the browser) -----------------------

# Finds every "Eligible through <date>" style sentence, then climbs the DOM to
# the nearest box that contains product links and records each product in it.
# Separately keeps climbing to find the order ID. Text-based, so it survives
# most Amazon layout changes.
EXTRACTION_JS = r"""
() => {
    const RETURN_RE = /(?:eligible\s+(?:for\s+(?:return|replacement|refund)\s+)?(?:through|until)|returnable\s+(?:through|until)|return\s+window\s+closes(?:\s+on)?)\s*:?\s*((?:[A-Z][a-z]+\.?)\s+\d{1,2}(?:,?\s+\d{4})?)/i;
    const HINT_RE = /eligible|returnable|return window/i;
    const ORDER_RE = /\b[0-9D]\d{2}-\d{7}-\d{7}\b/;
    const PRODUCT_SEL = "a[href*='/dp/'], a[href*='/gp/product/'], .yohtmlc-product-title";
    const JUNK_RE = /^(buy it again|view your item|write a product review|track package|return or replace items?|get product support)$/i;

    const cleanTitle = (el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
    const titlesIn = (root) => {
        const out = [];
        for (const a of root.querySelectorAll(PRODUCT_SEL)) {
            const t = cleanTitle(a);
            if (t.length >= 4 && !JUNK_RE.test(t) && !out.includes(t)) out.push(t);
        }
        return out;
    };
    const asinOf = (root, title) => {
        for (const a of root.querySelectorAll("a[href*='/dp/'], a[href*='/gp/product/']")) {
            if (cleanTitle(a) !== title) continue;
            const m = (a.getAttribute("href") || "").match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/);
            if (m) return m[1];
        }
        return "";
    };

    // Find the smallest elements whose text contains a full return sentence.
    // Using element text (not single text nodes) handles dates in their own <span>.
    const candidates = new Set();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
        if (!node.nodeValue || !HINT_RE.test(node.nodeValue)) continue;
        const nodeTxt = node.nodeValue.replace(/\s+/g, " ").trim();
        let el = node.parentElement;
        for (let i = 0; el && i < 4; i++, el = el.parentElement) {
            const txt = (el.innerText || el.textContent || "").replace(/\s+/g, " ");
            if (txt.length > 400) break;  // climbed past the sentence into a big block
            const m = txt.match(RETURN_RE);
            if (!m) continue;
            // The match must include this text node, not some other item's sentence.
            const at = txt.indexOf(nodeTxt);
            if (at !== -1 && at <= m.index + m[0].length && at + nodeTxt.length >= m.index) {
                candidates.add(el);
            }
            break;
        }
    }

    const hits = [];
    for (const el of candidates) {
        const m = (el.innerText || el.textContent || "").match(RETURN_RE);
        if (!m) continue;

        let titles = [], box = null, orderId = "", depth = 0;
        for (let cur = el; cur && cur !== document.body && depth < 15; cur = cur.parentElement, depth++) {
            if (!titles.length) {
                const t = titlesIn(cur);
                if (t.length) { titles = t; box = cur; }
            }
            if (!orderId) {
                const idm = (cur.innerText || cur.textContent || "").match(ORDER_RE);
                if (idm) orderId = idm[0];
            }
            if (titles.length && orderId) break;
        }

        if (!titles.length) titles = ["(title not found)"];
        for (const title of titles) {
            hits.push({
                sentence: m[0].replace(/\s+/g, " ").trim(),
                date_text: m[1],
                title,
                asin: box ? asinOf(box, title) : "",
                order_id: orderId,
            });
        }
    }

    const orderIds = [...new Set((document.body.innerText.match(new RegExp(ORDER_RE.source, "g")) || []))];
    const hasNext = !!document.querySelector(".a-pagination .a-last:not(.a-disabled) a");
    return { hits, orderIds, hasNext };
}
"""


# -------- Pure helpers (unit-tested) --------------------------------------

def parse_return_date(raw, today=None):
    """Parse 'December 5, 2026' / 'Dec. 5' into a date.

    If the year is missing, pick the year that puts the date closest to today
    (return windows are at most a few months either side of now).
    """
    today = today or date.today()
    try:
        d = dateparser.parse(raw.replace(".", ""), fuzzy=True, default=datetime(today.year, 1, 1)).date()
    except (ValueError, OverflowError, TypeError):
        return None
    if not re.search(r"\d{4}", raw):
        options = [d.replace(year=today.year + k) for k in (-1, 0, 1)]
        d = min(options, key=lambda x: abs((x - today).days))
    return d


def build_items(hits, today=None):
    """Turn raw browser hits into clean, de-duplicated, sorted items."""
    today = today or date.today()
    seen, items = set(), []
    for h in hits:
        d = parse_return_date(h.get("date_text", ""), today)
        if not d:
            continue
        key = (h.get("order_id", ""), h.get("asin") or h.get("title", ""), d)
        if key in seen:
            continue
        seen.add(key)
        order_id = h.get("order_id", "")
        items.append({
            # NFKC turns the "fancy" bold/italic Unicode letters some sellers use
            # (e.g. "𝟐𝟎𝟐𝟔 𝐔𝐩𝐠𝐫𝐚𝐝𝐞𝐝") into plain text.
            "title": unicodedata.normalize("NFKC", h.get("title") or "(title not found)").strip(),
            "return_by": d.isoformat(),
            "days_left": (d - today).days,
            "order_id": order_id,
            "asin": h.get("asin", ""),
            "order_url": f"{BASE_URL}/gp/your-account/order-details?orderID={order_id}" if order_id else "",
        })
    items.sort(key=lambda r: (r["days_left"], r["title"]))
    return items


def build_output(status, items, threshold, error=None, pages=0, previous=None):
    """Build the JSON document Home Assistant reads.

    When the run fails, keep the previous item list (marked stale) so the
    dashboard still shows something useful, but recompute days_left.
    """
    stale = False
    if status != "ok" and previous and previous.get("all_items"):
        today = date.today()
        items = []
        for it in previous["all_items"]:
            it = dict(it)
            it["days_left"] = (date.fromisoformat(it["return_by"]) - today).days
            items.append(it)
        stale = True

    items = [i for i in items if i["days_left"] >= 0]
    urgent = [i for i in items if i["days_left"] <= threshold]
    now = datetime.now().astimezone().isoformat(timespec="seconds")  # tz-aware for HA
    return {
        "status": status,                      # ok | login_required | error
        "error": error,
        "stale": stale,
        "updated_at": now,
        "last_success": now if status == "ok" else (previous or {}).get("last_success"),
        "pages_scanned": pages,
        "threshold_days": threshold,
        "urgent_count": len(urgent),
        "total_returnable": len(items),
        "next_deadline": items[0]["return_by"] if items else None,
        "urgent_items": urgent,
        "all_items": items,
    }


def write_json_atomic(path, data):
    """Write to a temp file then rename, so HA never reads a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".returns-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def load_previous(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# -------- Browser ---------------------------------------------------------

class LoginRequired(Exception):
    pass


class BotCheck(Exception):
    pass


BOT_CHECK_MARKERS = ("i am human", "check the box to continue",
                     "enter the characters you see below", "validatecaptcha")


def is_bot_check(html):
    """Amazon's "are you a robot" page, shown when it sees too many visits."""
    low = html.lower()
    return any(m in low for m in BOT_CHECK_MARKERS)


def is_signin_page(page):
    if SIGNIN_URL_RE.search(page.url):
        return True
    try:
        return page.locator("form[name='signIn'], #ap_email, #ap_password").count() > 0
    except Exception:
        return False


def _signed_in_url(url):
    return bool(ORDERS_URL_RE.search(url)) and not SIGNIN_URL_RE.search(url)


def wait_for_login(ctx, timeout_s):
    """Wait until the user has signed in, in any tab. Returns the page to use.

    Polls every open tab instead of watching one, so sign-in still works if
    Amazon opens a new tab (2-step verification, passkeys) or the original
    tab gets closed.
    """
    log("\n>>> Sign in to Amazon in the Brave window "
        "(including any 2-step verification). Don't close the window.")
    log(f">>> Waiting up to {timeout_s // 60} minutes...\n")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pages = [pg for pg in ctx.pages if not pg.is_closed()]
        if not pages:
            raise LoginRequired(
                "The Brave window was closed before sign-in finished. "
                "Run --login again and leave the window open.")
        for pg in pages:
            if _signed_in_url(pg.url):
                try:
                    pg.wait_for_load_state("domcontentloaded", timeout=15_000)
                except Exception:
                    continue
                log(">>> Signed in. Continuing.")
                return pg
        try:
            pages[0].wait_for_timeout(1000)
        except Exception:
            time.sleep(1)  # that tab just closed; re-check on the next loop
    raise LoginRequired(f"Sign-in not finished within {timeout_s // 60} minutes.")


def save_debug(page, debug_dir, name):
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(debug_dir / f"{name}.png"), full_page=True)
        (debug_dir / f"{name}.html").write_text(page.content())
        log(f"Saved debug snapshot: {debug_dir / name}.png/.html")
    except Exception as e:  # never let debugging break a run
        log(f"(could not save debug snapshot: {e})")


BRAVE_CANDIDATES = [
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",            # macOS
    str(Path.home() / "Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
    "/usr/bin/brave-browser",                                                  # Debian / Pi OS
    "/usr/bin/brave-browser-stable",
    "/opt/brave.com/brave/brave",
    "/snap/bin/brave",
]


def find_brave(explicit=None):
    """Return the Brave executable path, or raise if Brave isn't installed."""
    if explicit:
        if not Path(explicit).exists():
            raise FileNotFoundError(f"Browser not found at {explicit}")
        return explicit
    for path in BRAVE_CANDIDATES + [shutil.which("brave-browser") or "", shutil.which("brave") or ""]:
        if path and Path(path).exists():
            return path
    raise FileNotFoundError(
        "Brave Browser not found. Install it (https://brave.com/linux/ on a Pi) "
        "or pass --browser-path / set AMAZON_RETURNS_BROWSER.")


@contextlib.contextmanager
def profile_lock(session_dir, wait_s=600):
    """Only one run at a time may use the Brave profile.

    Two Braves on one profile makes the second launch hang until it times
    out, so a second run (e.g. the timer firing during a manual run) waits
    for the first to finish instead.
    """
    with open(session_dir / ".run.lock", "w") as fh:
        deadline = time.monotonic() + wait_s
        announced = False
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"Another run has been using the Brave profile for over "
                        f"{wait_s // 60} minutes. Is a --login window still open?")
                if not announced:
                    log("Another run is using the Brave profile; waiting for it to finish...")
                    announced = True
                time.sleep(2)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def scrape(args):
    from playwright.sync_api import sync_playwright

    args.session_dir.mkdir(exist_ok=True)
    os.chmod(args.session_dir, 0o700)
    debug_dir = args.session_dir / "debug"   # may contain personal data; not served

    # Always Brave, for --login and daily runs alike, so Amazon sees the same
    # browser every time and doesn't throw away the session.
    launch = dict(
        user_data_dir=str(args.session_dir),
        executable_path=find_brave(args.browser_path),
        headless=not args.headed,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        args=["--disable-blink-features=AutomationControlled"],
    )
    log(f"Using browser: {launch['executable_path']}")

    all_hits, seen_orders, pages = [], set(), 0
    with profile_lock(args.session_dir), sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(**launch)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(30_000)

            for page_no in range(args.max_pages):
                url = ORDERS_URL.format(filter=args.lookback, start=page_no * ORDERS_PER_PAGE)
                log(f"Loading orders page {page_no + 1} ({'visible' if args.headed else 'headless'})...")
                page.goto(url, wait_until="domcontentloaded")

                if is_signin_page(page):
                    if not args.login:
                        save_debug(page, debug_dir, "signin")
                        raise LoginRequired(
                            "Amazon session expired. Run with --login to sign in again.")
                    page = wait_for_login(ctx, args.login_timeout)
                    page.set_default_timeout(30_000)
                    page.goto(url, wait_until="domcontentloaded")

                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                # Scroll so lazy-loaded parts of the page render.
                for _ in range(4):
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(500)

                result = page.evaluate(EXTRACTION_JS)
                if not result["orderIds"] and is_bot_check(page.content()):
                    save_debug(page, debug_dir, "bot-check")
                    err = BotCheck(
                        "Amazon showed an \"I am human\" check instead of your orders, "
                        "usually after many visits in a short time. Keeping the last "
                        "good data; the next daily run will try again.")
                    # Pages read before the check are still good; main() keeps them.
                    err.hits, err.pages, err.orders = all_hits, pages, seen_orders
                    raise err
                pages += 1
                new_orders = set(result["orderIds"]) - seen_orders
                log(f"  {len(result['hits'])} returnable entries, {len(result['orderIds'])} orders on page")
                all_hits.extend(result["hits"])
                seen_orders |= set(result["orderIds"])

                if args.debug:
                    save_debug(page, debug_dir, f"page{page_no + 1}")
                # Stop at the end of the list: no new orders, or no "Next" button
                # on a page that isn't full.
                if not new_orders or (not result["hasNext"] and len(result["orderIds"]) < ORDERS_PER_PAGE):
                    break
                page.wait_for_timeout(1500)  # be polite between pages

            if pages == 1 and not seen_orders:
                save_debug(page, debug_dir, "no-orders")
                log("WARNING: no orders found on the page. Layout may have changed; "
                    f"see {debug_dir}")
        finally:
            ctx.close()

    return all_hits, pages


# -------- Main ------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Scrape Amazon return-by dates to JSON.")
    ap.add_argument("--login", action="store_true",
                    help="Open a visible browser so you can sign in (first run, or when the session expires).")
    ap.add_argument("--headed", action="store_true",
                    help="Show the browser window (implied by --login).")
    ap.add_argument("--threshold", type=int, default=int(os.environ.get("AMAZON_RETURNS_THRESHOLD", 2)),
                    help="Days left at or below which an item counts as urgent (default 2).")
    ap.add_argument("--lookback", default="months-3",
                    help="Amazon time filter: last30, months-3, year-2026, ... (default months-3).")
    ap.add_argument("--max-pages", type=int, default=5,
                    help="Maximum order pages to read, 10 orders each (default 5).")
    ap.add_argument("--browser-path", default=os.environ.get("AMAZON_RETURNS_BROWSER"),
                    help="Path to the Brave executable (default: found automatically).")
    ap.add_argument("--data-dir", type=Path, default=Path(os.environ.get("AMAZON_RETURNS_DATA_DIR", DEFAULT_DATA_DIR)),
                    help="Folder for returns.json (the only folder you should serve over HTTP).")
    ap.add_argument("--session-dir", type=Path, default=Path(os.environ.get("AMAZON_RETURNS_SESSION_DIR", DEFAULT_SESSION_DIR)),
                    help="Folder for the browser profile and cookies. Keep this private.")
    ap.add_argument("--login-timeout", type=int, default=600,
                    help="Seconds to wait for you to sign in (default 600).")
    ap.add_argument("--debug", action="store_true",
                    help="Save a screenshot and HTML of every page to <session-dir>/debug.")
    args = ap.parse_args(argv)
    args.headed = args.headed or args.login

    out_path = args.data_dir / "returns.json"
    previous = load_previous(out_path)

    try:
        hits, pages = scrape(args)
        items = build_items(hits)
        output = build_output("ok", items, args.threshold, pages=pages)
        code = 0
    except BotCheck as e:
        log(f"ERROR: {e}")
        if e.pages:
            # Fresh items from the pages that loaded, plus previous items from
            # orders on pages we didn't reach this time.
            today = date.today()
            fresh = build_items(e.hits)
            # Some items have no order ID, so also match on ASIN/title + date.
            fresh_keys = {(i["asin"] or i["title"], i["return_by"]) for i in fresh}
            kept = [dict(it, days_left=(date.fromisoformat(it["return_by"]) - today).days)
                    for it in (previous or {}).get("all_items", [])
                    if it.get("order_id") not in e.orders
                    and (it.get("asin") or it["title"], it["return_by"]) not in fresh_keys]
            items = sorted(fresh + kept, key=lambda r: (r["days_left"], r["title"]))
            output = build_output("ok", items, args.threshold, pages=e.pages)
            output["partial"] = True
            output["error"] = f"Partial: read {e.pages} page(s) before Amazon's \"I am human\" check."
            code = 0
        else:
            output = build_output("error", [], args.threshold, error=str(e), previous=previous)
            code = 3
    except LoginRequired as e:
        log(f"ERROR: {e}")
        output = build_output("login_required", [], args.threshold, error=str(e), previous=previous)
        code = 2
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        output = build_output("error", [], args.threshold, error=f"{type(e).__name__}: {e}", previous=previous)
        code = 1

    write_json_atomic(out_path, output)
    log(f"Wrote {out_path}: status={output['status']}, "
        f"{output['urgent_count']} urgent of {output['total_returnable']} returnable")
    for it in output["all_items"]:
        flag = "!" if it["days_left"] <= args.threshold else " "
        log(f" {flag} {it['days_left']:3d}d  {it['return_by']}  {it['title'][:70]}")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
