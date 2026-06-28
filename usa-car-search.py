#!/usr/bin/env python3
"""
USA Car Search — multi-source US used car scraper with Telegram alerts.

Scrapes CarGurus, Cars.com, Craigslist, AutoTrader, Facebook Marketplace,
eBay Motors, and auto.dev. Filters by year, mileage, color, and distance.
Sends a Telegram message summarizing new listings.

US only — distance filtering uses US ZIP codes and all sources are US-based.

Usage:
    python3 usa-car-search.py           # print results to stdout
    python3 usa-car-search.py --notify  # also send Telegram alert
    python3 usa-car-search.py --all     # treat all listings as new

Setup:
    Copy .env.example to .env and fill in your values, OR export env vars.
    See README.md for full setup instructions.
"""

import json, re, sys, os, argparse, urllib.request, urllib.parse, math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ── Load .env if present ──────────────────────────────────────────────────────
# Install `python-dotenv` and uncomment to auto-load a .env file:
# from dotenv import load_dotenv
# load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION — edit these or set environment variables
# ═══════════════════════════════════════════════════════════════════════════════

# -- Vehicle to search for --
SEARCH_MAKE     = os.environ.get("SEARCH_MAKE",     "Subaru")
SEARCH_MODEL    = os.environ.get("SEARCH_MODEL",    "WRX")
# Keywords used for free-text search sites (Craigslist, Facebook, eBay)
SEARCH_KEYWORDS = os.environ.get("SEARCH_KEYWORDS", "subaru wrx")

# -- Search location --
ZIP    = os.environ.get("SEARCH_ZIP",    "10001")
RADIUS = int(os.environ.get("SEARCH_RADIUS", "150"))

# -- Vehicle filters --
MIN_YEAR  = int(os.environ.get("MIN_YEAR",  "2019"))
MAX_YEAR  = int(os.environ.get("MAX_YEAR",  "2021"))
MAX_MILES = int(os.environ.get("MAX_MILES", "65000"))

# -- Price range (used by Craigslist, Facebook, eBay) --
MIN_PRICE = int(os.environ.get("MIN_PRICE", "15000"))
MAX_PRICE = int(os.environ.get("MAX_PRICE", "40000"))

# -- Craigslist --
# Subdomain slugs from https://www.craigslist.org/about/sites
CL_REGIONS = os.environ.get("CL_REGIONS", "newyork,boston,philadelphia").split(",")

# -- Source search URLs (CarGurus, AutoTrader, Cars.com) --
# These sites use internal make/model codes that can't be guessed.
# Steps: go to the site, set all your filters (make, model, year, mileage,
# color, location, radius), then copy the URL from your browser and paste here.
# Leave blank to skip that source.
CARGURUS_URL    = os.environ.get("CARGURUS_URL",    "")
CARGURUS_URL_2  = os.environ.get("CARGURUS_URL_2",  "")   # optional second search (e.g. a trim variant)
AUTOTRADER_URL  = os.environ.get("AUTOTRADER_URL",  "")
CARSDOTCOM_URL  = os.environ.get("CARSDOTCOM_URL",  "")

# -- Facebook Marketplace --
# City slug from the FB Marketplace URL: facebook.com/marketplace/<city>/search
FB_CITY         = os.environ.get("FB_CITY", "newyork")
FB_SESSION_FILE = os.environ.get("FB_SESSION_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fb-session.json"))

# -- Telegram notifications --
TG_TOKEN    = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID  = os.environ.get("TG_CHAT_ID",  "")
TG_TOPIC_ID = os.environ.get("TG_TOPIC_ID", "")  # Optional: forum/topic thread ID

# -- API keys --
AD_API_KEY      = os.environ.get("AUTODEV_API_KEY", "")      # free at https://auto.dev
EBAY_TOKEN_FILE         = os.environ.get("EBAY_TOKEN_FILE",         os.path.join(os.path.dirname(os.path.abspath(__file__)), "ebay-token.txt"))
EBAY_REFRESH_TOKEN_FILE = os.environ.get("EBAY_REFRESH_TOKEN_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ebay-refresh-token.txt"))
EBAY_CLIENT_ID          = os.environ.get("EBAY_CLIENT_ID",          "")
EBAY_CLIENT_SECRET      = os.environ.get("EBAY_CLIENT_SECRET",      "")
EBAY_RUNAME             = os.environ.get("EBAY_RUNAME",             "")

# -- AutoTrader Chrome CDP (bypasses bot detection) --
# Set CHROME_CDP_HOST to your Windows gateway IP when running from WSL2.
# Run: ip route show default | awk '{print $3}'
# See README.md for full setup.
CHROME_CDP_HOST = os.environ.get("CHROME_CDP_HOST", "")
CHROME_CDP_PORT = int(os.environ.get("CHROME_CDP_PORT", "9222"))

# -- Enable/disable individual sources --
ENABLE_CARGURUS   = os.environ.get("ENABLE_CARGURUS",   "true").lower() == "true"
ENABLE_CRAIGSLIST = os.environ.get("ENABLE_CRAIGSLIST", "true").lower() == "true"
ENABLE_CARSDOTCOM = os.environ.get("ENABLE_CARSDOTCOM", "true").lower() == "true"
ENABLE_AUTOTRADER = os.environ.get("ENABLE_AUTOTRADER", "true").lower() == "true"
ENABLE_FACEBOOK   = os.environ.get("ENABLE_FACEBOOK",   "true").lower() == "true"
ENABLE_EBAY       = os.environ.get("ENABLE_EBAY",       "true").lower() == "true"
ENABLE_AUTODEV    = os.environ.get("ENABLE_AUTODEV",    "true").lower() == "true"

# ═══════════════════════════════════════════════════════════════════════════════
#  END CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")

# Derived search constants
_MAKE_KW    = SEARCH_MAKE.lower()
_MODEL_KW   = SEARCH_MODEL.lower()
_KW_PARTS   = [k.lower() for k in SEARCH_KEYWORDS.split()]
_CL_QUERY   = SEARCH_KEYWORDS.replace(" ", "+")
_FB_QUERY   = SEARCH_KEYWORDS.replace(" ", "+")

# Dynamic year regex covering MIN_YEAR..MAX_YEAR
_year_alts  = "|".join(str(y) for y in range(MIN_YEAR, MAX_YEAR + 1))
YEAR_RE     = re.compile(rf'\b({_year_alts})\b')

# Facebook search URLs built from config
FB_SEARCH_URL = (
    f"https://www.facebook.com/marketplace/{FB_CITY}/search/"
    f"?query={_FB_QUERY}&categoryID=vehicles"
    f"&minPrice={MIN_PRICE}&maxPrice={MAX_PRICE}"
    "&radius=321"
)

# eBay Motors search URL
EBAY_SEARCH_URL = (
    "https://www.ebay.com/sch/Cars-Trucks/6001/i.html"
    f"?_nkw={urllib.parse.quote(SEARCH_KEYWORDS)}"
    "&_fsrp=1&rt=nc"
)


def _has_model_kw(text):
    """Return True if text contains the search model keyword."""
    t = text.lower()
    return all(k in t for k in _KW_PARTS) or _MODEL_KW in t


# ── Geo helpers ───────────────────────────────────────────────────────────────

def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

try:
    import pgeocode as _pgeocode
    _nomi = _pgeocode.Nominatim('us')
    def zip_to_latlon(zipcode):
        try:
            r = _nomi.query_postal_code(str(zipcode).strip().zfill(5))
            if r is not None and not (r.latitude != r.latitude):
                return float(r.latitude), float(r.longitude)
        except Exception:
            pass
        return None, None
    def zip_to_city_state(zipcode):
        try:
            r = _nomi.query_postal_code(str(zipcode).strip().zfill(5))
            if r is not None:
                city = str(r.place_name) if r.place_name == r.place_name else ""
                state = str(r.state_code) if r.state_code == r.state_code else ""
                if city and state:
                    return city, state
        except Exception:
            pass
        return None, None
    def city_to_state_and_distance(city_name):
        try:
            df = _nomi._data
            matches = df[df['place_name'].str.lower() == city_name.lower()]
            best = None
            for _, row in matches.iterrows():
                if row.latitude != row.latitude or row.longitude != row.longitude:
                    continue
                d = haversine_miles(ORIGIN_LAT, ORIGIN_LON, float(row.latitude), float(row.longitude))
                if d is not None and d <= RADIUS:
                    if best is None or d < best[1]:
                        best = (str(row.state_code), int(d))
            return best
        except Exception:
            return None
    HAS_PGEOCODE = True
except ImportError:
    HAS_PGEOCODE = False
    def zip_to_latlon(zipcode):
        return None, None
    def zip_to_city_state(zipcode):
        return None, None
    def city_to_state_and_distance(city_name):
        return None

# Derive origin lat/lon from SEARCH_ZIP at startup
_origin = zip_to_latlon(ZIP)
ORIGIN_LAT = _origin[0] if _origin[0] is not None else 40.7128
ORIGIN_LON = _origin[1] if _origin[1] is not None else -74.0060

