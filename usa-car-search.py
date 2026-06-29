#!/usr/bin/env python3
"""
YOUR_SEARCH_MODEL search — 2019-2021, black/grey, <65k mi, 200mi of 14450.
Scrapes CarGurus, Cars.com, and Craigslist (multi-region) with real Playwright Chromium.
Sends Telegram alerts for new listings.
Usage: python3 wrx-search.py [--notify] [--all]
"""

import json, re, sys, os, argparse, urllib.request, urllib.parse, math, signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# Your City (YOUR_ZIP) lat/lon
ORIGIN_LAT = 43.1048
ORIGIN_LON = -77.2767

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
            if r is not None and not (r.latitude != r.latitude):  # NaN check
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
        """Find state and distance for a city name by searching all ZIPs within radius."""
        try:
            df = _nomi._data
            matches = df[df['place_name'].str.lower() == city_name.lower()]
            best = None
            for _, row in matches.iterrows():
                if row.latitude != row.latitude or row.longitude != row.longitude:
                    continue
                d = haversine_miles(ORIGIN_LAT, ORIGIN_LON, float(row.latitude), float(row.longitude))
                if d is not None and d <= RADIUS_MILES:
                    if best is None or d < best[1]:
                        best = (str(row.state_code), int(d))
            return best  # (state, distance) or None
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

def zip_distance_miles(zipcode):
    """Return haversine miles from Rochester to a ZIP, or None if unknown."""
    lat, lon = zip_to_latlon(zipcode)
    if lat is None:
        return None
    return haversine_miles(ORIGIN_LAT, ORIGIN_LON, lat, lon)

def nhtsa_decode_vin(vin):
    """Query NHTSA free VIN decoder. Returns dict of decoded fields or {}."""
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
            "color": "",  # NHTSA doesn't have color
        }
    except Exception:
        return {}

from playwright.sync_api import sync_playwright
try:
    from playwright_stealth import stealth_sync
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

ZIP = os.environ.get("SEARCH_ZIP", "YOUR_ZIP")
RADIUS = 200
RADIUS_MILES = RADIUS
MAX_MILES = 65000
MIN_YEAR = 2019
MAX_YEAR = 2021

SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wrx-seen.json")

TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "-1003765105884")
TG_TOPIC_ID = os.environ.get("TG_TOPIC_ID", "966")

AD_API_KEY = os.environ.get("AUTODEV_API_KEY", "YOUR_AUTODEV_API_KEY_HERE")

FB_SESSION_FILE = "fb-session.json"
FB_SEARCH_URL = (
    "https://www.facebook.com/marketplace/rochester/search/"
    "?query=subaru+wrx&categoryID=vehicles"
    "&minPrice=15000&maxPrice=40000"
    "&radius=321"  # ~200 miles in km
)
FB_SEARCH_STI_URL = (
    "https://www.facebook.com/marketplace/rochester/search/"
    "?query=subaru+wrx+sti&categoryID=vehicles"
    "&minPrice=15000&maxPrice=40000"
    "&radius=321"
)
AD_RADIUS = 200  # full radius — auto.dev supports it

# d2292 = WRX, d2293 = WRX STI — search both via the WRX page (STI appears too)
CARGURUS_URL = (
    "https://www.cargurus.com/Cars/l-Used-Subaru-WRX-d2292"
    f"?zip={ZIP}&distance={RADIUS}"
    f"&minYear={MIN_YEAR}&maxYear={MAX_YEAR}"
    f"&maxMileage={MAX_MILES}"
    "&sortDir=ASC&sortType=PRICE"
)
CARGURUS_STI_URL = (
    "https://www.cargurus.com/Cars/l-Used-Subaru-WRX-STI-d2341"
    f"?zip={ZIP}&distance={RADIUS}"
    f"&minYear={MIN_YEAR}&maxYear={MAX_YEAR}"
    f"&maxMileage={MAX_MILES}"
    "&transmissionTypes=MANUAL"
    "&sortDir=ASC&sortType=PRICE"
)

# Craigslist regions within ~200mi of Your City (YOUR_ZIP)
CL_REGIONS = ["rochester", "buffalo", "syracuse", "albany", "twintiers"]
CL_QUERY = "subaru+wrx"
CL_MIN_PRICE = 15000
CL_MAX_PRICE = 35000

AUTOTRADER_URL = (
    "https://www.autotrader.com/cars-for-sale/used-cars/subaru/wrx/"
    f"rochester-ny-{ZIP}"
    f"?startYear={MIN_YEAR}&endYear={MAX_YEAR}"
    f"&searchRadius={RADIUS}"
    "&makeCode=SUB&modelCode=SUBWRX"
    "&extColorSimple=BLACK&extColorSimple=GRAY"
    "&listingType=USED&sortBy=distanceASC"
)

CARSDOTCOM_URL = (
    "https://www.cars.com/shopping/results/"
    "?stock_type=used&makes[]=subaru&models[]=subaru-wrx&models[]=subaru-wrx_sti"
    f"&zip={ZIP}&maximum_distance={RADIUS}"
    f"&year_min={MIN_YEAR}&year_max={MAX_YEAR}"
    f"&mileage_max={MAX_MILES}"
    "&sort=list_price"
)


def color_matches_str(color_str, allow_unknown=False):
    if not color_str:
        return allow_unknown
    c = color_str.lower()
    return any(kw in c for kw in ["black", "gray", "grey", "charcoal", "dark", "obsidian", "magnetic", "graphite"])


def trim_matches(trim_name):
    return True  # all trims allowed


# ── CarGurus ────────────────────────────────────────────────────────────────

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
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(big_script[start : start + i + 1])
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
                            "priceData": {"localizedPrice": rec.get("priceTitle"), "price": rec.get("price")},
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
                            "_from_rec": True,
                        })
            except Exception as e:
                print(f"[warn] cg rec parse: {e}", file=sys.stderr)

    return listings


def _cg_extract_dom_cards(page):
    """Parse visible listing cards from CarGurus DOM — catches older inventory the JSON misses."""
    cards = page.evaluate("""() => {
        const results = [];
        const links = Array.from(document.querySelectorAll('a[href*="/details/"]'));
        const seen = new Set();
        links.forEach(a => {
            const idMatch = a.href.match(/\\/details\\/(\\d+)/);
            const lid = idMatch ? idMatch[1] : '';
            if (!lid || seen.has(lid)) return;
            seen.add(lid);
            // Walk up to find the card container (3 levels up from the link)
            let card = a;
            for (let i = 0; i < 3; i++) {
                if (card.parentElement) card = card.parentElement;
            }
            const text = card.innerText || '';
            results.push({
                _dom: true,
                _lid: lid,
                _href: a.href,
                _text: text,
            });
        });
        return results;
    }""")
    return cards or []


def _cg_parse_dom(card):
    """Parse a DOM card dict into the standard listing format."""
    text = card.get('_text', '')
    lid = card.get('_lid', '')
    href = card.get('_href', '')

    year_m = re.search(r'\b(201[89]|202[01])\b', text)
    if not year_m:
        return None
    year = int(year_m.group(1))

    trim_m = re.search(r'\b(premium|limited|sti|sport|base)\b', text, re.I)
    trim = trim_m.group(0).title() if trim_m else ''

    # Find the largest dollar amount — actual price, not the small shipping fee
    price_matches = re.findall(r'\$([\d,]+)', text)
    prices = [int(p.replace(',', '')) for p in price_matches]
    price = max(prices) if prices else None

    # Match mileage — "XX,XXX mi" but not "X mi away" (distance) or "/mo" (payment)
    miles_m = re.search(r'([\d,]{4,})\s+mi\b(?!\s*away)', text)
    mileage = int(miles_m.group(1).replace(',', '')) if miles_m else None

    # Deal rating from card text
    deal = ''
    for d in ['Great Deal', 'Good Deal', 'Fair Deal', 'High Priced', 'Overpriced']:
        if d in text:
            deal = d
            break

    # Location — look for city, state pattern or "X mi away"
    loc_m = re.search(r'(\d+) mi away', text)
    distance = int(loc_m.group(1)) if loc_m else None

    if not lid:
        return None
    url = f"https://www.cargurus.com/details/{lid}"

    return {
        'id': f'cg_{lid}',
        'title': f"{year} Subaru WRX {trim}".strip(),
        'year': year,
        'trim': trim,
        'price': price,
        'mileage': mileage,
        'color': 'Unknown',   # color not in card text; will be fetched if needed
        'color_str': '',
        'location': 'N/A',
        'distance': distance,
        'deal': deal,
        'url': url,
        'source': 'CarGurus',
    }


