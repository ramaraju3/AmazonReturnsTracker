"""
Amazon Returns Checker - Raspberry Pi edition
==============================================
Headless Chromium scraper that writes results to JSON for Home Assistant.

First run:  python3 amazon_returns_pi.py --login
            (opens a visible browser via Pi Connect desktop; sign in to Amazon)

Daily run:  python3 amazon_returns_pi.py
            (headless; reads saved session, writes returns.json)
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from dateutil import parser as dateparser
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# -------- Config ----------------------------------------------------------

ORDERS_URL = "https://www.amazon.com/gp/your-account/order-history"
SCRIPT_DIR = Path(__file__).resolve().parent
SESSION_DIR = SCRIPT_DIR / "amazon_session"
OUTPUT_JSON = SCRIPT_DIR / "returns.json"
LOOKBACK_FILTER = "months-3"


EXTRACTION_JS = r"""
() => {
    const RE = /(eligible\s+(?:for\s+return\s+)?through|returnable\s+through|return\s+window\s+closes?(?:\s+on)?)\s+([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4})/i;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const hits = [];
    let node;
    while ((node = walker.nextNode())) {
        const txt = node.nodeValue;
        if (!txt) continue;
        const m = txt.match(RE);
        if (!m) continue;

        let el = node.parentElement;
        let titleText = "", orderId = "", depth = 0;
        while (el && el !== document.body && depth < 12) {
            const titleEl = el.querySelector(
                "a[href*='/dp/'], a[href*='/gp/product/'], " +
                ".yohtmlc-product-title, .a-link-normal[href*='/product/']"
            );
            if (titleEl && titleEl.innerText.trim().length > 10) {
                titleText = titleEl.innerText.trim();
            }
            const idMatch = el.innerText.match(/\b\d{3}-\d{7}-\d{7}\b/);
            if (idMatch) orderId = idMatch[0];
            if (titleText && orderId) break;
            el = el.parentElement;
            depth++;
        }
        hits.push({
            date_text: m[2],
            title: titleText || "(title not found)",
            order_id: orderId,
        });
    }
    return hits;
}
"""


def parse_date_str(raw):
    try:
        return dateparser.parse(raw, fuzzy=True).date()
    except (ValueError, OverflowError):
        return None


def days_until(d):
    return (d - datetime.now().date()).days


def scrape(login_mode=False):
    SESSION_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=not login_mode,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.new_page()
        print(f"Opening Amazon orders ({'visible' if login_mode else 'headless'})...",
              file=sys.stderr)
        page.goto(ORDERS_URL, wait_until="domcontentloaded")

        if "signin" in page.url or "ap/signin" in page.url:
            if not login_mode:
                print("ERROR: redirected to sign-in but running headless. "
                      "Re-run with --login first.", file=sys.stderr)
                browser.close()
                return None
            print("\n>>> Sign in to Amazon in the browser window.", file=sys.stderr)
            print(">>> Script waits 5 minutes.\n", file=sys.stderr)
            page.wait_for_url("**/order-history*", timeout=300_000)

        try:
            page.locator(
                "select#time-filter, select[name='orderFilter']"
            ).select_option(LOOKBACK_FILTER, timeout=3000)
            page.wait_for_load_state("domcontentloaded")
        except PWTimeout:
            pass

        for _ in range(4):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(600)
        page.wait_for_timeout(800)

        hits = page.evaluate(EXTRACTION_JS)
        browser.close()

    seen, items = set(), []
    for h in hits:
        d = parse_date_str(h["date_text"])
        if not d:
            continue
        key = (h["title"], d)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "title": h["title"],
            "return_by": d.isoformat(),
            "days_left": days_until(d),
            "order_id": h["order_id"],
        })

    items.sort(key=lambda r: r["days_left"])
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true",
                    help="Run with visible browser to sign in.")
    ap.add_argument("--threshold", type=int, default=2,
                    help="Days threshold for 'urgent' flag (default 2).")
    args = ap.parse_args()

    items = scrape(login_mode=args.login)
    if items is None:
        sys.exit(1)

    urgent = [i for i in items if 0 <= i["days_left"] <= args.threshold]

    output = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "threshold_days": args.threshold,
        "urgent_count": len(urgent),
        "total_returnable": len(items),
        "urgent_items": urgent,
        "all_items": items,
    }

    OUTPUT_JSON.write_text(json.dumps(output, indent=2))
    print(f"Wrote {OUTPUT_JSON}: {len(urgent)} urgent of {len(items)} returnable",
          file=sys.stderr)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