def zip_distance_miles(zipcode):
    lat, lon = zip_to_latlon(zipcode)
    if lat is None:
        return None
    return haversine_miles(ORIGIN_LAT, ORIGIN_LON, lat, lon)


# ── VIN decoder ───────────────────────────────────────────────────────────────

def nhtsa_decode_vin(vin):
    if not vin or len(vin) != 17:
        return {}
    try:
        url = f"https://vpic.nhtsa.dot.gov/api/vehicles/decodevinvalues/{vin}?format=json"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = urllib.request.urlopen(req, timeout=8)
        data = json.loads(resp.read())
        results = data.get("Results", [{}])[0]
        return {
            "make": results.get("Make", ""),
            "model": results.get("Model", ""),
            "year": results.get("ModelYear", ""),
            "trim": results.get("Trim", ""),
        }
    except Exception:
        return {}


from playwright.sync_api import sync_playwright
try:
    from playwright_stealth import stealth_sync
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False


# ── Color filter ──────────────────────────────────────────────────────────────

def color_matches_str(color_str, allow_unknown=False):
    """
    Return True if the color matches your target colors.
    Edit this to change which colors are included.
    allow_unknown=True includes listings with no color data.
    """
    if not color_str:
        return allow_unknown
    c = color_str.lower()
    # Default: black, grey/gray, charcoal, and similar dark colors.
    # Comment out or remove lines to change what's included.
    return any(kw in c for kw in [
        "black", "gray", "grey", "charcoal", "dark",
        "obsidian", "magnetic", "graphite",
    ])
    # Examples:
    # To match ANY color: return True
    # To match white/silver/black: return any(kw in c for kw in ["white","silver","black"])


def trim_matches(trim_name):
    """Return True if the trim should be included. Edit to restrict trims."""
    return True  # all trims — example: return "limited" in trim_name.lower()


# ── CarGurus ──────────────────────────────────────────────────────────────────
# Set CARGURUS_URL (and optionally CARGURUS_URL_2) in your config.
# Go to cargurus.com, search with your filters, copy the URL.

def _cg_extract_listings(page):
    big_script = page.evaluate("""() => {
        const scripts = Array.from(document.querySelectorAll('script:not([src])'));
        for (const s of scripts) {
            if (s.textContent.length > 500000) return s.textContent;
        }
        return null;
    }""")
    if not big_script:
        return []
    listings = []
    seen_ids = set()
    for m in re.finditer(r'\{"type":"LISTING_USED[^"]*","data":\{', big_script):
        start = m.start() + len(m.group()) - 1
        depth = 0
        for i, c in enumerate(big_script[start:start + 60000]):
            if c == "{": depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(big_script[start: start + i + 1])
                        lid = data.get("id")
                        if lid and lid not in seen_ids:
                            seen_ids.add(lid)
                            listings.append(data)
                    except Exception:
                        pass
                    break
    rec_script = page.evaluate("""() => {
        const scripts = Array.from(document.querySelectorAll('script:not([src])'));
        for (const s of scripts) {
            const txt = s.textContent || '';
            if (txt.includes('"recommendations"') && txt.includes('"listingId"') && txt.length < 50000)
                return txt;
        }
        return null;
    }""")
    if rec_script:
        m = re.search(r'\[(\{"listingId".*)\]', rec_script, re.DOTALL)
        if m:
            try:
                recs = json.loads("[" + m.group(1) + "]")
                for rec in recs:
                    lid = rec.get("listingId")
                    if lid and lid not in seen_ids:
                        seen_ids.add(lid)
                        listings.append({
                            "id": lid,
                            "listingTitle": rec.get("listingTitle"),
                            "mileageData": {"value": rec.get("mileage")},
                            "priceData": {"price": rec.get("price")},
                            "exteriorColorData": {"name": rec.get("exteriorColor"), "normalized": None},
                            "ontologyData": {
                                "carYear": str(rec.get("year", "")),
                                "makeName": rec.get("make"),
                                "modelName": rec.get("model"),
                                "trimName": rec.get("trim"),
                            },
                            "sellerData": {"city": rec.get("cityRegion", "").split(",")[0], "stateAbbreviation": ""},
                            "distance": None,
                            "dealRating": rec.get("dealFinderRating", ""),
                        })
            except Exception as e:
                print(f"[warn] cg rec parse: {e}", file=sys.stderr)
    return listings


def _cg_extract_dom_cards(page):
    cards = page.evaluate("""() => {
        const results = [];
        const links = Array.from(document.querySelectorAll('a[href*="/details/"]'));
        const seen = new Set();
        links.forEach(a => {
            const idMatch = a.href.match(/\\/details\\/(\\d+)/);
            const lid = idMatch ? idMatch[1] : '';
            if (!lid || seen.has(lid)) return;
            seen.add(lid);
            let card = a;
            for (let i = 0; i < 3; i++) { if (card.parentElement) card = card.parentElement; }
            results.push({ _lid: lid, _href: a.href, _text: card.innerText || '' });
        });
        return results;
    }""")
    return cards or []


def _cg_parse_dom(card):
    text = card.get('_text', '')
    lid = card.get('_lid', '')
    year_m = YEAR_RE.search(text)
    if not year_m:
        return None
    year = int(year_m.group(1))
    trim_m = re.search(r'\b(premium|limited|sti|sport|base)\b', text, re.I)
    trim = trim_m.group(0).title() if trim_m else ''
    price_matches = re.findall(r'\$([\d,]+)', text)
    prices = [int(p.replace(',', '')) for p in price_matches]
    price = max(prices) if prices else None
    miles_m = re.search(r'([\d,]{4,})\s+mi\b(?!\s*away)', text)
    mileage = int(miles_m.group(1).replace(',', '')) if miles_m else None
    deal = ''
    for d in ['Great Deal', 'Good Deal', 'Fair Deal', 'High Priced', 'Overpriced']:
        if d in text: deal = d; break
    loc_m = re.search(r'(\d+) mi away', text)
    distance = int(loc_m.group(1)) if loc_m else None
    if not lid:
        return None
    return {
        'id': f'cg_{lid}',
        'title': f"{year} {SEARCH_MAKE} {SEARCH_MODEL} {trim}".strip(),
        'year': year, 'trim': trim, 'price': price, 'mileage': mileage,
        'color': 'Unknown', 'color_str': '', 'location': 'N/A',
        'distance': distance, 'deal': deal,
        'url': f"https://www.cargurus.com/details/{lid}",
        'source': 'CarGurus',
    }


def _cg_parse(data):
    onto = data.get("ontologyData") or {}
    mileage_data = data.get("mileageData") or {}
    price_data = data.get("priceData") or {}
    seller = data.get("sellerData") or {}
    color_data = data.get("exteriorColorData") or {}
    try:
        year = int(onto.get("carYear") or "")
    except Exception:
        return None
    mileage = mileage_data.get("value")
    if mileage is None:
        try: mileage = int(data.get("localizedMileage", "").replace(",", ""))
        except Exception: mileage = None
    price = price_data.get("current") or price_data.get("price") or price_data.get("totalPrice")
    if price and isinstance(price, str):
        price = int(re.sub(r"[^\d]", "", price)) or None
    elif price:
        price = int(price)
    trim = onto.get("trimName") or ""
    title = data.get("listingTitle") or f"{year} {onto.get('makeName',SEARCH_MAKE)} {onto.get('modelName',SEARCH_MODEL)} {trim}".strip()
    city = seller.get("city") or seller.get("cityRegion") or ""
    state = seller.get("stateAbbreviation") or seller.get("state") or seller.get("region") or ""
    seller_zip = seller.get("postalCode") or seller.get("zip") or ""
    distance = data.get("distance")
    if isinstance(distance, float): distance = int(distance)
    if not state and seller_zip:
        _, s = zip_to_city_state(str(seller_zip)[:5])
        if s: state = s
    if (not state or distance is None) and city:
        result = city_to_state_and_distance(city)
        if result:
            if not state: state = result[0]
            if distance is None: distance = result[1]
    if distance is None and seller_zip:
        d = zip_distance_miles(str(seller_zip)[:5])
        if d is not None: distance = int(d)
    location = f"{city}, {state}".strip(", ") if (city or state) else "N/A"
    lid = data.get("id") or data.get("listingId")
    color_name = color_data.get("name") or "Unknown"
    color_norm = (color_data.get("normalized") or "").upper()
    return {
        "id": f"cg_{lid}", "title": title, "year": year, "trim": trim,
        "price": price, "mileage": mileage,
        "color": color_name, "color_str": f"{color_norm} {color_name}",
        "location": location, "distance": distance,
        "deal": (data.get("dealRating") or "").replace("_", " ").title(),
        "url": f"https://www.cargurus.com/details/{lid}",
        "source": "CarGurus",
    }