def _cg_parse(data):
    onto = data.get("ontologyData") or {}
    mileage_data = data.get("mileageData") or {}
    price_data = data.get("priceData") or {}
    seller = data.get("sellerData") or {}
    color_data = data.get("exteriorColorData") or {}

    year_str = onto.get("carYear") or ""
    try:
        year = int(year_str)
    except Exception:
        return None

    mileage = mileage_data.get("value")
    if mileage is None:
        localized = data.get("localizedMileage", "").replace(",", "")
        try:
            mileage = int(localized)
        except Exception:
            mileage = None

    price = price_data.get("current") or price_data.get("price") or price_data.get("totalPrice")
    if price and isinstance(price, str):
        price = int(re.sub(r"[^\d]", "", price)) or None
    elif price:
        price = int(price)

    trim = onto.get("trimName") or ""
    title = data.get("listingTitle") or f"{year} {onto.get('makeName','')} {onto.get('modelName','')} {trim}".strip()

    city = seller.get("city") or seller.get("cityRegion") or ""
    state = seller.get("stateAbbreviation") or seller.get("state") or seller.get("region") or ""
    seller_zip = seller.get("postalCode") or seller.get("zip") or ""

    distance = data.get("distance")
    if isinstance(distance, float):
        distance = int(distance)

    # If state missing, try ZIP lookup first
    if not state and seller_zip:
        _, s = zip_to_city_state(str(seller_zip)[:5])
        if s:
            state = s
    # If state still missing but we have city, search all ZIPs for that city within radius
    if (not state or distance is None) and city:
        result = city_to_state_and_distance(city)
        if result:
            if not state:
                state = result[0]
            if distance is None:
                distance = result[1]
    # Final fallback: ZIP-based distance
    if distance is None and seller_zip:
        d = zip_distance_miles(str(seller_zip)[:5])
        if d is not None:
            distance = int(d)

    location = f"{city}, {state}".strip(", ") if (city or state) else "N/A"

    lid = data.get("id") or data.get("listingId")
    color_name = color_data.get("name") or "Unknown"
    color_norm = (color_data.get("normalized") or "").upper()

    return {
        "id": f"cg_{lid}",
        "title": title,
        "year": year,
        "trim": trim,
        "price": price,
        "mileage": mileage,
        "color": color_name,
        "color_str": f"{color_norm} {color_name}",
        "location": location,
        "distance": distance,
        "deal": (data.get("dealRating") or "").replace("_", " ").title(),
        "url": f"https://www.cargurus.com/details/{lid}",
        "source": "CarGurus",
    }


