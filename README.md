# USA Car Search

A Python scraper that searches multiple US car listing sites for used vehicles matching your criteria and sends a daily Telegram summary with new listings.

Works for any make and model. Built for reliability across sites with different bot-detection levels.

> **US only.** Distance filtering uses US ZIP codes via the `pgeocode` library, and all supported listing sites are US-based. Canadian listings will not be found or filtered correctly.

**Sources:**
| Source | Method | Notes |
|---|---|---|
| CarGurus | Browser scrape | Paste your search URL |
| Cars.com | Browser scrape | Paste your search URL |
| Craigslist | Browser scrape | Multi-region, keyword search |
| AutoTrader | Browser scrape | Chrome CDP recommended |
| Facebook Marketplace | Browser scrape | Requires saved session |
| eBay Motors | API + scrape | API recommended |
| auto.dev | API | Free key |

---

## Prerequisites

- Python 3.9+
- WSL2 (Windows) or Linux with a display — the scraper runs in headed (non-headless) mode to reduce bot detection

---

## Installation

```bash
pip install playwright pgeocode playwright-stealth
playwright install chromium
```

Optional — load config from a `.env` file:
```bash
pip install python-dotenv
```

---

## Quick Start

**1. Copy and fill in your config:**
```bash
cp .env.example .env
# Edit .env with your vehicle, ZIP, Telegram bot, etc.
```

**2. Uncomment the dotenv loader** at the top of `usa-car-search.py`:
```python
from dotenv import load_dotenv
load_dotenv()
```

**3. Run it:**
```bash
python3 usa-car-search.py           # print results to stdout
python3 usa-car-search.py --notify  # also send Telegram alert
python3 usa-car-search.py --all     # treat all listings as new (good for first run)
```

---

## Configuration

All settings are environment variables. Set them in `.env` or export them in your shell.

### Vehicle

```env
SEARCH_MAKE=Honda
SEARCH_MODEL=Civic
SEARCH_KEYWORDS=honda civic    # used for free-text search (CL, FB, eBay)
```

### Location & filters

```env
SEARCH_ZIP=90210
SEARCH_RADIUS=100      # miles
MIN_YEAR=2020
MAX_YEAR=2023
MAX_MILES=50000
MIN_PRICE=15000
MAX_PRICE=35000
```

### Colors

Edit `color_matches_str()` in `usa-car-search.py` to change which colors are included. Default: black, grey/gray, charcoal, and similar dark tones.

```python
# Match any color:
return True

# Match specific colors:
return any(kw in c for kw in ["white", "silver", "black"])
```

### Trims

Edit `trim_matches()` to restrict results to specific trims:

```python
# Only STI and Limited:
return any(t in trim_name.lower() for t in ["sti", "limited"])
```

---

## Source Setup

### Craigslist
Set `CL_REGIONS` to a comma-separated list of subdomain slugs. Find them at [craigslist.org/about/sites](https://www.craigslist.org/about/sites).

```env
CL_REGIONS=losangeles,sandiego,orangecounty
```

### CarGurus, AutoTrader, Cars.com

These sites use internal make/model codes that vary by vehicle. The easiest approach:

1. Go to the site
2. Search using the site's own filters (make, model, year range, mileage, color, ZIP, radius)
3. Copy the URL from your browser
4. Paste it into your `.env`

```env
CARGURUS_URL=https://www.cargurus.com/Cars/l-Used-Honda-Civic-d2188?zip=90210&distance=100&...
AUTOTRADER_URL=https://www.autotrader.com/cars-for-sale/used-cars/honda/civic/?zip=90210&...
CARSDOTCOM_URL=https://www.cars.com/shopping/results/?makes[]=honda&models[]=honda-civic&...
```

You can also set `CARGURUS_URL_2` for a second CarGurus search (e.g. a different trim or variant).

### Facebook Marketplace

Facebook requires a real logged-in session. Run the auth setup script once:

```bash
python3 fb-auth-setup.py
```

A browser window opens — log into Facebook, then press Enter. Your session is saved to `fb-session.json`. Re-run when it expires (typically every few weeks).

Set `FB_CITY` to the city slug from the Facebook Marketplace URL:
```env
FB_CITY=losangeles   # → facebook.com/marketplace/losangeles/search
```

### eBay Motors (Browse API — recommended)

1. Sign up at [developer.ebay.com](https://developer.ebay.com)
2. Create a production app
3. Go to **User Tokens** → generate a User OAuth Token (expires ~18 months)
4. Save the token to `ebay-token.txt`

The scraper falls back to page scraping if no token is found.

### auto.dev

Get a free API key at [auto.dev](https://auto.dev). Set `AUTODEV_API_KEY`.

---

## AutoTrader: Chrome CDP Setup (recommended)

AutoTrader aggressively blocks headless browsers. Connecting to a real Chrome instance via CDP bypasses this.

**Windows/WSL2 setup:**

1. Create `launch-chrome-debug.bat`:
```batch
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --remote-debugging-address=0.0.0.0 ^
  --remote-allow-origins=* ^
  --user-data-dir="C:\Temp\chrome-debug" ^
  --no-first-run ^
  about:blank
```

2. Run this before the scraper (or add to Task Scheduler to run at login).

3. Find your Windows IP from WSL2:
```bash
ip route show default | awk '{print $3}'
```

4. Set it in `.env`:
```env
CHROME_CDP_HOST=172.x.x.x
CHROME_CDP_PORT=9222
```

5. If Chrome doesn't respond from WSL2, add a port forwarding rule (run as admin in PowerShell):
```powershell
$wsl = (wsl hostname -I).Trim()
netsh interface portproxy add v4tov4 listenport=9222 listenaddress=0.0.0.0 connectport=9222 connectaddress=$wsl
```

If Chrome CDP is not configured, the scraper falls back to Playwright's Chromium automatically.

---

## Telegram Notifications

1. Create a bot with [@BotFather](https://t.me/BotFather), copy the token
2. Add the bot to your chat or channel
3. Get the chat ID (for groups/channels: use [@userinfobot](https://t.me/userinfobot) or the Telegram API)
4. Set in `.env`:
```env
TG_BOT_TOKEN=123456:ABC-...
TG_CHAT_ID=-1001234567890
TG_TOPIC_ID=42   # optional: forum thread ID
```

Run with `--notify`:
```bash
python3 usa-car-search.py --notify
```

---

## Daily Automated Run

Add to crontab (Linux/WSL2):
```bash
crontab -e
```
```cron
0 17 * * * cd /path/to/car-search && python3 usa-car-search.py --notify >> search.log 2>&1
```

---

## How It Works

- Results are saved to `seen.json` between runs
- New listings get flagged `[NEW]` in stdout and `🆕` in Telegram
- Listings that disappear (sold/removed) are counted and reported
- Deduplication happens across sources using VIN (when available) or a year/mileage/price fingerprint
- Distance is computed as the haversine distance from `SEARCH_ZIP` using the `pgeocode` library

---

## Adapt for Any Vehicle

1. Set `SEARCH_MAKE`, `SEARCH_MODEL`, and `SEARCH_KEYWORDS`
2. Get your search URLs from CarGurus, AutoTrader, and Cars.com and paste them in
3. Set Craigslist regions near you
4. Optionally adjust `color_matches_str()` and `trim_matches()`