def scrape_cargurus(page, url):
    label = url[:60]
    print(f"[CarGurus] Loading {label}...", file=sys.stderr)
    intercepted = []
    def handle_response(response):
        r_url = response.url
        if "cargurus.com" in r_url and any(k in r_url for k in ["listings", "inventory", "searchResults"]):
            try: intercepted.append(response.json())
            except Exception: pass
    page.on("response", handle_response)
    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(3000)
    for scroll_y in [800, 2000, 4000, 6000, 8000]:
        page.evaluate(f"window.scrollTo(0, {scroll_y})")
        page.wait_for_timeout(1200)
    api_listings = []
    for body in intercepted:
        if isinstance(body, dict):
            listings_data = body.get("listings") or (body.get("data") or {}).get("listings")
            if listings_data and isinstance(listings_data, list):
                api_listings.extend(listings_data)
    raw = api_listings if api_listings else _cg_extract_listings(page)
    print(f"[CarGurus] Raw: {len(raw)}", file=sys.stderr)
    dom_cards = _cg_extract_dom_cards(page)
    results = []
    seen_ids = set()
    dom_candidates = []
    for card in dom_cards:
        if not _has_model_kw(card.get('_text', '')):
            continue
        parsed = _cg_parse_dom(card)
        if not parsed: continue
        if not (MIN_YEAR <= parsed["year"] <= MAX_YEAR): continue
        if parsed["mileage"] is not None and parsed["mileage"] > MAX_MILES: continue
        if not trim_matches(parsed["trim"]): continue
        dist = parsed.get("distance")
        if dist is not None and dist > RADIUS: continue
        dom_candidates.append(parsed)
    detail_page = page.context.new_page()
    for parsed in dom_candidates:
        try:
            detail_page.goto(parsed["url"], wait_until="domcontentloaded", timeout=30000)
            detail_page.wait_for_timeout(2000)
            detail_text = detail_page.inner_text("body") or ""
        except Exception as e:
            print(f"[CarGurus] Detail fetch failed {parsed['id']}: {e}", file=sys.stderr)
            if parsed["id"] not in seen_ids:
                seen_ids.add(parsed["id"])
                results.append(parsed)
            continue
        if not _has_model_kw(detail_text):
            continue
        color_m = re.search(r"exterior colou?r[:\s]+([^\n·\|]+)", detail_text, re.I)
        color_raw = color_m.group(1).strip() if color_m else ""
        if color_raw and not color_matches_str(color_raw):
            continue
        if parsed.get("distance") is None:
            dist_m = re.search(r"([\d,]+)\s*mi(?:les?)?\s*away", detail_text, re.I)
            if dist_m: parsed["distance"] = int(dist_m.group(1).replace(",", ""))
        if parsed.get("distance") is None:
            page_src = detail_page.content()
            lat_m = re.search(r'"latitude"\s*:\s*([\d.-]+)', page_src)
            lon_m = re.search(r'"longitude"\s*:\s*([\d.-]+)', page_src)
            if lat_m and lon_m:
                try:
                    dist = haversine_miles(ORIGIN_LAT, ORIGIN_LON, float(lat_m.group(1)), float(lon_m.group(1)))
                    parsed["distance"] = int(dist)
                except Exception: pass
        if parsed.get("distance") is not None and parsed["distance"] > RADIUS:
            continue
        loc_m = re.search(r"([A-Z][a-zA-Z\s]+),\s*([A-Z]{2})\b", detail_text)
        if loc_m: parsed["location"] = f"{loc_m.group(1).strip()}, {loc_m.group(2)}"
        parsed["color"] = color_raw or "Unknown"
        parsed["color_str"] = color_raw
        if parsed["id"] not in seen_ids:
            seen_ids.add(parsed["id"])
            results.append(parsed)
    detail_page.close()
    for item in raw:
        parsed = _cg_parse(item)
        if not parsed: continue
        if not (MIN_YEAR <= parsed["year"] <= MAX_YEAR): continue
        if parsed["mileage"] is not None and parsed["mileage"] > MAX_MILES: continue
        if not color_matches_str(parsed["color_str"], allow_unknown=True): continue
        if not trim_matches(parsed["trim"]): continue
        dist = parsed.get("distance")
        if dist is not None and dist > RADIUS: continue
        if parsed["id"] not in seen_ids:
            seen_ids.add(parsed["id"])
            results.append(parsed)
    return results


# ── Craigslist ────────────────────────────────────────────────────────────────
# Searches CL_REGIONS using SEARCH_KEYWORDS as the query.

def _cl_scrape_region(page, region):
    url = (
        f"https://{region}.craigslist.org/search/cta"
        f"?query={_CL_QUERY}&min_price={MIN_PRICE}&max_price={MAX_PRICE}"
    )
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
    except Exception as e:
        print(f"[CL/{region}] Load error: {e}", file=sys.stderr)
        return []
    raw = page.evaluate("""() => {
        const cards = Array.from(document.querySelectorAll('.cl-search-result'));
        return cards.map(card => ({
            pid: card.getAttribute('data-pid') || '',
            title: (card.querySelector('.posting-title .label') || card.querySelector('.posting-title') || {innerText:''}).innerText.trim(),
            price: (card.querySelector('.priceinfo') || {innerText:''}).innerText.trim(),
            meta: (card.querySelector('.meta') || {innerText:''}).innerText.trim(),
            location: (card.querySelector('.maptag') || {innerText:''}).innerText.trim(),
            href: (card.querySelector('a.posting-title') || {href:''}).href,
        }));
    }""")
    return [(region, item) for item in raw if item.get("pid") and item.get("title")]


