"""AutoTrader CDP worker — runs in its own process so Playwright sync API works on main thread."""
import sys, json, os

# Import shared config from parent script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# We need to replicate just the AutoTrader logic inline
import time, socket, subprocess
import urllib.request as _urlreq

CHROME_CDP_HOST = "192.168.1.77"
CHROME_CDP_PORT = 9222
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
CHROME_LAUNCH_ARGS = [
    "--remote-debugging-port=9222",
    "--remote-allow-origins=*",
    "--no-first-run", "--no-default-browser-check",
    "--user-data-dir=C:\\Temp\\chrome-debug",
]

AUTOTRADER_URL = (
    "https://www.autotrader.com/cars-for-sale/used-cars/subaru/wrx"
    "?zip=YOUR_ZIP&searchRadius=200&startYear=2019&endYear=2021&maxMileage=65000"
    "&listingTypes=USED&trimCodes=WRX_PREMIUM%2CWRX_STI&sortBy=relevance"
)

def cdp_ready():
    try:
        r = _urlreq.urlopen(f"http://{CHROME_CDP_HOST}:{CHROME_CDP_PORT}/json/version", timeout=2)
        return r.status == 200
    except Exception:
        return False

def chrome_kill():
    try:
        subprocess.run(
            ["/mnt/c/Windows/System32/cmd.exe", "/c", "taskkill", "/F", "/IM", "chrome.exe"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(2)
    except Exception:
        pass

def chrome_launch():
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
            print(f"Failed to launch Chrome: {e}", file=sys.stderr)
    return False

def get_cdp_url(force_relaunch=False):
    if force_relaunch:
        print("Relaunching Chrome (stale CDP session)...", file=sys.stderr)
        chrome_kill()
        time.sleep(2)

    if not force_relaunch and cdp_ready():
        return f"http://{CHROME_CDP_HOST}:{CHROME_CDP_PORT}"

    print("Chrome not ready — launching Chrome on Windows...", file=sys.stderr)
    launch_start = time.time()
    if not chrome_launch():
        print("Could not find or launch Chrome", file=sys.stderr)
        return None

    for i in range(45):
        time.sleep(1)
        if cdp_ready():
            print(f"Chrome CDP ready after {i+1}s ({time.time()-launch_start:.1f}s total)", file=sys.stderr)
            return f"http://{CHROME_CDP_HOST}:{CHROME_CDP_PORT}"

    print("Chrome launched but CDP not ready after 45s", file=sys.stderr)
    return None

def main():
    from playwright.sync_api import sync_playwright
    import re, json as _json

    with sync_playwright() as pw:
        for attempt in range(2):
            cdp_url = get_cdp_url(force_relaunch=(attempt > 0))
            if not cdp_url:
                print("Chrome CDP not available", file=sys.stderr)
                sys.exit(1)

            print(f"Connecting via Chrome CDP... ({cdp_url})", file=sys.stderr)
            try:
                browser = pw.chromium.connect_over_cdp(cdp_url, timeout=15000)
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                page = ctx.new_page()
                page.goto(AUTOTRADER_URL, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(4000)
                title = page.title() or ""
                if "unavailable" in title.lower() or "blocked" in title.lower():
                    print(f"Bot block ({title!r})", file=sys.stderr)
                    page.close()
                    browser.disconnect()
                    print("[]")
                    return

                for scroll_y in [800, 2000, 4000]:
                    page.evaluate(f"window.scrollTo(0, {scroll_y})")
                    page.wait_for_timeout(800)

                # Extract listings JSON from page scripts
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
                                        listings = _json.loads(raw_json[start:start+i+1])
                                    except Exception:
                                        pass
                                    break

                print(f"Raw listings (JSON): {len(listings)}", file=sys.stderr)
                page.close()
                try:
                    browser.disconnect()
                except Exception:
                    pass

                # Output raw listings as JSON — parent will filter
                print(_json.dumps(listings))
                return

            except Exception as e:
                err = str(e)
                print(f"CDP error: {err}", file=sys.stderr)
                if ("ECONNRESET" in err or "ECONNREFUSED" in err) and attempt == 0:
                    print("Connection reset — relaunching Chrome...", file=sys.stderr)
                    continue
                sys.exit(1)

    sys.exit(1)

if __name__ == "__main__":
    main()