def scrape_cargurus(page, url=None):
    if url is None:
        url = CARGURUS_URL
    label = "STI" if "STI" in url else "WRX"
    print(f"[CarGurus/{label}] Loading...", file=sys.stderr)

    intercepted = []

    def handle_response(response):
        url = response.url
        if "cargurus.com" in url and ("listings" in url or "inventory" in url or "searchResults" in url):
            try:
                body = response.json()
                intercepted.append(body)
            except Exception:
                pass

    page.on("response", handle_response)
    page.goto(url, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(3000)
    for scroll_y in [800, 2000, 4000, 6000, 8000]:
        page.evaluate(f"window.scrollTo(0, {scroll_y})")
        page.wait_for_timeout(1200)

    # Try to extract from intercepted API responses first
    api_listings = []
    for body in intercepted:
        # CarGurus API returns {"listings": [...]} or {"data": {"listings": [...]}}
        listings_data = None
        if isinstance(body, dict):
            listings_data = body.get("listings") or (body.get("data") or {}).get("listings")
        if listings_data and isinstance(listings_data, list):
            api_listings.extend(listings_data)

    if api_listings:
        print(f"[CarGurus] API listings captured: {len(api_listings)}", file=sys.stderr)
        raw = api_listings
    else:
        raw = _cg_extract_listings(page)

    print(f"[CarGurus] Raw listings: {len(raw)}", file=sys.stderr)

    # Also parse visible DOM cards — catches older inventory not in embedded JSON
    dom_cards = _cg_extract_dom_cards(page)
    print(f"[CarGurus] DOM cards: {len(dom_cards)}", file=sys.stderr)

    results = []
    seen_ids = set()

    # Process DOM cards — visit detail page for each to get color and location
    dom_candidates = []
    for card in dom_cards:
        # Must mention WRX in the card text — filters out sidebar/recommended cars
        card_text = card.get('_text', '').lower()
        if 'wrx' not in card_text:
            continue
        parsed = _cg_parse_dom(card)
        if not parsed:
            continue
        if not (MIN_YEAR <= parsed["year"] <= MAX_YEAR):
            continue
        if parsed["mileage"] is not None and parsed["mileage"] > MAX_MILES:
            continue
        if not trim_matches(parsed["trim"]):
            continue
        dist = parsed.get("distance")
        if dist is not None and dist > RADIUS:
            continue
        dom_candidates.append(parsed)

    print(f"[CarGurus] DOM candidates (pre-color): {len(dom_candidates)}", file=sys.stderr)

    detail_page = page.context.new_page()
    for parsed in dom_candidates:
        try:
            detail_page.goto(parsed["url"], wait_until="domcontentloaded", timeout=30000)
            detail_page.wait_for_timeout(2000)
            detail_text = detail_page.inner_text("body") or ""
        except Exception as e:
            print(f"[CarGurus] Detail fetch failed for {parsed['id']}: {e}", file=sys.stderr)
            # include without color/location rather than drop
            if parsed["id"] not in seen_ids:
                seen_ids.add(parsed["id"])
                results.append(parsed)
            continue

        # Verify it's actually a Subaru WRX
        if 'wrx' not in detail_text.lower() or 'subaru' not in detail_text.lower():
            print(f"[CarGurus] skip {parsed['id']} not a WRX (detail page check)", file=sys.stderr)
            continue

        # Color
        color_m = re.search(r"exterior colou?r[:\s]+([^\n·\|]+)", detail_text, re.I)
        if color_m:
            color_raw = color_m.group(1).strip()
        else:
            color_m2 = re.search(r"\b(Crystal Black|Magnetite Gray|Dark Gray|WR Blue|Ice Silver|Pure Red|Crystal White|Ceramic White|Lapis Blue|Subaru Blue|Solar Orange)\b", detail_text, re.I)
            color_raw = color_m2.group(0) if color_m2 else ""

        if color_raw and not color_matches_str(color_raw):
            print(f"[CarGurus] skip {parsed['id']} color='{color_raw}'", file=sys.stderr)
            continue

        # Distance — look for "X miles away" or "X mi away" on detail page first
        if parsed.get("distance") is None:
            dist_m = re.search(r"([\d,]+)\s*mi(?:les?)?\s*away", detail_text, re.I)
            if dist_m:
                parsed["distance"] = int(dist_m.group(1).replace(",", ""))
        # Try lat/lon from page source for precise haversine check
        if parsed.get("distance") is None:
            page_src = detail_page.content()
            lat_m = re.search(r'"latitude"\s*:\s*([\d.-]+)', page_src)
            lon_m = re.search(r'"longitude"\s*:\s*([\d.-]+)', page_src)
            if lat_m and lon_m:
                try:
                    dist = haversine_miles(ORIGIN_LAT, ORIGIN_LON, float(lat_m.group(1)), float(lon_m.group(1)))
                    parsed["distance"] = int(dist)
                except Exception:
                    pass

        # If still no distance, use state to filter obvious out-of-range listings
        # States within ~200mi of Your City (YOUR_ZIP): NY, PA, NJ, CT, MA, VT, NH, ME, RI, MD, DE
        if parsed.get("distance") is None:
            state_m = re.search(r",\s*([A-Z]{2})\b", detail_text)
            if state_m:
                state = state_m.group(1)
                IN_RANGE_STATES = {"NY", "PA", "NJ", "CT", "MA", "VT", "NH", "ME", "RI", "MD", "DE"}
                if state not in IN_RANGE_STATES:
                    print(f"[CarGurus] skip {parsed['id']} out-of-range state={state}", file=sys.stderr)
                    continue

        if parsed.get("distance") is not None and parsed["distance"] > RADIUS:
            print(f"[CarGurus] skip {parsed['id']} dist={parsed['distance']}", file=sys.stderr)
            continue

        # Location — city, ST pattern
        loc_m = re.search(r"([A-Z][a-zA-Z\s]+),\s*([A-Z]{2})\b", detail_text)
        if loc_m:
            parsed["location"] = f"{loc_m.group(1).strip()}, {loc_m.group(2)}"
        parsed["color"] = color_raw or "Unknown"
        parsed["color_str"] = color_raw

        if parsed["id"] not in seen_ids:
            seen_ids.add(parsed["id"])
            results.append(parsed)
            print(f"[CarGurus] Match: {parsed['id']} color='{color_raw}' dist={parsed.get('distance')} loc='{parsed['location']}'", file=sys.stderr)

    detail_page.close()
    print(f"[CarGurus] DOM matches: {len(results)}", file=sys.stderr)
    for item in raw:
        parsed = _cg_parse(item)
        if not parsed:
            continue
        if not (MIN_YEAR <= parsed["year"] <= MAX_YEAR):
            print(f"[CarGurus] skip year={parsed['year']} trim={parsed['trim']!r} color={parsed['color_str']!r}", file=sys.stderr)
            continue
        if parsed["mileage"] is not None and parsed["mileage"] > MAX_MILES:
            print(f"[CarGurus] skip miles={parsed['mileage']} trim={parsed['trim']!r} color={parsed['color_str']!r}", file=sys.stderr)
            continue
        if not color_matches_str(parsed["color_str"], allow_unknown=True):
            print(f"[CarGurus] skip color={parsed['color_str']!r} trim={parsed['trim']!r}", file=sys.stderr)
            continue
        if not trim_matches(parsed["trim"]):
            print(f"[CarGurus] skip trim={parsed['trim']!r} color={parsed['color_str']!r}", file=sys.stderr)
            continue
        dist = parsed.get("distance")
        if dist is not None and dist > RADIUS:
            print(f"[CarGurus] skip dist={dist} trim={parsed['trim']!r}", file=sys.stderr)
            continue
        if parsed["id"] not in seen_ids:
            seen_ids.add(parsed["id"])
            results.append(parsed)

    return results


# ── AutoTrader ───────────────────────────────────────────────────────────────

# ── Craigslist ────────────────────────────────────────────────────────────────

def _cl_scrape_region(page, region):
    url = (
        f"https://{region}.craigslist.org/search/cta"
        f"?query={CL_QUERY}&min_price={CL_MIN_PRICE}&max_price={CL_MAX_PRICE}"
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
    """Visit a CL detail page to get VIN, exterior color, and geo distance. Returns None to reject."""
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

    # VIN
    vin_m = re.search(r'\bVIN[:\s]+([A-HJ-NPR-Z0-9]{17})\b', detail_text, re.I)
    if vin_m:
        listing["vin"] = vin_m.group(1).upper()

    # Exterior color from attributes table ("paint color: X")
    color_label_m = re.search(r"paint colou?r[:\s]+([^\n]+)", detail_text, re.I)
    if color_label_m:
        color_raw = color_label_m.group(1).strip()
        if "custom" in color_raw.lower():
            print(f"[CL detail] skip {listing['id']} color='custom'", file=sys.stderr)
            return None
        listing["color"] = color_raw
        listing["color_str"] = color_raw
        if not color_matches_str(color_raw):
            print(f"[CL detail] skip {listing['id']} color='{color_raw}'", file=sys.stderr)
            return None

    # Geo distance from embedded lat/lon in map link
    lat_m = re.search(r'data-latitude="([\d.-]+)"', page.content())
    lon_m = re.search(r'data-longitude="([\d.-]+)"', page.content())
    if lat_m and lon_m:
        try:
            lat = float(lat_m.group(1))
            lon = float(lon_m.group(1))
            dist = haversine_miles(ORIGIN_LAT, ORIGIN_LON, lat, lon)
            listing["distance"] = int(dist)
            if dist > RADIUS:
                print(f"[CL detail] skip {listing['id']} dist={dist:.0f}mi ({listing.get('location','')})", file=sys.stderr)
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

    print(f"[Craigslist] Raw listings across {len(CL_REGIONS)} regions: {len(all_raw)}", file=sys.stderr)

    candidates = []
    for region, item in all_raw:
        title = item.get("title", "")
        meta = item.get("meta", "")
        price_txt = item.get("price", "")
        pid = item.get("pid", "")
        href = item.get("href", "")
        loc = item.get("location") or region.title()

        # Must mention WRX
        if "wrx" not in title.lower():
            continue

        # Year from title
        year_m = re.search(r"\b(2019|202[01])\b", title)
        if not year_m:
            continue
        year = int(year_m.group(1))

        # Mileage from meta ("81k mi" or "81,234 mi")
        mi_m = re.search(r"([\d,]+)k?\s*mi", meta, re.I)
        if mi_m:
            mi_str = mi_m.group(1).replace(",", "")
            mileage = int(mi_str) * 1000 if "k" in meta[mi_m.start():mi_m.end()].lower() else int(mi_str)
        else:
            mileage = None

        try:
            price = int(re.sub(r"[^\d]", "", price_txt)) if price_txt else None
        except Exception:
            price = None

        # Trim from title
        trim_m = re.search(r"\b(premium|limited|sti|sport|base)\b", title, re.I)
        trim = trim_m.group(0).title() if trim_m else ""

        # Color from title — if title mentions a non-matching color, skip early
        color_m = re.search(
            r"\b(black|gray|grey|silver|white|blue|red|green|orange|yellow|brown|graphite|charcoal)\b",
            title, re.I
        )
        color = color_m.group(0).title() if color_m else ""

        # Filter
        if mileage is not None and mileage > MAX_MILES:
            continue
        if not trim_matches(trim):
            continue
        if color and not color_matches_str(color):
            continue

        candidates.append({
            "id": f"cl_{pid}",
            "vin": "",
            "title": title,
            "year": year,
            "trim": trim,
            "price": price,
            "mileage": mileage,
            "color": color or "Unknown",
            "color_str": color,
            "location": loc,
            "distance": None,
            "deal": "",
            "url": href,
            "source": f"Craigslist/{region}",
        })

    print(f"[Craigslist] Candidates before detail check: {len(candidates)}", file=sys.stderr)

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


# ── Cars.com ─────────────────────────────────────────────────────────────────

def _cd_extract_listings(page):
    """Extract listings from Cars.com data-vehicle-details attributes."""
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
        if not lid or lid in seen:
            continue
        seen.add(lid)
        try:
            details = json.loads(item["details"])
        except Exception:
            continue
        details["_listing_id"] = lid
        details["_href"] = item.get("href", "")
        listings.append(details)

    return listings


def _cd_parse(data):
    year_str = data.get("year", "")
    try:
        year = int(year_str)
    except Exception:
        return None

    try:
        price = int(str(data.get("price", "") or "").replace(",", ""))
    except Exception:
        price = None

    try:
        mileage = int(str(data.get("mileage", "") or "").replace(",", ""))
    except Exception:
        mileage = None

    trim = data.get("trim", "")
    make = data.get("make", "Subaru")
    model = data.get("model", "WRX")
    title = f"{year} {make} {model} {trim}".strip()
    color = data.get("exteriorColor", "Unknown") or "Unknown"

    seller = data.get("seller", {}) or {}
    city = seller.get("city", "")
    state = seller.get("state", "")
    seller_zip = seller.get("zip", "")
    if city or state:
        location = f"{city}, {state}".strip(", ")
    elif seller_zip:
        location = f"ZIP {seller_zip}"
    else:
        location = "N/A"

    lid = data.get("_listing_id", "")
    href = data.get("_href", "")
    url = href if href else f"https://www.cars.com/vehicledetail/{lid}/"

    # Compute distance from ZIP
    distance = None
    zip_to_check = seller_zip or ""
    if zip_to_check:
        distance = zip_distance_miles(zip_to_check)
        if location.startswith("ZIP ") or not (city or state):
            c, s = zip_to_city_state(zip_to_check)
            if c and s:
                location = f"{c}, {s}"

    vin = data.get("vin", "") or ""

    return {
        "id": f"cd_{lid}",
        "vin": vin if len(vin) == 17 else "",
        "title": title,
        "year": year,
        "trim": trim,
        "price": price,
        "mileage": mileage,
        "color": color,
        "color_str": color,
        "location": location,
        "distance": int(distance) if distance is not None else None,
        "deal": "",
        "url": url,
        "source": "Cars.com",
    }


def scrape_carsdotcom(page):
    print("[Cars.com] Loading...", file=sys.stderr)
    if HAS_STEALTH:
        stealth_sync(page)
    try:
        # Visit homepage first to look like a real user, then navigate
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
        if not parsed:
            continue
        if parsed["year"] and not (MIN_YEAR <= parsed["year"] <= MAX_YEAR):
            continue
        if parsed["mileage"] is not None and parsed["mileage"] > MAX_MILES:
            continue
        if not color_matches_str(parsed["color_str"], allow_unknown=True):
            continue
        if not trim_matches(parsed["trim"]):
            continue
        dist = parsed.get("distance")
        if dist is not None and dist > RADIUS:
            print(f"[Cars.com] skip {parsed['id']} dist={dist}mi ({parsed['location']})", file=sys.stderr)
            continue
        # VIN verification via NHTSA if we have a VIN
        vin = parsed.get("vin", "")
        if vin:
            decoded = nhtsa_decode_vin(vin)
            make = (decoded.get("make") or "").upper()
            model = (decoded.get("model") or "").upper()
            if make and "SUBARU" not in make:
                print(f"[Cars.com] skip {parsed['id']} VIN decode says make={make}", file=sys.stderr)
                continue
            if model and "WRX" not in model:
                print(f"[Cars.com] skip {parsed['id']} VIN decode says model={model}", file=sys.stderr)
                continue
        results.append(parsed)

    return results


# ── Facebook Marketplace ─────────────────────────────────────────────────────

def _fb_scrape_url(page, url, label=""):
    """Load one FB Marketplace search URL and return raw card list. Returns None if not logged in."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)
        if "login" in page.url.lower() or "log in" in (page.title() or "").lower():
            return None
        # Dismiss "See more on Facebook" login popup if present
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
            # Also try clicking the X button on the modal
            close_btn = page.query_selector('div[aria-label="Close"]')
            if close_btn:
                close_btn.click()
                page.wait_for_timeout(800)
        except Exception:
            pass
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
            const text = a.innerText || a.textContent || '';
            results.push({ pid, href, text });
        }
        return results;
    }""")
    return raw or []


def scrape_facebook(ctx):
    if not os.path.exists(FB_SESSION_FILE):
        print("[Facebook] No session file found — run fb-auth-setup.py first", file=sys.stderr)
        return []

    page = ctx.new_page()
    wrx_raw = _fb_scrape_url(page, FB_SEARCH_URL, "/WRX")
    if wrx_raw is None:
        print("[Facebook] Session expired — sending alert", file=sys.stderr)
        send_telegram(
            "⚠️ <b>Facebook Marketplace session expired</b>\n"
            "WRX search is running without FB listings.\n\n"
            "To fix: run <code>python3 fb-auth-setup.py</code>"
        )
        page.close()
        return []

    sti_raw = _fb_scrape_url(page, FB_SEARCH_STI_URL, "/STI") or []
    page.close()

    # Merge, dedup by pid
    seen_pids = set()
    raw = []
    for item in (wrx_raw + sti_raw):
        pid = item.get("pid", "")
        if pid and pid not in seen_pids:
            seen_pids.add(pid)
            raw.append(item)

    print(f"[Facebook] Raw listings: {len(raw)} (WRX:{len(wrx_raw)} STI:{len(sti_raw)})", file=sys.stderr)

    # First pass: filter to plausible WRX candidates from card text
    candidates = []
    for item in raw:
        text = item.get("text", "")
        pid = item.get("pid", "")
        href = item.get("href", "")
        t = text.lower()
        if "wrx" not in t:
            continue
        year_m = re.search(r"\b(20\d{2})\b", text)
        if not year_m:
            continue
        year = int(year_m.group(1))
        if not (MIN_YEAR <= year <= MAX_YEAR):
            continue
        price_m = re.search(r"\$\s*([\d,]+)", text)
        price = int(price_m.group(1).replace(",", "")) if price_m else None
        if price and price > CL_MAX_PRICE:
            continue
        candidates.append({"pid": pid, "href": href, "year": year, "price": price})

    print(f"[Facebook] Candidates after card filter: {len(candidates)}", file=sys.stderr)

    # Second pass: visit each listing page to get real mileage, color, trim
    results = []
    detail_page = ctx.new_page()
    for item in candidates:
        pid = item["pid"]
        href = item["href"]
        year = item["year"]
        price = item["price"]
        try:
            detail_page.goto(href, wait_until="domcontentloaded", timeout=30000)
            detail_page.wait_for_timeout(2000)
            detail_text = detail_page.inner_text("body") or ""
        except Exception as e:
            print(f"[Facebook] Detail fetch failed for {pid}: {e}", file=sys.stderr)
            continue

        dt = detail_text

        # Mileage: "Driven X miles" or "X,XXX miles"
        mi_m = re.search(r"(?:driven\s+)?([\d,]+)\s*miles", dt, re.I)
        if mi_m:
            mileage = int(mi_m.group(1).replace(",", ""))
        else:
            mileage = None

        if mileage is not None and mileage > MAX_MILES:
            print(f"[Facebook] skip fb_{pid} miles={mileage}", file=sys.stderr)
            continue

        # Color: look for "Exterior color: X"
        color_label_m = re.search(r"exterior colou?r[:\s]+([^\n·]+)", dt, re.I)
        if color_label_m:
            color_raw = color_label_m.group(1).strip()
        else:
            color_m = re.search(r"\b(black|gray|grey|charcoal|graphite|obsidian|magnetic|dark|white|silver|blue|red|orange)\b", dt, re.I)
            color_raw = color_m.group(0).title() if color_m else ""

        if color_raw and not color_matches_str(color_raw, allow_unknown=True):
            print(f"[Facebook] skip fb_{pid} color='{color_raw}'", file=sys.stderr)
            continue

        # Trim
        trim_m = re.search(r"\b(premium|limited|sti|sport|base)\b", dt, re.I)
        trim = trim_m.group(0).title() if trim_m else ""

        if not trim_matches(trim):
            print(f"[Facebook] skip fb_{pid} trim='{trim}'", file=sys.stderr)
            continue

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
            "id": f"fb_{pid}",
            "vin": "",
            "title": f"{year} Subaru WRX {trim}".strip(),
            "year": year,
            "trim": trim,
            "price": price,
            "mileage": mileage,
            "color": color_raw or "Unknown",
            "color_str": color_raw,
            "location": location,
            "distance": distance,
            "deal": "",
            "url": href,
            "source": "Facebook",
        })
        print(f"[Facebook] Match: fb_{pid} {year} {trim} color='{color_raw}' miles={mileage} price={price}", file=sys.stderr)

    detail_page.close()
    return results


