# Amazon Returns Tracker for Home Assistant

A small system that scrapes your Amazon order history daily and surfaces items whose return window is closing soon — as a Home Assistant dashboard tile and an iOS notification.

**Why this exists:** Amazon shows return-by dates only inside the orders page, not in any API or notification. It's easy to forget you have 30 days to return something until day 31. This project scrapes those dates once a day and warns you when something's about to expire.

## What it does

- Daily scrape (default 9 AM) of your Amazon orders (all pages from the last 3 months) using Brave Browser (headless) driven by Playwright
- Writes `data/returns.json`: every returnable item with its title, return-by date, days remaining, order ID and order link, plus a `status` field (`ok`, `login_required` or `error`)
- Serves **only** the `data/` folder over HTTP on the local network
- Home Assistant pulls it via REST sensor every 5 minutes
- Lovelace card shows the full list; automations ping your phone when items enter the urgent window **and** when the Amazon login expires or the scraper stops working

## Architecture

```
+----------------------+        +------------------------+
|  Scraper Pi          |        |  Home Assistant Pi     |
|  (Raspberry Pi OS)   |        |  (HA OS / Container)   |
|                      |        |                        |
|  Playwright + headless Brave  |  REST sensor polls     |
|  scrapes daily                |  http://scraper:8765   |
|  -> data/returns.json         |  -> Lovelace card      |
|  -> http.server serving       |  -> 10:30 automation   |
|     data/ on :8765            |     -> iOS push        |
+----------------------+        +------------------------+
        ^                                  |
        +----------- LAN HTTP -------------+
```

You don't need two Pis — both pieces can run on the same host, or the scraper can run on a Mac/Linux box. The split is just convenient if you already have HA running on dedicated hardware.

## Requirements

**Scraper host (Mac, Linux, or Raspberry Pi):**
- Python 3.10+
- Brave Browser installed (see Setup step 1)
- Ability to run a graphical browser *once* for the initial Amazon login (the only time it needs a screen)

**Home Assistant:** any install type — OS, Container, Supervised, Core. Just needs `rest:` sensor support (built-in, no integration to install).

**iOS notifications:** the [Home Assistant Companion app](https://companion.home-assistant.io/) installed and signed in.

## Setup

### 1. Install on the scraper host

```bash
git clone https://github.com/ramaraju3/AmazonReturnsTracker.git amazon-returns
cd amazon-returns

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

The scraper always uses **Brave Browser**, for the login and the daily runs. You don't need `playwright install`.

- **Mac:** install Brave from [brave.com](https://brave.com/download/). It's found automatically in `/Applications`.
- **Raspberry Pi / Linux:** install Brave from its apt repository:

```bash
sudo curl -fsSLo /usr/share/keyrings/brave-browser-archive-keyring.gpg \
    https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/brave-browser-archive-keyring.gpg] https://brave-browser-apt-release.s3.brave.com/ stable main" \
    | sudo tee /etc/apt/sources.list.d/brave-browser-release.list
