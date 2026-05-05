# Architecture notes

## Why scrape instead of using an API?

Amazon does not expose return-window dates through any of its public or partner APIs. The `selling-partner-api` returns shipment data but not customer-facing return eligibility. The only place this information appears is the rendered HTML of the orders page.

## Why text-based extraction?

Earlier versions of this script targeted CSS selectors like `.order-card` and `.yohtmlc-product-title`. These break every few months when Amazon changes their layout.

The current approach (`EXTRACTION_JS` in `amazon_returns_pi.py`) walks the DOM's text nodes, regex-matches lines containing "Eligible through DATE", and then climbs the parent chain to find the nearest item title and order ID. This survives most layout changes — the only thing that would break it is Amazon removing the words "Eligible through" entirely.

## Why headless Chromium and not `requests`?

Amazon's orders page is heavily JavaScript-rendered. A plain HTTP request returns mostly empty `<div>`s waiting for React to populate them. Playwright drives a real browser engine, lets the page fully render, and then reads the resulting DOM.

## Session handling

Playwright's `launch_persistent_context` keeps cookies, localStorage, and IndexedDB in `amazon_session/`. The first `--login` run populates this; every subsequent headless run reads from it.

Sessions typically last 30+ days. They can expire earlier if:
- You sign out from another device
- Amazon detects "suspicious activity" (changed IP, etc.)
- The cookie's TTL elapses

When they expire, the headless run hits a sign-in redirect, fails to find the orders page, and writes JSON with `total_returnable: 0`. Re-run with `--login` to refresh.

## Why a separate HTTP server instead of HA reading the file directly?

If HA and the scraper run on different machines (the typical case), HA needs network access to the JSON. Options considered:

| Approach | Verdict |
|---|---|
| HA reads via SSH (`command_line` sensor) | Works, but HA OS makes SSH key permissions awkward |
| MQTT broker | Heavyweight for serving one file |
| Shared NFS/Samba mount | Filesystem coupling between hosts |
| **HTTP file server** | Simplest, no auth needed on trusted LAN, works with HA's built-in `rest:` sensor |

The `python -m http.server` approach has zero dependencies and is one line of systemd config.

## Security model

This system is built on the assumption that your home network is trusted. The HTTP server has no authentication. Anyone on the LAN who knows the URL can read your returnable items list. If your network includes untrusted devices (rented IoT cameras, guest networks bridged to main, etc.), put the scraper on a more isolated subnet or add basic auth via a tiny nginx reverse proxy.

The Amazon credentials live only in the `amazon_session/` cookie cache — never in plaintext, never in the JSON, never sent over the network.

## Why systemd and not cron?

Three reasons:
1. `Persistent=true` — if the Pi was off at scheduled time, runs as soon as it boots.
2. `RandomizedDelaySec` — avoids the bot-fingerprint of running at exactly :00:00 every day.
3. `journalctl` integration — better logging and observability than cron's mail-the-user model.

## Limitations

- **US-only by default.** The regex matches English month names. Localizing for other Amazon storefronts requires updating the date pattern in `EXTRACTION_JS`.
- **No "already returned" tracking.** If you return an item, the script keeps reporting it until Amazon's "Eligible through" text is removed (usually within a day of the return being scanned).
- **No multi-account support.** One scraper instance = one Amazon account.
