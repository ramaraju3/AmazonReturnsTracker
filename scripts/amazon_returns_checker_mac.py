"""
Amazon Returns Checker (Brave edition, v2)
==========================================
Finds items whose return window closes within DAYS_THRESHOLD days.

Strategy: instead of relying on Amazon's order-card CSS classes (which keep
changing), this version finds the literal text "Eligible through <DATE>" on
the page, then walks UP the DOM to find the nearest item title.

Setup:
    pip3 install playwright python-dateutil
    python3 -m playwright install chromium

Usage:
    python3 amazon_returns_checker.py
"""

import re
import sys
from datetime import datetime
from pathlib import Path

from dateutil import parser as dateparser
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# -------- Config ----------------------------------------------------------

BRAVE_PATH = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
ORDERS_URL = "https://www.amazon.com/gp/your-account/order-history"
SESSION_DIR = Path("./amazon_session").resolve()
DAYS_THRESHOLD = 30      # change to 2 for production; 30 for testing
LOOKBACK_FILTER = "months-3"


# Match either "Eligible through MAY 28, 2026" or just "through MAY 28, 2026"
# inside any larger return-related sentence.
RETURN_REGEX = re.compile(
    r"(?:eligible|returnable|return\s+window\s+closes?)\s*(?:through|until|by|on)?\s*"
    r"([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)


# -------- Helpers ---------------------------------------------------------

def parse_date_str(raw: str):
    try:
        return dateparser.parse(raw, fuzzy=True).date()
    except (ValueError, OverflowError):
        return None


def days_until(date_obj):
    return (date_obj - datetime.now().date()).days


# -------- Scraper ---------------------------------------------------------

# JS that finds every text node containing "Eligible through" (or similar),
# walks up to the nearest order/shipment/item container, and pulls a likely
# product title from it. Returns a list of {date_text, title, order_id}.
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

        // Climb ancestors to find the nearest container that *also* contains
        // a product link. Stop at <body>.
        let el = node.parentElement;
        let titleText = "";
        let orderId = "";
        let depth = 0;
        while (el && el !== document.body && depth < 12) {
            // Look for an item title link inside this ancestor.
            const titleEl = el.querySelector(
                "a[href*='/dp/'], a[href*='/gp/product/'], " +
                ".yohtmlc-product-title, .a-link-normal[href*='/product/']"
            );
            if (titleEl && titleEl.innerText.trim().length > 10) {
                titleText = titleEl.innerText.trim();
            }

            // Look for order id like "#123-4567890-1234567"
            const idMatch = el.innerText.match(/\b\d{3}-\d{7}-\d{7}\b/);
            if (idMatch) orderId = idMatch[0];

            if (titleText && orderId) break;
            el = el.parentElement;
            depth++;
        }

        hits.push({
            sentence: m[0],
            date_text: m[2],
            title: titleText || "(title not found nearby)",
            order_id: orderId,
        });
    }
    return hits;
}
"""


def run():
    SESSION_DIR.mkdir(exist_ok=True)
    if not Path(BRAVE_PATH).exists():
        print(f"ERROR: Brave not found at {BRAVE_PATH}")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=False,
            executable_path=BRAVE_PATH,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.new_page()
        print("Opening Amazon orders in Brave...")
        page.goto(ORDERS_URL, wait_until="domcontentloaded")

        if "signin" in page.url or "ap/signin" in page.url:
            print("\n>>> Please sign in. Script waits 5 minutes.\n")
            page.wait_for_url("**/order-history*", timeout=300_000)

        # Optional: narrow to last 3 months
        try:
            page.locator(
                "select#time-filter, select[name='orderFilter']"
            ).select_option(LOOKBACK_FILTER, timeout=3000)
            page.wait_for_load_state("domcontentloaded")
        except PWTimeout:
            pass

        # Scroll to render lazy content
        for _ in range(3):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(600)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)

        hits = page.evaluate(EXTRACTION_JS)

        # Parse dates and dedupe
        seen = set()
        items = []
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
                "return_by": d,
                "days_left": days_until(d),
                "order_id": h["order_id"],
            })

        urgent = [i for i in items if 0 <= i["days_left"] <= DAYS_THRESHOLD]
        urgent.sort(key=lambda r: r["days_left"])

        print("\n" + "=" * 70)
        print(f"Items with return window closing in {DAYS_THRESHOLD} days or fewer:")
        print("=" * 70)
        if not urgent:
            print("None found.")
            if items:
                print(f"\n(Found {len(items)} returnable items, but none within "
                      f"{DAYS_THRESHOLD} days. Latest:")
                for r in sorted(items, key=lambda x: x["days_left"])[:5]:
                    print(f"   {r['days_left']:3d}d - {r['return_by']} - "
                          f"{r['title'][:70]}")
                print(")")
        else:
            for r in urgent:
                badge = "TODAY" if r["days_left"] == 0 else f"{r['days_left']}d left"
                print(f"  [{badge}] return by {r['return_by']:%b %d, %Y}")
                print(f"          {r['title'][:90]}")
                if r["order_id"]:
                    print(f"          order #{r['order_id']}")
                print()

        print(f"(Scanned {len(items)} returnable items in total.)")
        input("\nPress Enter to close the browser...")
        browser.close()


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(0)