sudo apt update && sudo apt install -y brave-browser
```

If Brave is somewhere else, pass `--browser-path` or set `AMAZON_RETURNS_BROWSER`.

### 2. First-run login

You must sign into Amazon once interactively. The session cookie is then reused for all future headless runs.

```bash
python scripts/amazon_returns.py --login
```

A Brave window opens. Log into Amazon (including any 2-step verification code). Once your orders page loads, the script continues by itself, scrapes, writes `data/returns.json`, and exits. You have 10 minutes to sign in (`--login-timeout` to change).

Tip: tick **Keep me signed in** on the Amazon sign-in page so the session lasts longer.

**On a Pi without a monitor:** run `--login` from the Pi's desktop (Pi Connect / VNC). Don't copy `amazon_session/` over from a Mac: Brave on macOS encrypts cookies with the Mac Keychain, so a Linux Brave can't read them and you'd have to sign in again anyway.

**On Wayland (Pi OS Trixie+):** if the browser fails to open with `Missing X server or $DISPLAY`, install `xwayland` and authorize the local user:

```bash
sudo apt install -y xwayland
xhost +SI:localuser:$(whoami)
```

Then retry. See [`docs/wayland-troubleshooting.md`](docs/wayland-troubleshooting.md) for the full story.

### 3. Verify headless mode works

This is what the daily timer will use:

```bash
python scripts/amazon_returns.py
head -20 data/returns.json
```

You should see `"status": "ok"` and your order data. If `total_returnable` is 0 but you have returnable items, see [Troubleshooting](#troubleshooting).

### 4. Daily scheduling (systemd)

Copy the unit files, edit paths to match your install, and enable:

```bash
sudo cp systemd/amazon-returns.service /etc/systemd/system/
sudo cp systemd/amazon-returns.timer /etc/systemd/system/
sudo nano /etc/systemd/system/amazon-returns.service   # edit User= and paths
sudo systemctl daemon-reload
sudo systemctl enable --now amazon-returns.timer
systemctl list-timers amazon-returns.timer             # confirm "NEXT" populated
```

### 5. Serve the JSON over HTTP

```bash
sudo cp systemd/amazon-returns-http.service /etc/systemd/system/
sudo nano /etc/systemd/system/amazon-returns-http.service   # edit User= and paths
sudo systemctl daemon-reload
sudo systemctl enable --now amazon-returns-http.service
curl http://localhost:8765/returns.json | head -5            # verify
curl -I http://localhost:8765/amazon_session/                # must be 404
```

The server only shares the `data/` folder. **Never point it at the project folder**: `amazon_session/` holds your Amazon login cookies.

From any other device on your LAN:
`http://SCRAPER_IP:8765/returns.json`

### 6. Home Assistant — REST sensor

Copy [`homeassistant/configuration.yaml`](homeassistant/configuration.yaml) into your `configuration.yaml` and replace `SCRAPER_IP`. It creates three sensors:

| Sensor | State | Useful attributes |
|---|---|---|
| `sensor.amazon_returns_urgent` | number of urgent items | `urgent_items`, `all_items`, `next_deadline` |
| `sensor.amazon_returns_total` | number of returnable items | |
| `sensor.amazon_returns_status` | `ok` / `login_required` / `error` | `error`, `last_success`, `stale` |

Reload via **Developer Tools → YAML → All YAML configuration**, then check **Developer Tools → States → `sensor.amazon_returns_status`** (should be `ok`).

### 7. Lovelace card

Add a Markdown card to any dashboard with the contents of [`homeassistant/lovelace-card.yaml`](homeassistant/lovelace-card.yaml).

### 8. Daily notification automation

Import [`homeassistant/automation.yaml`](homeassistant/automation.yaml). It has two automations:

- **Daily check** (10:30): lists items due within the urgent window.
- **Scraper needs attention**: tells you when the Amazon login has expired, the scraper errored, the HTTP server is unreachable, or there's been no successful scrape for 26 hours.

Replace `notify.mobile_app_YOUR_IPHONE` with your own device's notify action (find it in **Developer Tools → Actions**).

## Running on a Mac

The same script works on macOS. For testing:

```bash
python scripts/amazon_returns.py --login       # first time
python scripts/amazon_returns.py               # headless
python scripts/amazon_returns.py --headed      # watch it work
```

Brave is used for everything, including `--login`, so no other browser should open.

## Upgrading from the old scripts

`amazon_returns_pi.py` and `amazon_returns_checker_mac.py` are replaced by `scripts/amazon_returns.py`.

1. `git pull`, then copy the two systemd files again (the paths changed) and `sudo systemctl daemon-reload && sudo systemctl restart amazon-returns-http`.
2. Install Brave (see step 1), delete the old session (`rm -rf scripts/amazon_session`), and run `python scripts/amazon_returns.py --login` once. The old session came from a different browser, so it won't carry over.
3. Delete the old `scripts/returns.json`.
4. Update the Home Assistant YAML from `homeassistant/` (new status sensor, automation and card).

