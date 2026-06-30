#!/usr/bin/env python3
"""Create a Playwright storage-state file for Facebook Marketplace scraping.

Usage:
  python3 fb-auth-setup.py

The script opens Chromium, lets you log into Facebook manually, then saves the
browser storage state to FB_SESSION_FILE or ./fb-session.json.
"""

import os
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    session_file = Path(os.environ.get("FB_SESSION_FILE", "./fb-session.json")).expanduser()
    session_file.parent.mkdir(parents=True, exist_ok=True)

    fb_city = os.environ.get("FB_CITY", "newyork")
    url = f"https://www.facebook.com/marketplace/{fb_city}/search/?query=car&categoryID=vehicles"

    print(f"Opening Facebook Marketplace auth window for city: {fb_city}")
    print("Log in manually, make sure Marketplace loads, then return here and press Enter.")
    print(f"Session will be saved to: {session_file}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        input("Press Enter after login is complete...")
        context.storage_state(path=str(session_file))
        browser.close()

    print(f"Saved Facebook session to {session_file}")


if __name__ == "__main__":
    main()