CHROME_CDP_HOST = "172.29.240.1"
CHROME_CDP_PORT = 9222


CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
CHROME_LAUNCH_ARGS = [
    f"--remote-debugging-port=9222",
    "--remote-allow-origins=*",
    "--no-first-run", "--no-default-browser-check",
    "--user-data-dir=C:\\Temp\\chrome-debug",
]


def _chrome_kill():
    """Kill the debug Chrome instance on Windows."""
    import subprocess
    try:
        subprocess.run(
            ["/mnt/c/Windows/System32/cmd.exe", "/c", "taskkill", "/F", "/IM", "chrome.exe"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        __import__("time").sleep(2)
    except Exception:
        pass


def _chrome_launch():
    """Launch Chrome on Windows with CDP enabled. Returns True if launched."""
    import subprocess, os
    for chrome_path in CHROME_PATHS:
        wsl_path = "/mnt/c" + chrome_path[2:].replace("\\", "/")
        if not os.path.exists(wsl_path):
            continue
        try:
            subprocess.Popen(
                ["/mnt/c/Windows/System32/cmd.exe", "/c", "start", "", chrome_path] + CHROME_LAUNCH_ARGS,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return True
        except Exception as e:
            print(f"[AutoTrader] Failed to launch Chrome: {e}", file=sys.stderr)
    return False


def _chrome_cdp_url(force_relaunch=False):
    """Return Chrome CDP URL, launching Chrome on Windows if needed. Waits for full CDP readiness (not just TCP open)."""
    import time
    try:
        import urllib.request as _urlreq
    except ImportError:
        import urllib2 as _urlreq

    def cdp_ready():
        """Check /json/version — confirms Chrome is fully CDP-ready, not just TCP-open."""
        try:
            r = _urlreq.urlopen(f"http://{CHROME_CDP_HOST}:{CHROME_CDP_PORT}/json/version", timeout=2)
            return r.status == 200
        except Exception:
            return False

    if force_relaunch:
        print("[AutoTrader] Relaunching Chrome (stale CDP session)...", file=sys.stderr)
        _chrome_kill()
        time.sleep(2)

    if not force_relaunch and cdp_ready():
        return f"http://{CHROME_CDP_HOST}:{CHROME_CDP_PORT}"

    print("[AutoTrader] Chrome not ready — launching Chrome on Windows...", file=sys.stderr)
    launch_start = time.time()
    if not _chrome_launch():
        print("[AutoTrader] Could not find or launch Chrome on Windows", file=sys.stderr)
        return None

    for i in range(45):
        time.sleep(1)
        if cdp_ready():
            print(f"[AutoTrader] Chrome CDP ready after {i+1}s ({time.time()-launch_start:.1f}s total)", file=sys.stderr)
            return f"http://{CHROME_CDP_HOST}:{CHROME_CDP_PORT}"

    print("[AutoTrader] Chrome launched but CDP not ready after 45s", file=sys.stderr)
    return None


def scrape_autotrader_cdp(pw):
    """Scrape AutoTrader via real Chrome CDP — bypasses bot detection."""
    for attempt in range(2):
        force_relaunch = attempt > 0
        cdp_url = _chrome_cdp_url(force_relaunch=force_relaunch)
        if not cdp_url:
            print("[AutoTrader] Chrome CDP not available", file=sys.stderr)
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
                print(f"[AutoTrader] Bot block even via CDP ({title!r})", file=sys.stderr)
                page.close()
                browser.disconnect()
                return []
            for scroll_y in [800, 2000, 4000]:
                page.evaluate(f"window.scrollTo(0, {scroll_y})")
                page.wait_for_timeout(800)
            results = _autotrader_parse_page(page)
            page.close()
            try:
                browser.disconnect()
            except Exception:
                pass
            return results
        except Exception as e:
            err = str(e)
            print(f"[AutoTrader] CDP error: {err}", file=sys.stderr)
            if "ECONNRESET" in err or "ECONNREFUSED" in err:
                if attempt == 0:
                    print("[AutoTrader] Connection reset — will kill and relaunch Chrome...", file=sys.stderr)
                    continue
            return None
    return None


# ── AutoTrader ───────────────────────────────────────────────────────────────

def _autotrader_parse_page(page):
    """Extract WRX listings from a loaded AutoTrader page."""
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
                        try:
                            listings = _json.loads(raw_json[start:start+i+1])
                        except Exception:
                            pass
                        break

    if listings:
        print(f"[AutoTrader] Raw listings (JSON): {len(listings)}", file=sys.stderr)
        results = []
        for item in listings:
            try:
                year = int(item.get("year", 0))
            except Exception:
                continue
            if not (MIN_YEAR <= year <= MAX_YEAR):
                continue
            try:
                mileage = int(str(item.get("mileage", "") or "").replace(",", ""))
            except Exception:
                mileage = None
            if mileage is not None and mileage > MAX_MILES:
                continue
            try:
                price = int(str(item.get("pricingDetail", {}).get("salePrice") or item.get("price", "") or "").replace(",", "").replace("$", ""))
            except Exception:
                price = None
            trim = (item.get("trim") or "")
            color = (item.get("color") or item.get("exteriorColor") or "")
            if not color_matches_str(color, allow_unknown=True):
                continue
            vin = item.get("vin", "")
            lid = item.get("id") or item.get("listingId") or vin
            owner = item.get("owner", {}) or {}
            city = owner.get("city", "")
            state = owner.get("state", "")
            location = f"{city}, {state}".strip(", ") if (city or state) else "N/A"
            results.append({
                "id": f"at_{lid}",
                "vin": vin,
                "title": f"{year} {item.get('make','Subaru')} {item.get('model','WRX')} {trim}".strip(),
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
            const text = (el.innerText || el.textContent || '').trim().slice(0, 400);
            results.push({ vid, href: a.href.split('?')[0], text });
        }
        return results;
    }""")
    print(f"[AutoTrader] DOM cards: {len(cards)}", file=sys.stderr)
    results = []
    for card in cards:
        text = card.get("text", "")
        href = card.get("href", "")
        vid = card.get("vid", "")
        if "wrx" not in text.lower():
            continue
        year_m = re.search(r"\b(20\d{2})\b", text)
        if not year_m:
            continue
        year = int(year_m.group(1))
        if not (MIN_YEAR <= year <= MAX_YEAR):
            continue
        price_m = re.search(r"\$\s*([\d,]+)", text)
        price = int(price_m.group(1).replace(",", "")) if price_m else None
        mi_m = re.search(r"([\d,]+)\s*mi(?:les)?", text, re.I)
        mileage = int(mi_m.group(1).replace(",", "")) if mi_m else None
        if mileage is not None and mileage > MAX_MILES:
            continue
        trim_m = re.search(r"\b(premium|limited|sti|sport|base)\b", text, re.I)
        trim = trim_m.group(0).title() if trim_m else ""
        color_m = re.search(r"\b(black|gray|grey|charcoal|graphite|obsidian|magnetic|dark)\b", text, re.I)
        color = color_m.group(0).title() if color_m else ""
        dist_m = re.search(r"(\d+)\s*mi(?:les)?\s*away", text, re.I)
        distance = int(dist_m.group(1)) if dist_m else None
        loc_m = re.search(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2})\b", text)
        location = loc_m.group(1) if loc_m else "N/A"
        results.append({
            "id": f"at_{vid}",
            "vin": "",
            "title": f"{year} Subaru WRX {trim}".strip(),
            "year": year, "trim": trim, "price": price, "mileage": mileage,
            "color": color or "Unknown", "color_str": color,
            "location": location, "distance": distance, "deal": "",
            "url": f"https://www.autotrader.com/cars-for-sale/vehicle/{vid}",
            "source": "AutoTrader",
        })
    return results


def _autotrader_filter(listings):
    """Filter raw AutoTrader listings JSON (output from _at_worker.py) into normalized results."""
    results = []
    for item in listings:
        try:
            year = int(item.get("year", 0))
        except Exception:
            continue
        if not (MIN_YEAR <= year <= MAX_YEAR):
            continue
        try:
            mileage = int(str(item.get("mileage", "") or "").replace(",", ""))
        except Exception:
            mileage = None
        if mileage is not None and mileage > MAX_MILES:
            continue
        try:
            price = int(str(item.get("pricingDetail", {}).get("salePrice") or item.get("price", "") or "").replace(",", "").replace("$", ""))
        except Exception:
            price = None
        trim = (item.get("trim") or "")
        color = (item.get("color") or item.get("exteriorColor") or "")
        if not color_matches_str(color, allow_unknown=True):
            continue
        vin = item.get("vin", "")
        lid = item.get("id") or item.get("listingId") or vin
        owner = item.get("owner", {}) or {}
        city = owner.get("city", "")
        state = owner.get("state", "")
        location = f"{city}, {state}".strip(", ") if (city or state) else "N/A"
        results.append({
            "id": f"at_{lid}",
            "vin": vin,
            "title": f"{year} {item.get('make','Subaru')} {item.get('model','WRX')} {trim}".strip(),
            "year": year, "trim": trim, "price": price, "mileage": mileage,
            "color": color or "Unknown", "color_str": color,
            "location": location, "distance": None, "deal": "",
            "url": f"https://www.autotrader.com/cars-for-sale/vehicle/{lid}",
            "source": "AutoTrader",
        })
    return results


def scrape_autotrader(page):
    print("[AutoTrader] Loading...", file=sys.stderr)
    try:
        page.goto(AUTOTRADER_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)
        for scroll_y in [800, 2000, 4000]:
            page.evaluate(f"window.scrollTo(0, {scroll_y})")
            page.wait_for_timeout(1000)
    except Exception as e:
        print(f"[AutoTrader] Load error: {e}", file=sys.stderr)
        return []

    # AutoTrader embeds listing data in a JSON blob in a script tag
    raw_json = page.evaluate("""() => {
        const scripts = Array.from(document.querySelectorAll('script'));
        for (const s of scripts) {
            const txt = s.textContent || '';
            if (txt.includes('"listings"') && txt.includes('"vin"') && txt.length > 10000) return txt;
        }
        return null;
    }""")

    listings = []
    if raw_json:
        m = re.search(r'"listings"\s*:\s*(\[)', raw_json)
        if m:
            start = m.start(1)
            depth = 0
            for i, c in enumerate(raw_json[start:start+500000]):
                if c in "[{": depth += 1
                elif c in "]}":
                    depth -= 1
                    if depth == 0:
                        try:
                            listings = json.loads(raw_json[start:start+i+1])
                        except Exception:
                            pass
                        break

    # Detect bot block page
    page_title = page.title() or ""
    if "unavailable" in page_title.lower() or "blocked" in page_title.lower():
        print(f"[AutoTrader] Bot block detected ({page_title!r}) — skipping", file=sys.stderr)
        return []

    # Fallback: scrape visible listing cards from headed page
    if not listings:
        # Each unique vehicle link is a listing; group by vehicle ID
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
                // Walk up to the card container for full text
                let el = a;
                for (let i = 0; i < 6; i++) { if (el.parentElement) el = el.parentElement; }
                const text = (el.innerText || el.textContent || '').trim().slice(0, 400);
                results.push({ vid, href: a.href.split('?')[0], text });
            }
            return results;
        }""")
        print(f"[AutoTrader] Raw cards: {len(cards)}", file=sys.stderr)
        results = []
        for card in cards:
            text = card.get("text", "")
            href = card.get("href", "")
            vid = card.get("vid", "")
            if "wrx" not in text.lower():
                continue
            year_m = re.search(r"\b(20\d{2})\b", text)
            if not year_m:
                continue
            year = int(year_m.group(1))
            if not (MIN_YEAR <= year <= MAX_YEAR):
                continue
            price_m = re.search(r"\$\s*([\d,]+)", text)
            price = int(price_m.group(1).replace(",", "")) if price_m else None
            mi_m = re.search(r"([\d,]+)\s*mi(?:les)?", text, re.I)
            mileage = int(mi_m.group(1).replace(",", "")) if mi_m else None
            if mileage is not None and mileage > MAX_MILES:
                continue
            trim_m = re.search(r"\b(premium|limited|sti|sport|base)\b", text, re.I)
            trim = trim_m.group(0).title() if trim_m else ""
            if not trim_matches(trim):
                continue
            color_m = re.search(r"\b(black|gray|grey|charcoal|graphite|obsidian|magnetic|dark)\b", text, re.I)
            color = color_m.group(0).title() if color_m else ""
            dist_m = re.search(r"(\d+)\s*mi(?:les)?\s*away", text, re.I)
            distance = int(dist_m.group(1)) if dist_m else None
            loc_m = re.search(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2})\b", text)
            location = loc_m.group(1) if loc_m else "N/A"
            results.append({
                "id": f"at_{vid}",
                "vin": "",
                "title": f"{year} Subaru WRX {trim}".strip(),
                "year": year, "trim": trim, "price": price, "mileage": mileage,
                "color": color or "Unknown", "color_str": color,
                "location": location, "distance": distance, "deal": "",
                "url": f"https://www.autotrader.com/cars-for-sale/vehicle/{vid}",
                "source": "AutoTrader",
            })
        return results

    print(f"[AutoTrader] Raw listings (JSON): {len(listings)}", file=sys.stderr)
    results = []
    for item in listings:
        try:
            year = int(item.get("year", 0))
        except Exception:
            continue
        if not (MIN_YEAR <= year <= MAX_YEAR):
            print(f"[AutoTrader] skip year={year}", file=sys.stderr)
            continue
        try:
            mileage = int(str(item.get("mileage", "") or "").replace(",", ""))
        except Exception:
            mileage = None
        if mileage is not None and mileage > MAX_MILES:
            print(f"[AutoTrader] skip miles={mileage}", file=sys.stderr)
            continue
        try:
            price = int(str(item.get("pricingDetail", {}).get("salePrice") or item.get("price", "") or "").replace(",", "").replace("$", ""))
        except Exception:
            price = None
        trim = (item.get("trim") or "")
        if not trim_matches(trim):
            print(f"[AutoTrader] skip trim={trim!r}", file=sys.stderr)
            continue
        color = (item.get("color") or item.get("exteriorColor") or "")
        if not color_matches_str(color):
            print(f"[AutoTrader] skip color={color!r}", file=sys.stderr)
            continue
        vin = item.get("vin", "")
        lid = item.get("id") or item.get("listingId") or vin
        owner = item.get("owner", {}) or {}
        city = owner.get("city", "")
        state = owner.get("state", "")
        location = f"{city}, {state}".strip(", ") if (city or state) else "N/A"
        url = f"https://www.autotrader.com/cars-for-sale/vehicle/{lid}"
        title = f"{year} {item.get('make','Subaru')} {item.get('model','WRX')} {trim}".strip()
        results.append({
            "id": f"at_{lid}",
            "vin": vin,
            "title": title, "year": year, "trim": trim, "price": price, "mileage": mileage,
            "color": color or "Unknown", "color_str": color,
            "location": location, "distance": None, "deal": "",
            "url": url, "source": "AutoTrader",
        })
    return results


# ── eBay Motors ──────────────────────────────────────────────────────────────

EBAY_SEARCH_URL = (
    "https://www.ebay.com/sch/Cars-Trucks/6001/i.html"
    "?_nkw=subaru+wrx"
    "&_fsrp=1"
    "&rt=nc"
    # Note: eBay Motors ignores _stpos/_sadis for vehicle pickups; we filter by ZIP post-fetch
)


def scrape_ebay(ctx):
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
            ? nodes.map(node => node.querySelector('a[href*="/itm/"]')).filter(Boolean)
            : Array.from(document.querySelectorAll('a[href*="/itm/"]'));
        links.forEach(a => {
            const m = (a.href || '').match(/\\/itm\\/(\\d+)/);
            if (!m) return;
            const iid = m[1];
            if (iid === '123456' || seen.has(iid)) return;  // skip eBay placeholder
            seen.add(iid);
            const card = a.closest('li.s-item') || a.closest('[data-view]') || a.parentElement;
            const text = ((card && card.innerText) || a.innerText || '').slice(0, 800);
            results.push({ iid, href: a.href.split('?')[0], text });
        });
        return results;
    }""")
    page.close()

    print(f"[eBay] Raw cards: {len(cards)}", file=sys.stderr)

    candidates = []
    for card in cards:
        text = card.get("text", "")
        if "wrx" not in text.lower():
            continue
        year_m = re.search(r"\b(2019|202[01])\b", text)
        if not year_m:
            continue
        year = int(year_m.group(1))

        price_m = re.search(r"\$([\d,]+(?:\.\d+)?)", text)
        price = int(float(price_m.group(1).replace(",", ""))) if price_m else None
        if price and price > CL_MAX_PRICE:
            continue
        if price and price < CL_MIN_PRICE:
            continue

        trim_m = re.search(r"\b(premium|limited|sti|sport|base)\b", text, re.I)
        trim = trim_m.group(0).title() if trim_m else ""

        color_m = re.search(
            r"\b(black|gray|grey|charcoal|graphite|dark|obsidian|magnetic)\b",
            text, re.I
        )
        color = color_m.group(0).title() if color_m else ""
        if color and not color_matches_str(color):
            continue

        # Location/ZIP from card text ("from YOUR_ZIP" or city, ST)
        loc_m = re.search(r'from\s+([A-Za-z ,]+|\d{5})', text, re.I)
        loc_txt = loc_m.group(1).strip() if loc_m else ""

        candidates.append({
            "iid": card["iid"],
            "href": card["href"],
            "year": year,
            "trim": trim,
            "price": price,
            "color": color,
            "location": loc_txt,
        })

    print(f"[eBay] Candidates after card filter: {len(candidates)}", file=sys.stderr)

    results = []
    detail_page = ctx.new_page()
    for item in candidates:
        iid = item["iid"]
        href = item["href"]
        try:
            detail_page.goto(href, wait_until="domcontentloaded", timeout=30000)
            detail_page.wait_for_timeout(2000)
            dt = detail_page.inner_text("body") or ""
        except Exception as e:
            print(f"[eBay] detail fetch failed {iid}: {e}", file=sys.stderr)
            continue

        # Must be a Subaru WRX
        if "subaru" not in dt.lower() or "wrx" not in dt.lower():
            print(f"[eBay] skip {iid} not a WRX", file=sys.stderr)
            continue

        # VIN from item specifics
        vin_m = re.search(r'VIN[:\s]+([A-HJ-NPR-Z0-9]{17})\b', dt, re.I)
        vin = vin_m.group(1).upper() if vin_m else ""

        # Mileage
        mi_m = re.search(r'(?:mileage|odometer)[:\s]+([\d,]+)', dt, re.I)
        if not mi_m:
            mi_m = re.search(r'([\d,]+)\s*mi(?:les)?\b', dt, re.I)
        mileage = int(mi_m.group(1).replace(",", "")) if mi_m else None
        if mileage is not None and mileage > MAX_MILES:
            print(f"[eBay] skip {iid} mileage={mileage}", file=sys.stderr)
            continue

        # Exterior color
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

        # ZIP / distance from item specifics
        zip_m = re.search(r'\b(\d{5})\b', item.get("location", ""))
        distance = None
        if zip_m:
            distance = zip_distance_miles(zip_m.group(1))
        if distance is None:
            # Try to find ZIP anywhere on the detail page
            seller_zip_m = re.search(r'(?:located in|location)[:\s]+.*?(\d{5})', dt, re.I)
            if seller_zip_m:
                distance = zip_distance_miles(seller_zip_m.group(1))
        if distance is not None and distance > RADIUS:
            print(f"[eBay] skip {iid} dist={distance:.0f}mi", file=sys.stderr)
            continue

        # Trim from detail if not in title
        if not item["trim"]:
            trim_m = re.search(r'\b(premium|limited|sti|sport|base)\b', dt, re.I)
            item["trim"] = trim_m.group(0).title() if trim_m else ""

        results.append({
            "id": f"eb_{iid}",
            "vin": vin,
            "title": f"{item['year']} Subaru WRX {item['trim']}".strip(),
            "year": item["year"],
            "trim": item["trim"],
            "price": item["price"],
            "mileage": mileage,
            "color": item.get("color", ""),
            "color_str": item.get("color", ""),
            "location": item.get("location", "N/A"),
            "distance": int(distance) if distance is not None else None,
            "deal": "",
            "url": href,
            "source": "eBay Motors",
        })
        print(f"[eBay] Match: eb_{iid} {item['year']} color='{item.get('color','')}' mileage={mileage} price={item['price']}", file=sys.stderr)

    detail_page.close()
    print(f"[eBay] After detail filter: {len(results)}", file=sys.stderr)
    return results


# ── eBay Browse API ──────────────────────────────────────────────────────────
EBAY_APP_ID = "YOUR_EBAY_CLIENT_ID_HERE"
EBAY_TOKEN_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ebay-token.txt")
EBAY_REFRESH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ebay-refresh-token.txt")
EBAY_CLIENT_ID    = "YOUR_EBAY_CLIENT_ID_HERE"
EBAY_CLIENT_SECRET= "YOUR_EBAY_CLIENT_SECRET_HERE"
EBAY_RUNAME       = "YOUR_EBAY_RUNAME_HERE"

def _ebay_refresh_access_token():
    """Use the refresh token to get a new access token. Returns new token or None."""
    if not os.path.exists(EBAY_REFRESH_FILE):
        return None
    refresh_token = open(EBAY_REFRESH_FILE).read().strip()
    if not refresh_token:
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
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
        token = resp.get("access_token", "")
        if token:
            with open(EBAY_TOKEN_FILE, "w") as f:
                f.write(token)
            print("[eBay] Access token refreshed successfully.", file=sys.stderr)
            return token
    except Exception as e:
        print(f"[eBay] Refresh failed: {e}", file=sys.stderr)
    return None

def _ebay_oauth_token():
    """Return a valid eBay access token, auto-refreshing if needed."""
    token = None
    if os.path.exists(EBAY_TOKEN_FILE):
        token = open(EBAY_TOKEN_FILE).read().strip()
    # Quick validity check — if token starts with known prefix, try it first
    if token:
        return token
    # No token — try refresh
    return _ebay_refresh_access_token()

def fetch_ebay_api():
    print("[eBay API] Fetching...", file=sys.stderr)
    token = _ebay_oauth_token()
    if not token:
        print("[eBay API] No token file found — skipping", file=sys.stderr)
        return []

    results = []
    for keyword in ["Subaru WRX", "Subaru WRX STI"]:
        params = urllib.parse.urlencode({
            "q": keyword,
            "category_ids": "6001",  # eBay Motors > Cars & Trucks
            "filter": f"conditionIds:{{3000}},price:[5000..60000],priceCurrency:USD",
            "limit": "50",
        })
        req = urllib.request.Request(
            f"https://api.ebay.com/buy/browse/v1/item_summary/search?{params}",
            headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print(f"[eBay API] 401 — attempting token refresh...", file=sys.stderr)
                token = _ebay_refresh_access_token()
                if not token:
                    print(f"[eBay API] Refresh failed — skipping eBay.", file=sys.stderr)
                    return results
                req2 = urllib.request.Request(
                    f"https://api.ebay.com/buy/browse/v1/item_summary/search?{params}",
                    headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
                )
                try:
                    with urllib.request.urlopen(req2, timeout=20) as r:
                        data = json.loads(r.read())
                except Exception as e2:
                    print(f"[eBay API] Retry failed: {e2}", file=sys.stderr)
                    continue
            else:
                print(f"[eBay API] Search error for '{keyword}': {e}", file=sys.stderr)
                continue
        except Exception as e:
            print(f"[eBay API] Search error for '{keyword}': {e}", file=sys.stderr)
            continue

        items = data.get("itemSummaries", [])
        print(f"[eBay API] '{keyword}': {len(items)} raw results", file=sys.stderr)

        for item in items:
            title = item.get("title", "")
            if not re.search(r'\bWRX\b', title, re.I):
                continue
            try:
                price = int(float(item.get("price", {}).get("value", 0)))
            except Exception:
                price = 0
            if price < 5000:
                continue

            # Parse year from title
            year_m = re.search(r'\b(20\d{2})\b', title)
            if not year_m:
                continue
            year = int(year_m.group(1))
            if not (MIN_YEAR <= year <= MAX_YEAR):
                print(f"[eBay API] skip year={year} '{title}'", file=sys.stderr)
                continue

            # Color check from title — pass unknown (will be filtered at detail level if needed)
            color_m = re.search(r'\b(black|gray|grey|charcoal|graphite|dark|obsidian|crystal black|magnetite|white|blue|red|silver)\b', title, re.I)
            color_raw = color_m.group(0) if color_m else ""
            # Skip only if title explicitly mentions a non-matching color
            if color_raw and not color_matches_str(color_raw, allow_unknown=True):
                continue

            # Location/distance
            loc_data = item.get("itemLocation", {})
            postal = loc_data.get("postalCode", "")
            dist = zip_distance_miles(postal) if postal else None
            if dist is not None and dist > 200:
                print(f"[eBay API] skip {item.get('itemId')} dist={dist:.0f}mi", file=sys.stderr)
                continue

            url = item.get("itemWebUrl", f"https://www.ebay.com/itm/{item.get('itemId','')}")
            trim_m = re.search(r'\b(STI|Premium|Limited|Base)\b', title, re.I)
            trim = trim_m.group(0) if trim_m else ""

            # Pull mileage from localizedAspects if available
            mileage = None
            for aspect in item.get("localizedAspects", []):
                if "mileage" in aspect.get("name", "").lower():
                    try:
                        mileage = int(re.sub(r'[^\d]', '', aspect.get("value", "")))
                    except Exception:
                        pass
                    break
            if mileage is not None and mileage > MAX_MILES:
                print(f"[eBay API] skip {item.get('itemId')} miles={mileage}", file=sys.stderr)
                continue

            location_str = f"{loc_data.get('city','')}, {loc_data.get('stateOrProvince','')}".strip(", ") or "N/A"

            results.append({
                "id": f"eb_{item.get('itemId','')}",
                "title": title,
                "year": year,
                "trim": trim,
                "price": price,
                "mileage": mileage,
                "color": color_raw or "Unknown",
                "color_str": color_raw,
                "location": location_str,
                "distance": int(dist) if dist is not None else None,
                "deal": "",
                "url": url,
                "source": "eBay Motors",
            })
            print(f"[eBay API] Match: {item.get('itemId')} {year} '{color_raw}' ${price:.0f} miles={mileage}", file=sys.stderr)

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
            enriched.append(r)
            continue

        # Pull aspects
        aspects = {a["name"].lower(): a["value"] for a in d.get("localizedAspects", [])}
        mileage = r["mileage"]
        for key in ("mileage", "odometer"):
            if key in aspects:
                try:
                    mileage = int(re.sub(r'[^\d]', '', aspects[key]))
                except Exception:
                    pass
                break
        if mileage is not None and mileage > MAX_MILES:
            print(f"[eBay API] skip {item_id} miles={mileage}", file=sys.stderr)
            continue

        color_raw = r["color_str"]
        for key in ("exterior color", "color"):
            if key in aspects:
                color_raw = aspects[key]
                break
        if not color_raw or not color_matches_str(color_raw, allow_unknown=False):
            print(f"[eBay API] skip {item_id} color={color_raw!r} (no confirmed dark color)", file=sys.stderr)
            continue

        # Pull VIN from aspects for cross-source dedup
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
        location_str = f"{city}, {state}".strip(", ") or r["location"] or "N/A"
        dist = zip_distance_miles(postal) if postal else r.get("distance")
        if dist is None:
            print(f"[eBay API] skip {item_id} — can't verify distance (postal={postal!r})", file=sys.stderr)
            continue
        if dist > RADIUS:
            print(f"[eBay API] skip {item_id} dist={dist:.0f}mi ({location_str})", file=sys.stderr)
            continue

        r.update({
            "vin": vin,
            "mileage": mileage,
            "color": color_raw,
            "color_str": color_raw,
            "location": location_str,
            "distance": int(dist),
        })
        enriched.append(r)

    print(f"[eBay API] Total matches after detail filter: {len(enriched)}", file=sys.stderr)
    return enriched


# ── auto.dev API ──────────────────────────────────────────────────────────────

def scrape_autodev():
    if not AD_API_KEY:
        print("[auto.dev] No API key — skipping", file=sys.stderr)
        return []

    records = []
    for model in ["WRX", "WRX STI"]:
        params = {
            "make": "Subaru",
            "model": model,
            "year_min": MIN_YEAR,
            "year_max": MAX_YEAR,
            "mileage_max": MAX_MILES,
            "zip": ZIP,
            "radius": AD_RADIUS,
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
            print(f"[auto.dev] API error ({model}): {e}", file=sys.stderr)
    print(f"[auto.dev] Raw listings: {len(records)}", file=sys.stderr)

    results = []
    for item in records:
        year = item.get("year")
        try:
            year = int(year)
        except Exception:
            continue

        mileage = item.get("mileageUnformatted") or item.get("mileage")
        try:
            mileage = int(str(mileage).replace(",", "").split()[0])
        except Exception:
            mileage = None

        price = item.get("priceUnformatted") or item.get("price")
        try:
            price = int(str(price).replace(",", "").replace("$", ""))
        except Exception:
            price = None

        trim = item.get("trim", "") or ""
        color = item.get("displayColor", "") or item.get("exterior_color", "") or ""

        city = item.get("city", "")
        state = item.get("state", "")
        location = f"{city}, {state}".strip(", ") if (city or state) else "N/A"

        vin = item.get("vin", "")
        lid = vin or str(item.get("id", ""))
        vdp_path = item.get("hrefTarget", "") or item.get("vdpUrl", "")
        url_vdp = f"https://auto.dev{vdp_path}" if vdp_path and vdp_path.startswith("/") else (vdp_path or f"https://auto.dev/listings/{lid}")

        title = f"{year} {item.get('make','Subaru')} {item.get('model','WRX')} {trim}".strip()

        if not (MIN_YEAR <= year <= MAX_YEAR):
            continue
        if mileage is not None and mileage > MAX_MILES:
            continue
        if not trim_matches(trim):
            continue
        if not color_matches_str(color):
            continue

        # Distance check using lat/lon from API response
        distance = None
        try:
            lat = float(item.get("latitude") or item.get("lat") or 0)
            lon = float(item.get("longitude") or item.get("lng") or item.get("lon") or 0)
            if lat and lon:
                distance = int(haversine_miles(ORIGIN_LAT, ORIGIN_LON, lat, lon))
                if distance > RADIUS:
                    print(f"[auto.dev] skip {lid} dist={distance}mi ({location})", file=sys.stderr)
                    continue
        except Exception:
            pass

        results.append({
            "id": f"ad_{lid}",
            "vin": vin if len(vin) == 17 else "",
            "title": title,
            "year": year,
            "trim": trim,
            "price": price,
            "mileage": mileage,
            "color": color or "Unknown",
            "color_str": color,
            "location": location,
            "distance": distance,
            "deal": "",
            "url": url_vdp,
            "source": "auto.dev",
        })

    return results


# ── Main scrape ───────────────────────────────────────────────────────────────

def scrape():
    # Use headed mode if a display is available — needed for AutoTrader anti-bot bypass
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
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

        page = ctx.new_page()
        try:
            cg_results = scrape_cargurus(page, CARGURUS_URL)
        except Exception as e:
            print(f"[CarGurus/WRX] Failed: {e}", flush=True)
            cg_results = []

        page_sti = ctx.new_page()
        try:
            cg_sti_results = scrape_cargurus(page_sti, CARGURUS_STI_URL)
        except Exception as e:
            print(f"[CarGurus/STI] Failed: {e}", flush=True)
            cg_sti_results = []
        cg_results = cg_results + cg_sti_results

        try:
            cl_results = scrape_craigslist(ctx)
        except Exception as e:
            print(f"[Craigslist] Failed: {e}", flush=True)
            cl_results = []

        # Cars.com gets its own context with a different UA to avoid Cloudflare fingerprinting
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
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            cd_ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page2 = cd_ctx.new_page()
            cd_results = scrape_carsdotcom(page2)
            cd_ctx.close()
        except Exception as e:
            print(f"[Cars.com] Failed: {e}", flush=True)
            cd_results = []

        # AutoTrader — real Chrome via CDP, run in subprocess so it can be killed hard on timeout
        # (Playwright sync API is thread-locked; subprocess is the only safe way to timeout it)
        import subprocess as _subprocess, json as _json, sys as _sys
        _at_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_at_worker.py")
        at_results = []
        try:
            _at_proc = _subprocess.run(
                [_sys.executable, _at_script],
                capture_output=True, text=True, timeout=150
            )
            if _at_proc.returncode == 0 and _at_proc.stdout.strip():
                raw_listings = _json.loads(_at_proc.stdout.strip())
                at_results = _autotrader_filter(raw_listings)
                print(f"[AutoTrader] {len(at_results)} result(s) after filter (from {len(raw_listings)} raw)", file=sys.stderr, flush=True)
            else:
                stderr_tail = _at_proc.stderr.strip().splitlines()
                for line in stderr_tail:
                    print(f"[AutoTrader] {line}", file=sys.stderr, flush=True)
        except _subprocess.TimeoutExpired:
            print("[AutoTrader] Hard timeout (150s) — subprocess killed, skipping", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[AutoTrader] Subprocess error: {e}", file=sys.stderr, flush=True)

        # Facebook — uses separate context with saved session if available
        fb_results = []
        if os.path.exists(FB_SESSION_FILE):
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
            except Exception as e:
                print(f"[Facebook] Failed: {e}", flush=True)
                fb_results = []
        else:
            print("[Facebook] Skipping — no session file. Run fb-auth-setup.py to enable.", file=sys.stderr)

        eb_results = fetch_ebay_api()

        browser.close()

    ad_results = scrape_autodev()

    # Deduplicate across sources: prefer VIN match, fall back to (year, mileage, price) fingerprint
    seen_vins = set()
    seen_fp = set()
    all_results = []
    for r in cg_results + cl_results + cd_results + at_results + ad_results + fb_results + eb_results:
        vin = (r.get("vin") or "").strip().upper()
        # Normalize to int; round price to nearest $500 to catch cross-source fee differences
        def _norm(v):
            try: return int(round(float(v))) if v is not None else None
            except (TypeError, ValueError): return None
        def _norm_price(v):
            try: return int(round(float(v) / 500)) if v is not None else None
            except (TypeError, ValueError): return None
        fp = (_norm(r.get("year")), _norm(r.get("mileage")), _norm_price(r.get("price")))
        if vin and len(vin) == 17:
            if vin in seen_vins:
                print(f"[Dedup] VIN match — dropping {r.get('source')} {r.get('id')}", file=sys.stderr)
                continue
            if fp != (None, None, None) and fp in seen_fp:
                print(f"[Dedup] Fingerprint match (VIN path) {fp} — dropping {r.get('source')} {r.get('id')}", file=sys.stderr)
                continue
            seen_vins.add(vin)
        else:
            if fp in seen_fp and fp != (None, None, None):
                print(f"[Dedup] Fingerprint match {fp} — dropping {r.get('source')} {r.get('id')}", file=sys.stderr)
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
        except Exception:
            pass
    return set(), set(), {}


def save_seen(listings):
    ids = sorted({r["id"] for r in listings})
    vins = sorted({r["vin"] for r in listings if r.get("vin") and len(r["vin"]) == 17})
    prices = {r["id"]: r["price"] for r in listings if r.get("price")}
    with open(SEEN_FILE, "w") as f:
        json.dump({"ids": ids, "vins": vins, "prices": prices}, f, indent=2)


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message):
    if not TG_TOKEN:
        print("[Telegram] No TG_BOT_TOKEN set", file=sys.stderr)
        return
    import urllib.request
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
        data=data,
        headers={"Content-Type": "application/json"},
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
    src = r.get("source", "")
    return (
        f"🚗 <b>{r.get('title','(no title)')}</b>{deal}\n"
        f"💰 {price}  📏 {miles}\n"
        f"🎨 {r.get('color','?')}  📍 {r.get('location','?')}{dist}\n"
        f"🔗 {r['url']}  <i>({src})</i>"
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
    """Fill in deal rating for listings missing one, using price vs median of all priced listings."""
    prices = [r["price"] for r in listings if r.get("price")]
    if not prices:
        return listings
    prices_sorted = sorted(prices)
    n = len(prices_sorted)
    median = prices_sorted[n // 2]
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--notify", action="store_true", help="Send Telegram summary")
    parser.add_argument("--all", action="store_true", help="Treat all listings as new")
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
        if r["id"] in seen_ids:
            return False
        vin = (r.get("vin") or "").strip().upper()
        if vin and len(vin) == 17 and vin in seen_vins:
            return False
        return True

    new_listings = [r for r in listings if is_new(r)]
    sold_ids = seen_ids - current_ids

    print(f"\n{'='*65}")
    print(f"  WRX Premium/STI {MIN_YEAR}-{MAX_YEAR} | Black/Grey | <{MAX_MILES//1000}k mi | {RADIUS}mi of {ZIP}")
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
        print(f"  ── {len(sold_ids)} previously seen listing(s) no longer found (sold/removed) ──\n")

    if args.notify:
        header = f"🚗 <b>WRX Daily Update</b> — {datetime.now().strftime('%b %d, %Y')}\n"
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