## Configuration

| Setting | Default | Where |
|---|---|---|
| Scrape time | 09:00 daily | `systemd/amazon-returns.timer` |
| Notification time | 10:30 daily | `homeassistant/automation.yaml` |
| Urgent threshold | 2 days | `--threshold` or `AMAZON_RETURNS_THRESHOLD` |
| How far back | last 3 months | `--lookback` (`last30`, `months-3`, `year-2026`) |
| Max order pages | 5 (50 orders) | `--max-pages` |
| Browser | Brave (auto-detected) | `--browser-path` or `AMAZON_RETURNS_BROWSER` |
| Output folder | `data/` | `--data-dir` or `AMAZON_RETURNS_DATA_DIR` |
| Session folder | `amazon_session/` | `--session-dir` or `AMAZON_RETURNS_SESSION_DIR` |
| HTTP port | 8765 | `systemd/amazon-returns-http.service` |
| HA poll interval | 5 minutes | `homeassistant/configuration.yaml` |

Exit codes: `0` ok, `2` login required, `1` other error.

## Troubleshooting

### Status is `login_required`

The Amazon session expired. Run `python scripts/amazon_returns.py --login` on the scraper host. Until you do, the JSON keeps the last known items (marked `"stale": true`) so the dashboard still shows something. A screenshot of what the scraper saw is saved to `amazon_session/debug/signin.png`.

### `--login` never continues after I sign in

Make sure you end up on your orders list (Account → Your Orders). The script continues as soon as the URL is `/your-orders/orders` or `/gp/your-account/order-history`.

### Home Assistant sensors are `unavailable`

Run `curl http://SCRAPER_IP:8765/returns.json` from another machine. A 404 means the HTTP server is serving the wrong folder: it must serve `data/`, not the project folder or `scripts/`.

### `total_returnable` is 0 but I have items

Run `python scripts/amazon_returns.py --headed --debug`. Each page is saved as a screenshot and HTML in `amazon_session/debug/`. If you see your orders but nothing was picked up, Amazon changed its wording; see the next section.

### Brave fails to open on Linux

See [`docs/wayland-troubleshooting.md`](docs/wayland-troubleshooting.md). Short version: install `xwayland`, run `xhost +SI:localuser:$(whoami)`.

### Selectors broke / wrong items extracted

Amazon changes their HTML occasionally. The script uses text-based extraction (looks for "Eligible through DATE" anywhere in the DOM), which is robust against most layout changes. If the regex needs updating, edit `EXTRACTION_JS` in `scripts/amazon_returns.py`, add the new wording to `tests/fixtures/orders_page.html`, and run the tests:

```bash
pip install pytest && python -m pytest tests
```

### Notification doesn't fire

1. Check **Settings → Automations → Amazon Returns → Traces** — if the condition (`urgent_count > 0`) failed, that's expected on days with nothing urgent.
2. Run the action manually via the automation's "Run Actions" button.
3. If the sensor is `unavailable`, see [Home Assistant sensors are `unavailable`](#home-assistant-sensors-are-unavailable).

## Privacy and ToS

- Runs entirely on your local network. No data leaves your home.
- Amazon credentials live only as a cookie file in `amazon_session/` on the scraper host (folder permissions `700`, git-ignored, never served over HTTP).
- This scrapes pages you've already authenticated to view. Amazon's ToS technically prohibits automated scraping; in practice, low-volume personal use against your own account has been done by the community for years without issue. **Don't run this against accounts you don't own.**

## License

MIT. See [`LICENSE`](LICENSE).

## Contributing

Issues and PRs welcome. Particular areas where help is appreciated:
- HA blueprint version of the automation
- Selectors for non-US Amazon locales (the regex assumes English month names)
- A "mark as already returned" mechanism so the urgent list filters correctly
- Issues responded to as time permits