def _cl_visit_detail(page, listing):
    href = listing.get("url", "")
    if not href:
        return listing
    try:
        page.goto(href, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        detail_text = page.inner_text("body") or ""
    except Exception as e:
        print(f"[CL detail] fetch failed {listing['id']}: {e}", file=sys.stderr)
        return listing
    vin_m = re.search(r'\bVIN[:\s]+([A-HJ-NPR-Z0-9]{17})\b', detail_text, re.I)
    if vin_m: listing["vin"] = vin_m.group(1).upper()
    color_label_m = re.search(r"paint colou?r[:\s]+([^\n]+)", detail_text, re.I)
    if color_label_m:
        color_raw = color_label_m.group(1).strip()
        if "custom" in color_raw.lower():
            return None
        listing["color"] = color_raw
        listing["color_str"] = color_raw
        if not color_matches_str(color_raw):
            print(f"[CL detail] skip {listing['id']} color='{color_raw}'", file=sys.stderr)
            return None
    lat_m = re.search(r'data-latitude="([\d.-]+)"', page.content())
    lon_m = re.search(r'data-longitude="([\d.-]+)"', page.content())
    if lat_m and lon_m:
        try:
            lat, lon = float(lat_m.group(1)), float(lon_m.group(1))
            dist = haversine_miles(ORIGIN_LAT, ORIGIN_LON, lat, lon)
            listing["distance"] = int(dist)
            if dist > RADIUS:
                return None
        except Exception:
            pass
    return listing


def scrape_craigslist(ctx):
    all_raw = []
    seen_pids = set()
    for region in CL_REGIONS:
        page = ctx.new_page()
        items = _cl_scrape_region(page, region)
        page.close()
        for r, item in items:
            pid = item["pid"]
            if pid not in seen_pids:
                seen_pids.add(pid)
                all_raw.append((r, item))
    print(f"[Craigslist] Raw across {len(CL_REGIONS)} regions: {len(all_raw)}", file=sys.stderr)
    candidates = []
    for region, item in all_raw:
        title = item.get("title", "")
        if not _has_model_kw(title):
            continue
        year_m = YEAR_RE.search(title)
        if not year_m: continue
        year = int(year_m.group(1))
        meta = item.get("meta", "")
        mi_m = re.search(r"([\d,]+)k?\s*mi", meta, re.I)
        if mi_m:
            mi_str = mi_m.group(1).replace(",", "")
            mileage = int(mi_str) * 1000 if "k" in meta[mi_m.start():mi_m.end()].lower() else int(mi_str)
        else:
            mileage = None
        try: price = int(re.sub(r"[^\d]", "", item.get("price", ""))) if item.get("price") else None
        except Exception: price = None
        trim_m = re.search(r"\b(premium|limited|sti|sport|base)\b", title, re.I)
        trim = trim_m.group(0).title() if trim_m else ""
        color_m = re.search(r"\b(black|gray|grey|silver|white|blue|red|green|orange|yellow|brown|graphite|charcoal)\b", title, re.I)
        color = color_m.group(0).title() if color_m else ""
        if mileage is not None and mileage > MAX_MILES: continue
        if not trim_matches(trim): continue
        if color and not color_matches_str(color): continue
        candidates.append({
            "id": f"cl_{item['pid']}", "vin": "",
            "title": title, "year": year, "trim": trim, "price": price,
            "mileage": mileage, "color": color or "Unknown", "color_str": color,
            "location": item.get("location") or region.title(),
            "distance": None, "deal": "", "url": item.get("href", ""),
            "source": f"Craigslist/{region}",
        })
    print(f"[Craigslist] Candidates before detail: {len(candidates)}", file=sys.stderr)
    skip_list = []
    need_detail = []
    for listing in candidates:
        if listing.get("color") and color_matches_str(listing["color"]) and listing.get("distance") is not None:
            print(f"[CL detail] skip fetch {listing['id']} — color+dist already known", file=sys.stderr)
            skip_list.append(listing)
        else:
            need_detail.append(listing)

    CL_WORKERS = min(4, len(need_detail)) if need_detail else 1

    def _visit_with_own_page(listing):
        # Playwright sync contexts are bound to the thread that created them.
        # Give each worker its own tiny browser/context to keep parallel CL
        # detail fetches from tripping greenlet thread switching errors.
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            thread_ctx = browser.new_context()
            page = thread_ctx.new_page()
            try:
                return _cl_visit_detail(page, listing)
            finally:
                page.close()
                thread_ctx.close()
                browser.close()

    detail_results = []
    if need_detail:
        with ThreadPoolExecutor(max_workers=CL_WORKERS) as pool:
            futs = {pool.submit(_visit_with_own_page, lst): lst for lst in need_detail}
            for fut in as_completed(futs):
                r = fut.result()
                if r is not None:
                    detail_results.append(r)

    results = skip_list + detail_results
    print(f"[Craigslist] After detail filter: {len(results)}", file=sys.stderr)
    return results


# ── Cars.com ──────────────────────────────────────────────────────────────────
# Set CARSDOTCOM_URL in your config. Go to cars.com, search with your filters,
# copy the URL from your browser.

def _cd_extract_listings(page):
    raw = page.evaluate("""() => {
        const cards = Array.from(document.querySelectorAll('fuse-card[data-listing-id], [data-listing-id][data-vehicle-details]'));
        return cards.map(card => ({
            id: card.getAttribute('data-listing-id') || '',
            details: card.getAttribute('data-vehicle-details') || '',
            href: (card.querySelector('a[href*="vehicledetail"]') || {}).href || ''
        }));
    }""")
    listings = []
    seen = set()
    for item in raw:
        lid = item.get("id", "")
        if not lid or lid in seen: continue
        seen.add(lid)
        try: details = json.loads(item["details"])
        except Exception: continue
        details["_listing_id"] = lid
        details["_href"] = item.get("href", "")
        listings.append(details)
    return listings


def _cd_parse(data):
    try: year = int(data.get("year", ""))
    except Exception: return None
    try: price = int(str(data.get("price", "") or "").replace(",", ""))
    except Exception: price = None
    try: mileage = int(str(data.get("mileage", "") or "").replace(",", ""))
    except Exception: mileage = None
    trim = data.get("trim", "")
    make = data.get("make", SEARCH_MAKE)
    model = data.get("model", SEARCH_MODEL)
    color = data.get("exteriorColor", "Unknown") or "Unknown"
    seller = data.get("seller", {}) or {}
    city, state = seller.get("city", ""), seller.get("state", "")
    seller_zip = seller.get("zip", "")
    if city and state:
        location = f"{city}, {state}"
    elif seller_zip:
        c, s = zip_to_city_state(seller_zip)
        location = f"{c}, {s}" if (c and s) else f"ZIP {seller_zip}"
    else:
        location = "N/A"
    lid = data.get("_listing_id", "")
    href = data.get("_href", "")
    url = href if href else f"https://www.cars.com/vehicledetail/{lid}/"
    distance = zip_distance_miles(seller_zip) if seller_zip else None
    vin = data.get("vin", "") or ""
    return {
        "id": f"cd_{lid}", "vin": vin if len(vin) == 17 else "",
        "title": f"{year} {make} {model} {trim}".strip(),
        "year": year, "trim": trim, "price": price, "mileage": mileage,
        "color": color, "color_str": color, "location": location,
        "distance": int(distance) if distance is not None else None,
        "deal": "", "url": url, "source": "Cars.com",
    }


def scrape_carsdotcom(page):
    print("[Cars.com] Loading...", file=sys.stderr)
    if HAS_STEALTH: stealth_sync(page)
    try:
        page.goto("https://www.cars.com/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000 + int(os.urandom(1)[0] / 255 * 2000))
        page.mouse.move(400 + int(os.urandom(1)[0] / 255 * 200), 300 + int(os.urandom(1)[0] / 255 * 100))
        page.wait_for_timeout(800)
        page.goto(CARSDOTCOM_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000 + int(os.urandom(1)[0] / 255 * 2000))
        page.evaluate("window.scrollTo(0, 400)")
        page.wait_for_timeout(1200)
        page.evaluate("window.scrollTo(0, 1000)")
        page.wait_for_timeout(1500)
    except Exception as e:
        print(f"[Cars.com] Load error: {e}", file=sys.stderr)
        return []
    raw = _cd_extract_listings(page)
    print(f"[Cars.com] Raw listings: {len(raw)}", file=sys.stderr)
    results = []
    for item in raw:
        parsed = _cd_parse(item)
        if not parsed: continue
        if parsed["year"] and not (MIN_YEAR <= parsed["year"] <= MAX_YEAR): continue
        if parsed["mileage"] is not None and parsed["mileage"] > MAX_MILES: continue
        if not color_matches_str(parsed["color_str"], allow_unknown=True): continue
        if not trim_matches(parsed["trim"]): continue
        dist = parsed.get("distance")
        if dist is not None and dist > RADIUS: continue
        vin = parsed.get("vin", "")
        if vin:
            decoded = nhtsa_decode_vin(vin)
            make = (decoded.get("make") or "").upper()
            model = (decoded.get("model") or "").upper()
            if make and SEARCH_MAKE.upper() not in make: continue
            if model and SEARCH_MODEL.upper() not in model: continue
        results.append(parsed)
    return results


# ── Facebook Marketplace ──────────────────────────────────────────────────────
# Searches using SEARCH_KEYWORDS and FB_CITY.
# Requires a saved session file. Run fb-auth-setup.py first.

def _fb_scrape_url(page, url, label=""):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)
        if "login" in page.url.lower() or "log in" in (page.title() or "").lower():
            return None
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
            close_btn = page.query_selector('div[aria-label="Close"]')
            if close_btn:
                close_btn.click()
                page.wait_for_timeout(800)
        except Exception: pass
        for _ in range(5):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
    except Exception as e:
        print(f"[Facebook{label}] Load error: {e}", file=sys.stderr)
        return []
    raw = page.evaluate("""() => {
        const results = [];
        const seen = new Set();
        const cards = Array.from(document.querySelectorAll('a[href*="/marketplace/item/"]'));
        for (const a of cards) {
            const href = a.href || '';
            const m = href.match(/marketplace\\/item\\/(\\d+)/);
            if (!m) continue;
            const pid = m[1];
            if (seen.has(pid)) continue;
            seen.add(pid);
            results.push({ pid, href, text: a.innerText || a.textContent || '' });
        }
        return results;
    }""")
    return raw or []


def scrape_facebook(ctx):
    if not os.path.exists(FB_SESSION_FILE):
        print("[Facebook] No session file — run fb-auth-setup.py first", file=sys.stderr)
        return []
    page = ctx.new_page()
    raw1 = _fb_scrape_url(page, FB_SEARCH_URL, "/search")
    if raw1 is None:
        print("[Facebook] Session expired — run fb-auth-setup.py to refresh", file=sys.stderr)
        send_telegram("⚠️ <b>Facebook Marketplace session expired</b>\nRun fb-auth-setup.py to refresh.")
        page.close()
        return []
    page.close()
    seen_pids = set()
    raw = []
    for item in (raw1 or []):
        pid = item.get("pid", "")
        if pid and pid not in seen_pids:
            seen_pids.add(pid)
            raw.append(item)
    print(f"[Facebook] Raw listings: {len(raw)}", file=sys.stderr)
    candidates = []
    for item in raw:
        text = item.get("text", "")
        if not _has_model_kw(text): continue
        year_m = YEAR_RE.search(text)
        if not year_m: continue
        year = int(year_m.group(1))
        if not (MIN_YEAR <= year <= MAX_YEAR): continue
        price_m = re.search(r"\$\s*([\d,]+)", text)
        price = int(price_m.group(1).replace(",", "")) if price_m else None
        if price and price > MAX_PRICE: continue
        candidates.append({"pid": item["pid"], "href": item["href"], "year": year, "price": price})
    print(f"[Facebook] Candidates after card filter: {len(candidates)}", file=sys.stderr)
    results = []
    detail_page = ctx.new_page()
    for item in candidates:
        pid, href, year, price = item["pid"], item["href"], item["year"], item["price"]
        try:
            detail_page.goto(href, wait_until="domcontentloaded", timeout=30000)
            detail_page.wait_for_timeout(2000)
            dt = detail_page.inner_text("body") or ""
        except Exception as e:
            print(f"[Facebook] Detail failed {pid}: {e}", file=sys.stderr)
            continue
        mi_m = re.search(r"(?:driven\s+)?([\d,]+)\s*miles", dt, re.I)
        mileage = int(mi_m.group(1).replace(",", "")) if mi_m else None
        if mileage is not None and mileage > MAX_MILES: continue
        color_label_m = re.search(r"exterior colou?r[:\s]+([^\n·]+)", dt, re.I)
        if color_label_m:
            color_raw = color_label_m.group(1).strip()
        else:
            color_m = re.search(r"\b(black|gray|grey|charcoal|graphite|obsidian|magnetic|dark|white|silver|blue|red|orange)\b", dt, re.I)
            color_raw = color_m.group(0).title() if color_m else ""
        if color_raw and not color_matches_str(color_raw, allow_unknown=True): continue
        trim_m = re.search(r"\b(premium|limited|sti|sport|base)\b", dt, re.I)
        trim = trim_m.group(0).title() if trim_m else ""
        if not trim_matches(trim): continue
        loc_m = re.search(r"in ([A-Z][a-zA-Z\s]+),\s*([A-Z]{2})", dt)
        if loc_m:
            city_name, state_abbr = loc_m.group(1).strip(), loc_m.group(2)
            location = f"{city_name}, {state_abbr}"
        else:
            city_name, state_abbr, location = "", "", "N/A"
        distance = None
        if city_name and state_abbr:
            try:
                import pgeocode
                nomi = pgeocode.Nominatim("us")
                df = nomi._data_frame
                rows = df[(df['place_name'].str.lower() == city_name.lower()) & (df['state_code'] == state_abbr)]
                if not rows.empty:
                    flat = float(rows.iloc[0]['latitude']); flon = float(rows.iloc[0]['longitude'])
                    distance = int(haversine_miles(ORIGIN_LAT, ORIGIN_LON, flat, flon))
            except Exception:
                pass
        if distance is not None and distance > RADIUS:
            continue
        results.append({
            "id": f"fb_{pid}", "vin": "",
            "title": f"{year} {SEARCH_MAKE} {SEARCH_MODEL} {trim}".strip(),
            "year": year, "trim": trim, "price": price, "mileage": mileage,
            "color": color_raw or "Unknown", "color_str": color_raw,
            "location": location, "distance": distance, "deal": "", "url": href,
            "source": "Facebook",
        })
    detail_page.close()
    return results


# ── AutoTrader ────────────────────────────────────────────────────────────────
# Set AUTOTRADER_URL in your config. Go to autotrader.com, set your filters,
# copy the URL from your browser.
#
# AutoTrader aggressively blocks headless browsers. Set CHROME_CDP_HOST to
# connect via a real Chrome instance for much better results.
# See README.md for setup.

_CHROME_LAUNCH_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
_CHROME_LAUNCH_ARGS = [
    "--remote-debugging-port=9222",
    "--remote-allow-origins=*",
    "--no-first-run", "--no-default-browser-check",
    "--user-data-dir=C:\\Temp\\chrome-debug",
]


def _chrome_reachable():
    import socket
    try:
        s = socket.create_connection((CHROME_CDP_HOST, CHROME_CDP_PORT), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def _chrome_kill():
    import subprocess
    try:
        subprocess.run(["/mnt/c/Windows/System32/cmd.exe", "/c", "taskkill", "/F", "/IM", "chrome.exe"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        __import__("time").sleep(2)
    except Exception:
        pass


def _chrome_launch_and_wait():
    import subprocess, time
    for chrome_path in _CHROME_LAUNCH_PATHS:
        wsl_path = "/mnt/c" + chrome_path[2:].replace("\\", "/")
        if not os.path.exists(wsl_path):
            continue
        try:
            subprocess.Popen(["/mnt/c/Windows/System32/cmd.exe", "/c", "start", "", chrome_path] + _CHROME_LAUNCH_ARGS,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[AutoTrader] Failed to launch Chrome: {e}", file=sys.stderr)
            continue
        for _ in range(15):
            time.sleep(1)
            if _chrome_reachable():
                print("[AutoTrader] Chrome CDP is now available", file=sys.stderr)
                return True
        print("[AutoTrader] Chrome launched but CDP not reachable after 15s", file=sys.stderr)
        return False
    print("[AutoTrader] Could not find Chrome on Windows", file=sys.stderr)
    return False


def _chrome_cdp_url(force_relaunch=False):
    if not CHROME_CDP_HOST:
        return None
    if force_relaunch:
        print("[AutoTrader] Relaunching Chrome (stale CDP session)...", file=sys.stderr)
        _chrome_kill()
    if not force_relaunch and _chrome_reachable():
        return f"http://{CHROME_CDP_HOST}:{CHROME_CDP_PORT}"
    print("[AutoTrader] Chrome CDP not running -- launching Chrome on Windows...", file=sys.stderr)
    if _chrome_launch_and_wait():
        return f"http://{CHROME_CDP_HOST}:{CHROME_CDP_PORT}"
    return None


def _autotrader_parse_page(page):
    raw_json = page.evaluate("""() => {
        const scripts = Array.from(document.querySelectorAll('script'));
        for (const s of scripts) {
            const txt = s.textContent || '';
            if (txt.includes('"listings"') && txt.includes('"vin"') && txt.length > 10000) return txt;
        }
        return null;
    }""")
    import re as _re, json as _json
    listings = []
    if raw_json:
        m = _re.search(r'"listings"\s*:\s*(\[)', raw_json)
        if m:
            start = m.start(1)
            depth = 0
            for i, c in enumerate(raw_json[start:start+500000]):
                if c in "[{": depth += 1
                elif c in "]}":
                    depth -= 1
                    if depth == 0:
                        try: listings = _json.loads(raw_json[start:start+i+1])
                        except Exception: pass
                        break
    if listings:
        print(f"[AutoTrader] Raw (JSON): {len(listings)}", file=sys.stderr)
        results = []
        for item in listings:
            try: year = int(item.get("year", 0))
            except Exception: continue
            if not (MIN_YEAR <= year <= MAX_YEAR): continue
            try: mileage = int(str(item.get("mileage", "") or "").replace(",", ""))
            except Exception: mileage = None
            if mileage is not None and mileage > MAX_MILES: continue
            try: price = int(str(item.get("pricingDetail", {}).get("salePrice") or item.get("price", "") or "").replace(",", "").replace("$", ""))
            except Exception: price = None
            trim = item.get("trim") or ""
            color = item.get("color") or item.get("exteriorColor") or ""
            if not color_matches_str(color, allow_unknown=True): continue
            vin = item.get("vin", "")
            lid = item.get("id") or item.get("listingId") or vin
            owner = item.get("owner", {}) or {}
            city, state = owner.get("city", ""), owner.get("state", "")
            location = f"{city}, {state}".strip(", ") if (city or state) else "N/A"
            results.append({
                "id": f"at_{lid}", "vin": vin,
                "title": f"{year} {item.get('make', SEARCH_MAKE)} {item.get('model', SEARCH_MODEL)} {trim}".strip(),
                "year": year, "trim": trim, "price": price, "mileage": mileage,
                "color": color or "Unknown", "color_str": color,
                "location": location, "distance": None, "deal": "",
                "url": f"https://www.autotrader.com/cars-for-sale/vehicle/{lid}",
                "source": "AutoTrader",
            })
        return results
    # Fallback: DOM card scraping
    cards = page.evaluate(r"""() => {
        const seen = new Set();
        const results = [];
        const links = Array.from(document.querySelectorAll('a[href*="/cars-for-sale/vehicle/"]'));
        for (const a of links) {
            const m = (a.href || '').match(/vehicle\/([0-9]+)/);
            if (!m) continue;
            const vid = m[1];
            if (seen.has(vid)) continue;
            seen.add(vid);
            let el = a;
            for (let i = 0; i < 6; i++) { if (el.parentElement) el = el.parentElement; }
            results.push({ vid, href: a.href.split('?')[0], text: (el.innerText || el.textContent || '').trim().slice(0, 400) });
        }
        return results;
    }""")
    print(f"[AutoTrader] DOM cards: {len(cards)}", file=sys.stderr)
    results = []
    for card in cards:
        text = card.get("text", "")
        if not _has_model_kw(text): continue
        year_m = YEAR_RE.search(text)
        if not year_m: continue
        year = int(year_m.group(1))
        if not (MIN_YEAR <= year <= MAX_YEAR): continue
        price_m = re.search(r"\$\s*([\d,]+)", text)
        price = int(price_m.group(1).replace(",", "")) if price_m else None
        mi_m = re.search(r"([\d,]+)\s*mi(?:les)?", text, re.I)
        mileage = int(mi_m.group(1).replace(",", "")) if mi_m else None
        if mileage is not None and mileage > MAX_MILES: continue
        trim_m = re.search(r"\b(premium|limited|sti|sport|base)\b", text, re.I)
        trim = trim_m.group(0).title() if trim_m else ""
        color_m = re.search(r"\b(black|gray|grey|charcoal|graphite|obsidian|magnetic|dark)\b", text, re.I)
        color = color_m.group(0).title() if color_m else ""
        dist_m = re.search(r"(\d+)\s*mi(?:les)?\s*away", text, re.I)
        distance = int(dist_m.group(1)) if dist_m else None
        loc_m = re.search(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2})\b", text)
        location = loc_m.group(1) if loc_m else "N/A"
        vid = card.get("vid", "")
        results.append({
            "id": f"at_{vid}", "vin": "",
            "title": f"{year} {SEARCH_MAKE} {SEARCH_MODEL} {trim}".strip(),
            "year": year, "trim": trim, "price": price, "mileage": mileage,
            "color": color or "Unknown", "color_str": color,
            "location": location, "distance": distance, "deal": "",
            "url": f"https://www.autotrader.com/cars-for-sale/vehicle/{vid}",
            "source": "AutoTrader",
        })
    return results


def scrape_autotrader_cdp(pw):
    for attempt in range(2):
        cdp_url = _chrome_cdp_url(force_relaunch=(attempt > 0))
        if not cdp_url:
            print("[AutoTrader] Chrome CDP not available -- falling back to Playwright", file=sys.stderr)
            return None
        print("[AutoTrader] Connecting via Chrome CDP...", file=sys.stderr)
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url, timeout=10000)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            page.goto(AUTOTRADER_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            title = page.title() or ""
            if "unavailable" in title.lower() or "blocked" in title.lower():
                page.close()
                try: browser.disconnect()
                except: pass
                return []
            for scroll_y in [800, 2000, 4000]:
                page.evaluate(f"window.scrollTo(0, {scroll_y})")
                page.wait_for_timeout(800)
            results = _autotrader_parse_page(page)
            page.close()
            try: browser.disconnect()
            except: pass
            return results
        except Exception as e:
            err = str(e)
            print(f"[AutoTrader] CDP error: {err}", file=sys.stderr)
            if ("ECONNRESET" in err or "ECONNREFUSED" in err) and attempt == 0:
                print("[AutoTrader] Connection reset -- killing and relaunching Chrome...", file=sys.stderr)
                continue
            return None
    return None


def scrape_autotrader(page):
    print("[AutoTrader] Loading (Playwright fallback)...", file=sys.stderr)
    try:
        page.goto(AUTOTRADER_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)
        for scroll_y in [800, 2000, 4000]:
            page.evaluate(f"window.scrollTo(0, {scroll_y})")
            page.wait_for_timeout(1000)
    except Exception as e:
        print(f"[AutoTrader] Load error: {e}", file=sys.stderr)
        return []
    page_title = page.title() or ""
    if "unavailable" in page_title.lower() or "blocked" in page_title.lower():
        print("[AutoTrader] Bot block detected — set up Chrome CDP for better results", file=sys.stderr)
        return []
    return _autotrader_parse_page(page)


# ── eBay Motors ───────────────────────────────────────────────────────────────
# Searches using SEARCH_KEYWORDS via the Browse API (recommended) or page scraping.
# API requires an eBay User Token in ebay-token.txt — see README.md.

def _ebay_refresh_access_token():
    """Use the refresh token to get a new access token. Returns token or None."""
    if not os.path.exists(EBAY_REFRESH_TOKEN_FILE):
        return None
    refresh_token = open(EBAY_REFRESH_TOKEN_FILE).read().strip()
    if not refresh_token or not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        return None
    import base64 as _b64
    creds = _b64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "https://api.ebay.com/oauth/api_scope",
    }).encode()
    req = urllib.request.Request(
        "https://api.ebay.com/identity/v1/oauth2/token",
        data=data,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
        token = resp.get("access_token", "")
        if token:
            with open(EBAY_TOKEN_FILE, "w") as f:
                f.write(token)
            print("[eBay API] Token refreshed successfully.", file=sys.stderr)
            return token
    except Exception as e:
        print(f"[eBay API] Token refresh failed: {e}", file=sys.stderr)
    return None


def _ebay_oauth_token():
    token = None
    if os.path.exists(EBAY_TOKEN_FILE):
        token = open(EBAY_TOKEN_FILE).read().strip()
    if not token:
        token = _ebay_refresh_access_token()
    if not token:
        return None
    # Test the token; if expired (401), try to refresh
    test_req = urllib.request.Request(
        "https://api.ebay.com/buy/browse/v1/item_summary/search?q=test&limit=1",
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
    )
    try:
        with urllib.request.urlopen(test_req, timeout=10) as r:
            r.read()
        return token
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("[eBay API] Token expired — attempting refresh...", file=sys.stderr)
            return _ebay_refresh_access_token()
    except Exception:
        pass
    return token


def fetch_ebay_api():
    print("[eBay API] Fetching...", file=sys.stderr)
    token = _ebay_oauth_token()
    if not token:
        print("[eBay API] No token — skipping. See README for setup.", file=sys.stderr)
        return []
    results = []
    for keyword in [f"{SEARCH_MAKE} {SEARCH_MODEL}"]:
        params = urllib.parse.urlencode({
            "q": keyword,
            "category_ids": "6001",
            "filter": f"conditionIds:{{3000}},price:[{MIN_PRICE}..{MAX_PRICE}],priceCurrency:USD",
            "limit": "50",
        })
        req = urllib.request.Request(
            f"https://api.ebay.com/buy/browse/v1/item_summary/search?{params}",
            headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
        except Exception as e:
            print(f"[eBay API] Error for '{keyword}': {e}", file=sys.stderr)
            continue
        items = data.get("itemSummaries", [])
        print(f"[eBay API] '{keyword}': {len(items)} raw results", file=sys.stderr)
        for item in items:
            title = item.get("title", "")
            if not _has_model_kw(title): continue
            try: price = int(float(item.get("price", {}).get("value", 0)))
            except Exception: price = 0
            if price < MIN_PRICE: continue
            year_m = re.search(r'\b(20\d{2})\b', title)
            if not year_m: continue
            year = int(year_m.group(1))
            if not (MIN_YEAR <= year <= MAX_YEAR):
                continue
            trim_m = re.search(r'\b(STI|Premium|Limited|Base|Sport)\b', title, re.I)
            trim = trim_m.group(0) if trim_m else ""
            results.append({
                "id": f"eb_{item.get('itemId','')}",
                "title": title,
                "year": year, "trim": trim, "price": price, "mileage": None,
                "color": "", "color_str": "",
                "location": "", "distance": None,
                "deal": "", "url": item.get("itemWebUrl", f"https://www.ebay.com/itm/{item.get('itemId','')}"),
                "source": "eBay Motors",
            })

    # Enrich candidates with detail API: mileage, color, VIN, location, distance
    print(f"[eBay API] Fetching details for {len(results)} candidates...", file=sys.stderr)
    enriched = []
    for r in results:
        item_id = r["id"].replace("eb_", "")
        detail_req = urllib.request.Request(
            f"https://api.ebay.com/buy/browse/v1/item/{item_id}",
            headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        )
        try:
            with urllib.request.urlopen(detail_req, timeout=15) as resp:
                d = json.loads(resp.read())
        except Exception as e:
            print(f"[eBay API] Detail fetch failed for {item_id}: {e}", file=sys.stderr)
            continue

        aspects = {a["name"].lower(): a["value"] for a in d.get("localizedAspects", [])}

        mileage = None
        for key in ("mileage", "odometer"):
            if key in aspects:
                try: mileage = int(re.sub(r'[^\d]', '', aspects[key]))
                except Exception: pass
                break
        if mileage is not None and mileage > MAX_MILES:
            print(f"[eBay API] skip {item_id} miles={mileage}", file=sys.stderr)
            continue

        color_raw = ""
        for key in ("exterior color", "color"):
            if key in aspects:
                color_raw = aspects[key]
                break
        if not color_raw or not color_matches_str(color_raw, allow_unknown=False):
            print(f"[eBay API] skip {item_id} color={color_raw!r}", file=sys.stderr)
            continue

        vin = ""
        for key in ("vin", "vehicle identification number"):
            if key in aspects:
                candidate = re.sub(r'[^A-HJ-NPR-Z0-9]', '', aspects[key].upper())
                if len(candidate) == 17:
                    vin = candidate
                break

        loc = d.get("itemLocation", {})
        postal = loc.get("postalCode", "")
        city = loc.get("city", "")
        state = loc.get("stateOrProvince", "")
        location_str = f"{city}, {state}".strip(", ") or "N/A"
        dist = zip_distance_miles(postal) if postal else None
        if dist is None:
            print(f"[eBay API] skip {item_id} — can't verify distance (postal={postal!r})", file=sys.stderr)
            continue
        if dist > RADIUS:
            print(f"[eBay API] skip {item_id} dist={dist:.0f}mi ({location_str})", file=sys.stderr)
            continue

        r.update({"vin": vin, "mileage": mileage, "color": color_raw, "color_str": color_raw,
                   "location": location_str, "distance": int(dist)})
        enriched.append(r)

    print(f"[eBay API] Total matches after detail filter: {len(enriched)}", file=sys.stderr)
    return enriched


def scrape_ebay(ctx):
    """Page-scraping fallback (no API token needed, but less reliable)."""
    print("[eBay] Loading...", file=sys.stderr)
    page = ctx.new_page()
    page.set_default_timeout(12000)
    page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_())
    try:
        page.goto(EBAY_SEARCH_URL, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)
        for scroll_y in [800, 2000]:
            page.evaluate(f"window.scrollTo(0, {scroll_y})")
            page.wait_for_timeout(800)
    except Exception as e:
        print(f"[eBay] Load error: {e}", file=sys.stderr)
        page.close()
        return []
    cards = page.evaluate("""() => {
        const results = [];
        const seen = new Set();
        const nodes = Array.from(document.querySelectorAll('li.s-item'));
        const links = nodes.length
            ? nodes.map(n => n.querySelector('a[href*="/itm/"]')).filter(Boolean)
            : Array.from(document.querySelectorAll('a[href*="/itm/"]'));
        links.forEach(a => {
            const m = (a.href || '').match(/\\/itm\\/(\\d+)/);
            if (!m) return;
            const iid = m[1];
            if (iid === '123456' || seen.has(iid)) return;
            seen.add(iid);
            const card = a.closest('li.s-item') || a.parentElement;
            results.push({ iid, href: a.href.split('?')[0], text: ((card && card.innerText) || '').slice(0, 800) });
        });
        return results;
    }""")
    page.close()
    candidates = []
    for card in cards:
        text = card.get("text", "")
        if not _has_model_kw(text): continue
        year_m = YEAR_RE.search(text)
        if not year_m: continue
        year = int(year_m.group(1))
        price_m = re.search(r"\$([\d,]+(?:\.\d+)?)", text)
        price = int(float(price_m.group(1).replace(",", ""))) if price_m else None
        if price and (price > MAX_PRICE or price < MIN_PRICE): continue
        trim_m = re.search(r"\b(premium|limited|sti|sport|base)\b", text, re.I)
        trim = trim_m.group(0).title() if trim_m else ""
        color_m = re.search(r"\b(black|gray|grey|charcoal|graphite|dark|obsidian|magnetic)\b", text, re.I)
        color = color_m.group(0).title() if color_m else ""
        if color and not color_matches_str(color): continue
        loc_m = re.search(r'from\s+([A-Za-z ,]+|\d{5})', text, re.I)
        candidates.append({
            "iid": card["iid"], "href": card["href"],
            "year": year, "trim": trim, "price": price, "color": color,
            "location": loc_m.group(1).strip() if loc_m else "",
        })
    results = []
    detail_page = ctx.new_page()
    for item in candidates:
        iid, href = item["iid"], item["href"]
        try:
            detail_page.goto(href, wait_until="domcontentloaded", timeout=30000)
            detail_page.wait_for_timeout(2000)
            dt = detail_page.inner_text("body") or ""
        except Exception as e:
            print(f"[eBay] detail failed {iid}: {e}", file=sys.stderr)
            continue
        if not _has_model_kw(dt): continue
        vin_m = re.search(r'VIN[:\s]+([A-HJ-NPR-Z0-9]{17})\b', dt, re.I)
        vin = vin_m.group(1).upper() if vin_m else ""
        mi_m = re.search(r'(?:mileage|odometer)[:\s]+([\d,]+)', dt, re.I) or re.search(r'([\d,]+)\s*mi(?:les)?\b', dt, re.I)
        mileage = int(mi_m.group(1).replace(",", "")) if mi_m else None
        if mileage is not None and mileage > MAX_MILES: continue
        color_label_m = re.search(r"exterior colou?r[:\s]+([^\n]+)", dt, re.I)
        if color_label_m:
            color_raw = color_label_m.group(1).strip()
            if "custom" in color_raw.lower() or not color_matches_str(color_raw):
                print(f"[eBay] skip {iid} color='{color_raw}'", file=sys.stderr)
                continue
            item["color"] = color_raw
        elif item["color"] and color_matches_str(item["color"]):
            pass  # card color confirmed
        else:
            print(f"[eBay] skip {iid} — no confirmed exterior color", file=sys.stderr)
            continue
        zip_m = re.search(r'\b(\d{5})\b', item.get("location", ""))
        distance = zip_distance_miles(zip_m.group(1)) if zip_m else None
        if distance is not None and distance > RADIUS: continue
        if not item["trim"]:
            trim_m = re.search(r'\b(premium|limited|sti|sport|base)\b', dt, re.I)
            item["trim"] = trim_m.group(0).title() if trim_m else ""
        results.append({
            "id": f"eb_{iid}", "vin": vin,
            "title": f"{item['year']} {SEARCH_MAKE} {SEARCH_MODEL} {item['trim']}".strip(),
            "year": item["year"], "trim": item["trim"], "price": item["price"],
            "mileage": mileage, "color": item.get("color", ""),
            "color_str": item.get("color", ""),
            "location": item.get("location", "N/A"),
            "distance": int(distance) if distance is not None else None,
            "deal": "", "url": href, "source": "eBay Motors",
        })
    detail_page.close()
    return results


# ── auto.dev API ──────────────────────────────────────────────────────────────
# Uses SEARCH_MAKE, SEARCH_MODEL, and location config.
# Free API key at https://auto.dev

def scrape_autodev():
    if not AD_API_KEY:
        print("[auto.dev] No API key — skipping. Get one free at https://auto.dev", file=sys.stderr)
        return []
    records = []
    params = {
        "make": SEARCH_MAKE,
        "model": SEARCH_MODEL,
        "year_min": MIN_YEAR,
        "year_max": MAX_YEAR,
        "mileage_max": MAX_MILES,
        "zip": ZIP,
        "radius": RADIUS,
        "condition": "used",
        "per_page": 50,
        "page": 1,
    }
    url = "https://auto.dev/api/listings?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {AD_API_KEY}",
        })
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        records.extend(data.get("records", data.get("listings", [])))
    except Exception as e:
        print(f"[auto.dev] API error: {e}", file=sys.stderr)
    print(f"[auto.dev] Raw listings: {len(records)}", file=sys.stderr)
    results = []
    for item in records:
        try: year = int(item.get("year"))
        except Exception: continue
        try: mileage = int(str(item.get("mileageUnformatted") or item.get("mileage") or "").replace(",", "").split()[0])
        except Exception: mileage = None
        try: price = int(str(item.get("priceUnformatted") or item.get("price") or "").replace(",", "").replace("$", ""))
        except Exception: price = None
        trim = item.get("trim", "") or ""
        color = item.get("displayColor", "") or item.get("exterior_color", "") or ""
        city, state = item.get("city", ""), item.get("state", "")
        location = f"{city}, {state}".strip(", ") if (city or state) else "N/A"
        vin = item.get("vin", "")
        lid = vin or str(item.get("id", ""))
        vdp_path = item.get("hrefTarget", "") or item.get("vdpUrl", "")
        url_vdp = f"https://auto.dev{vdp_path}" if vdp_path and vdp_path.startswith("/") else (vdp_path or f"https://auto.dev/listings/{lid}")
        if not (MIN_YEAR <= year <= MAX_YEAR): continue
        if mileage is not None and mileage > MAX_MILES: continue
        if not trim_matches(trim): continue
        if not color_matches_str(color): continue
        distance = None
        try:
            lat = float(item.get("latitude") or item.get("lat") or 0)
            lon = float(item.get("longitude") or item.get("lng") or item.get("lon") or 0)
            if lat and lon:
                distance = int(haversine_miles(ORIGIN_LAT, ORIGIN_LON, lat, lon))
                if distance > RADIUS: continue
        except Exception: pass
        results.append({
            "id": f"ad_{lid}", "vin": vin if len(vin) == 17 else "",
            "title": f"{year} {item.get('make', SEARCH_MAKE)} {item.get('model', SEARCH_MODEL)} {trim}".strip(),
            "year": year, "trim": trim, "price": price, "mileage": mileage,
            "color": color or "Unknown", "color_str": color,
            "location": location, "distance": distance, "deal": "",
            "url": url_vdp, "source": "auto.dev",
        })
    return results


# ── Main scrape ───────────────────────────────────────────────────────────────

def scrape():
    os.environ.setdefault("DISPLAY", ":0")
    os.environ.setdefault("WAYLAND_DISPLAY", "wayland-0")
    os.environ.setdefault("XDG_RUNTIME_DIR", "/mnt/wslg/runtime-dir")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        cg_results = []
        if ENABLE_CARGURUS:
            for cg_url in filter(None, [CARGURUS_URL, CARGURUS_URL_2]):
                page = ctx.new_page()
                try: cg_results += scrape_cargurus(page, cg_url)
                except Exception as e: print(f"[CarGurus] Failed: {e}", flush=True)

        cl_results = []
        if ENABLE_CRAIGSLIST:
            try: cl_results = scrape_craigslist(ctx)
            except Exception as e: print(f"[Craigslist] Failed: {e}", flush=True)

        cd_results = []
        if ENABLE_CARSDOTCOM and CARSDOTCOM_URL:
            try:
                cd_ctx = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
                    viewport={"width": 1366, "height": 768},
                    locale="en-US",
                    extra_http_headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                        "sec-ch-ua": '"Microsoft Edge";v="125", "Chromium";v="125", "Not.A/Brand";v="99"',
                        "sec-ch-ua-mobile": "?0",
                        "sec-ch-ua-platform": '"Windows"',
                    },
                )
                cd_ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
                page2 = cd_ctx.new_page()
                cd_results = scrape_carsdotcom(page2)
                cd_ctx.close()
            except Exception as e: print(f"[Cars.com] Failed: {e}", flush=True)
        elif ENABLE_CARSDOTCOM:
            print("[Cars.com] Skipping — CARSDOTCOM_URL not set", file=sys.stderr)

        at_results = []
        if ENABLE_AUTOTRADER and AUTOTRADER_URL:
            try:
                page3 = ctx.new_page()
                at_results = scrape_autotrader(page3)
            except Exception as e:
                print(f"[AutoTrader] Failed: {e}", flush=True)
                at_results = []
        elif ENABLE_AUTOTRADER:
            print("[AutoTrader] Skipping — AUTOTRADER_URL not set", file=sys.stderr)

        fb_results = []
        if ENABLE_FACEBOOK and os.path.exists(FB_SESSION_FILE):
            try:
                fb_ctx = browser.new_context(
                    storage_state=FB_SESSION_FILE,
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                )
                fb_ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
                fb_results = scrape_facebook(fb_ctx)
                fb_ctx.close()
            except Exception as e: print(f"[Facebook] Failed: {e}", flush=True)
        elif ENABLE_FACEBOOK:
            print("[Facebook] Skipping — no session file. Run fb-auth-setup.py to enable.", file=sys.stderr)

        eb_results = []
        if ENABLE_EBAY:
            eb_results = fetch_ebay_api() or scrape_ebay(ctx)

        browser.close()

    ad_results = scrape_autodev() if ENABLE_AUTODEV else []

    # Deduplicate by VIN, then by (year, mileage, price) fingerprint
    seen_vins, seen_fp, all_results = set(), set(), []
    def _norm(v):
        try: return int(round(float(v))) if v is not None else None
        except (TypeError, ValueError): return None
    def _norm_price(v):
        try: return int(round(float(v) / 500)) if v is not None else None
        except (TypeError, ValueError): return None
    for r in cg_results + cl_results + cd_results + at_results + ad_results + fb_results + eb_results:
        vin = (r.get("vin") or "").strip().upper()
        fp = (_norm(r.get("year")), _norm(r.get("mileage")), _norm_price(r.get("price")))
        if vin and len(vin) == 17:
            if vin in seen_vins:
                print(f"[Dedup] VIN match -- dropping {r.get('source')} {r.get('id')}", file=sys.stderr)
                continue
            if fp != (None, None, None) and fp in seen_fp:
                print(f"[Dedup] Fingerprint match (VIN path) {fp} -- dropping {r.get('source')} {r.get('id')}", file=sys.stderr)
                continue
            seen_vins.add(vin)
        else:
            if fp in seen_fp and fp != (None, None, None):
                print(f"[Dedup] Fingerprint match {fp} -- dropping {r.get('source')} {r.get('id')}", file=sys.stderr)
                continue
        seen_fp.add(fp)
        all_results.append(r)

    all_results.sort(key=lambda r: r.get("price") or 999999)
    return all_results


