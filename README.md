# Amazon Returns Tracker for Home Assistant

A small system that scrapes your Amazon order history daily and surfaces items whose return window is closing soon — as a Home Assistant dashboard tile and an iOS notification.

**Why this exists:** Amazon shows return-by dates only inside the orders page, not in any API or notification. It's easy to forget you have 30 days to return something until day 31. This project scrapes those dates once a day and warns you when something's about to expire.

## What it does

- Daily scrape (default 9 AM) of `amazon.com/gp/your-account/order-history` using a real headless Chromium via Playwright
- Writes results to `returns.json`: every returnable item with its title, return-by date, days remaining, and order ID
- Serves the JSON over HTTP on the local network
- Home Assistant pulls it via REST sensor every 5 minutes
- Lovelace card shows the full list; automation pings your phone when items enter the urgent window

## Architecture

```
+----------------------+        +------------------------+
|  Scraper Pi          |        |  Home Assistant Pi     |
|  (Raspberry Pi OS)   |        |  (HA OS / Container)   |
|                      |        |                        |
|  Playwright + headless        |  REST sensor polls     |
|  Chromium scrapes daily       |  http://scraper:8765   |
|  -> returns.json              |  -> Lovelace card      |
|  -> python -m http.server     |  -> 10:30 automation   |
|     on :8765                  |     -> iOS push        |
+----------------------+        +------------------------+
        ^                                  |
        +----------- LAN HTTP -------------+
```

You don't need two Pis — both pieces can run on the same host, or the scraper can run on a Mac/Linux box. The split is just convenient if you already have HA running on dedicated hardware.

## Requirements

**Scraper host (Mac, Linux, or Raspberry Pi):**
- Python 3.10+
- ~400 MB free for Playwright + Chromium
- Ability to run a graphical browser *once* for the initial Amazon login (the only time it needs a screen)

**Home Assistant:** any install type — OS, Container, Supervised, Core. Just needs `rest:` sensor support (built-in, no integration to install).

**iOS notifications:** the [Home Assistant Companion app](https://companion.home-assistant.io/) installed and signed in.

## Setup

### 1. Install on the scraper host

```bash
git clone https://github.com/YOUR_USERNAME/amazon-returns-ha.git
cd amazon-returns-ha

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

If you're on Linux/Pi, you'll also need shared libraries Chromium expects:

```bash
sudo apt install -y libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 \
    libcups2t64 libxkbcommon0 libatspi2.0-0t64 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2t64
```

### 2. First-run login

You must sign into Amazon once interactively. The session cookie is then reused for all future headless runs.

```bash
python scripts/amazon_returns_pi.py --login
```

A Chromium window opens. Log into Amazon. Once the orders page loads, the script auto-continues, scrapes, writes `returns.json`, and exits.

**On Wayland (Pi OS Trixie+):** if the browser fails to open with `Missing X server or $DISPLAY`, install `xwayland` and authorize the local user:

```bash
sudo apt install -y xwayland
xhost +SI:localuser:$(whoami)
```

Then retry. See [`docs/wayland-troubleshooting.md`](docs/wayland-troubleshooting.md) for the full story.

### 3. Verify headless mode works

This is what the daily timer will use:

```bash
python scripts/amazon_returns_pi.py
cat returns.json | python3 -m json.tool | head -20
```

You should see real order data. If `total_returnable` is 0 but you have returnable items, see [Troubleshooting](#troubleshooting).

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
```

From any other device on your LAN:
`http://SCRAPER_IP:8765/returns.json`

### 6. Home Assistant — REST sensor

Add to `configuration.yaml` (replace `SCRAPER_IP`):

```yaml
rest:
  - resource: "http://SCRAPER_IP:8765/returns.json"
    scan_interval: 300
    sensor:
      - name: "Amazon Returns Urgent"
        unique_id: amazon_returns_urgent
        value_template: "{{ value_json.urgent_count }}"
        json_attributes:
          - updated_at
          - urgent_count
          - total_returnable
          - urgent_items
          - all_items
      - name: "Amazon Returns Total"
        unique_id: amazon_returns_total
        value_template: "{{ value_json.total_returnable }}"
```

Reload via **Developer Tools → YAML → All YAML configuration**, then check **Developer Tools → States → `sensor.amazon_returns_urgent`**.

The full snippet is in [`homeassistant/configuration.yaml`](homeassistant/configuration.yaml).

### 7. Lovelace card

Add a Markdown card to any dashboard with the contents of [`homeassistant/lovelace-card.yaml`](homeassistant/lovelace-card.yaml).

### 8. Daily notification automation

Import [`homeassistant/automation.yaml`](homeassistant/automation.yaml). Replace `notify.mobile_app_YOUR_IPHONE` with your own device's notify service (find it in **Developer Tools → Actions**).

## Configuration

| Variable | Default | Where |
|---|---|---|
| Scrape time | 09:00 daily | `systemd/amazon-returns.timer` |
| Notification time | 10:30 daily | `homeassistant/automation.yaml` |
| Urgent threshold | 2 days | `--threshold` flag, default in script |
| HTTP port | 8765 | `systemd/amazon-returns-http.service` |
| HA poll interval | 5 minutes | `homeassistant/configuration.yaml` |

## Troubleshooting

### `total_returnable` is 0 but I have items

Most likely the Amazon session expired. Headless mode redirects to sign-in, fails to find orders, and writes empty results. Re-run with `--login` to refresh the session. Cookies typically last 30+ days.

### Chromium fails to open on Linux

See [`docs/wayland-troubleshooting.md`](docs/wayland-troubleshooting.md). Short version: install `xwayland`, run `xhost +SI:localuser:$(whoami)`.

### Selectors broke / wrong items extracted

Amazon changes their HTML occasionally. The script uses text-based extraction (looks for "Eligible through DATE" anywhere in the DOM), which is robust against most layout changes. If the regex needs updating, edit `EXTRACTION_JS` in `amazon_returns_pi.py`.

### Notification doesn't fire

1. Check **Settings → Automations → Amazon Returns → Traces** — if the condition (`urgent_count > 0`) failed, that's expected on days with nothing urgent.
2. Run the action manually via the automation's "Run Actions" button.
3. If you get `required key not provided @ data['message']`, your template is producing empty content — make sure you copied the full template with the `{% else %}` branch.

## Privacy and ToS

- Runs entirely on your local network. No data leaves your home.
- Amazon credentials live only as a cookie file on the scraper host.
- This scrapes pages you've already authenticated to view. Amazon's ToS technically prohibits automated scraping; in practice, low-volume personal use against your own account has been done by the community for years without issue. **Don't run this against accounts you don't own.**

## License

MIT. See [`LICENSE`](LICENSE).

## Contributing

Issues and PRs welcome. Particular areas where help is appreciated:
- HA blueprint version of the automation
- Selectors for non-US Amazon locales (the regex assumes English month names)
- A "mark as already returned" mechanism so the urgent list filters correctly