# ── Persistence ───────────────────────────────────────────────────────────────

def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            data = json.loads(open(SEEN_FILE).read())
            if isinstance(data, dict):
                return set(data.get("ids", [])), set(data.get("vins", [])), data.get("prices", {})
            return set(data), set(), {}
        except Exception: pass
    return set(), set(), {}


def save_seen(listings):
    ids = sorted({r["id"] for r in listings})
    vins = sorted({r["vin"] for r in listings if r.get("vin") and len(r["vin"]) == 17})
    prices = {r["id"]: r["price"] for r in listings if r.get("price")}
    with open(SEEN_FILE, "w") as f:
        json.dump({"ids": ids, "vins": vins, "prices": prices}, f, indent=2)


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[Telegram] TG_BOT_TOKEN or TG_CHAT_ID not set — skipping", file=sys.stderr)
        return
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if TG_TOPIC_ID:
        payload["message_thread_id"] = int(TG_TOPIC_ID)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data=data, headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        print("[Telegram] Sent", file=sys.stderr)
    except Exception as e:
        print(f"[Telegram] Error: {e}", file=sys.stderr)


def format_tg(r):
    price = f"${r['price']:,}" if r.get("price") else "N/A"
    miles = f"{r['mileage']:,} mi" if r.get("mileage") else "N/A"
    dist = f" · {r['distance']} mi away" if r.get("distance") is not None else ""
    deal = f" [{r['deal']}]" if r.get("deal") else ""
    return (
        f"🚗 <b>{r.get('title','(no title)')}</b>{deal}\n"
        f"💰 {price}  📏 {miles}\n"
        f"🎨 {r.get('color','?')}  📍 {r.get('location','?')}{dist}\n"
        f"🔗 {r['url']}  <i>({r.get('source','')})</i>"
    )


def print_result(r, tag=""):
    price = f"${r['price']:,}" if r.get("price") else "N/A"
    miles = f"{r['mileage']:,} mi" if r.get("mileage") else "N/A"
    dist = f" ({r['distance']} mi away)" if r.get("distance") is not None else ""
    label = f"[{tag}] " if tag else "  "
    print(f"{label}{r.get('title','(no title)')}  [{r.get('source','')}]")
    print(f"    Price:  {price}  |  Mileage: {miles}")
    print(f"    Color:  {r.get('color','?')}")
    print(f"    Where:  {r.get('location','?')}{dist}")
    print(f"    Deal:   {r.get('deal','?')}")
    print(f"    URL:    {r['url']}")
    print()


def score_deals(listings):
    """Fill in deal rating for listings missing one, based on price vs median of all priced listings."""
    prices = [r["price"] for r in listings if r.get("price")]
    if not prices:
        return listings
    median = sorted(prices)[len(prices) // 2]
    for r in listings:
        if r.get("deal") or not r.get("price"):
            continue
        pct = (r["price"] - median) / median
        if pct <= -0.10:
            r["deal"] = "Great Deal"
        elif pct <= -0.04:
            r["deal"] = "Good Deal"
        elif pct <= 0.04:
            r["deal"] = "Fair Deal"
        elif pct <= 0.12:
            r["deal"] = "High Priced"
        else:
            r["deal"] = "Overpriced"
    return listings


def main():
    parser = argparse.ArgumentParser(description="Multi-source used car search with Telegram alerts")
    parser.add_argument("--notify", action="store_true", help="Send Telegram summary")
    parser.add_argument("--all",    action="store_true", help="Treat all listings as new")
    args = parser.parse_args()

    listings = scrape()
    listings = score_deals(listings)
    seen_ids, seen_vins, seen_prices = load_seen()
    current_ids = {r["id"] for r in listings}
    price_drops = {}
    for r in listings:
        old = seen_prices.get(r["id"])
        if old and r.get("price") and r["price"] < old:
            price_drops[r["id"]] = old - r["price"]

    def is_new(r):
        if r["id"] in seen_ids: return False
        vin = (r.get("vin") or "").strip().upper()
        if vin and len(vin) == 17 and vin in seen_vins: return False
        return True

    new_listings = listings if args.all else [r for r in listings if is_new(r)]
    sold_ids = seen_ids - current_ids

    print(f"\n{'='*65}")
    print(f"  {SEARCH_MAKE} {SEARCH_MODEL} {MIN_YEAR}-{MAX_YEAR} | <{MAX_MILES//1000}k mi | {RADIUS}mi of {ZIP}")
    print(f"  {len(listings)} active | {len(new_listings)} new | {len(sold_ids)} sold/removed | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*65}\n")

    if not listings:
        print("  No matching listings found.")
    else:
        for r in listings:
            tag = "NEW" if is_new(r) else ("DRP" if r["id"] in price_drops else "   ")
            print_result(r, tag)
            if r["id"] in price_drops:
                print(f"    📉 Price dropped ${price_drops[r['id']]:,} since last run\n")

    if sold_ids:
        print(f"  ── {len(sold_ids)} previously seen listing(s) no longer found ──\n")

    if args.notify:
        header = f"🚗 <b>{SEARCH_MAKE} {SEARCH_MODEL} Search</b> — {datetime.now().strftime('%b %d, %Y')}\n"
        if not listings:
            send_telegram(header + "No active listings found today.")
        else:
            BATCH = 5
            for batch_start in range(0, len(listings), BATCH):
                batch = listings[batch_start:batch_start + BATCH]
                lines = []
                if batch_start == 0:
                    lines.append(header)
                    lines.append(f"<b>✅ {len(listings)} active listing(s):</b>\n")
                for r in batch:
                    prefix = "🆕 " if is_new(r) else ""
                    drop = price_drops.get(r["id"])
                    drop_str = f" 📉 <b>-${drop:,}</b>" if drop else ""
                    lines.append(prefix + format_tg(r) + drop_str)
                    lines.append("")
                send_telegram("\n".join(lines))
        if sold_ids:
            send_telegram(f"<b>❌ {len(sold_ids)} listing(s) sold/removed since last check</b>")

    save_seen(listings)


if __name__ == "__main__":
    main()
