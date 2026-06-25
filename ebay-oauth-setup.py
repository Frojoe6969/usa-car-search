#!/usr/bin/env python3
"""
eBay OAuth setup — exchanges auth code for access + refresh tokens.
Run this once to get a long-lived refresh token.
"""
import http.server, urllib.parse, urllib.request, base64, json, threading, webbrowser, sys, os

CLIENT_ID     = os.environ.get("EBAY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
RUNAME        = os.environ.get("EBAY_RUNAME", "")
REDIRECT_URI  = "https://signin.ebay.com/ws/eBayISAPI.dll?ThirdPartyAuthSucessFailure&isAuthSuccessful=true"

if not CLIENT_ID or not CLIENT_SECRET or not RUNAME:
    print("ERROR: Set EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, and EBAY_RUNAME env vars first.")
    print("Find these at developer.ebay.com → your app → Auth Accepted URLs.")
    sys.exit(1)
TOKEN_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ebay-token.txt")
REFRESH_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ebay-refresh-token.txt")
SCOPES        = "https://api.ebay.com/oauth/api_scope"

auth_url = (
    f"https://auth.ebay.com/oauth2/authorize"
    f"?client_id={CLIENT_ID}"
    f"&response_type=code"
    f"&redirect_uri={urllib.parse.quote(RUNAME)}"
    f"&scope={urllib.parse.quote(SCOPES)}"
)

print("\n=== eBay OAuth Setup ===\n")
print("Open this URL in your browser and sign in with your eBay account:\n")
print(auth_url)
print("\nAfter signing in, eBay will redirect to a page.")
print("Copy the FULL URL from your browser's address bar and paste it here.\n")

code_url = input("Paste the redirect URL here: ").strip()

# Extract code from URL
parsed = urllib.parse.urlparse(code_url)
params = urllib.parse.parse_qs(parsed.query)
code = params.get("code", [None])[0]
if not code:
    # Try fragment
    frag = urllib.parse.parse_qs(parsed.fragment)
    code = frag.get("code", [None])[0]

if not code:
    print(f"ERROR: Could not find 'code' in URL: {code_url}")
    sys.exit(1)

print(f"\nGot auth code. Exchanging for tokens...")

creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
data = urllib.parse.urlencode({
    "grant_type": "authorization_code",
    "code": code,
    "redirect_uri": RUNAME,
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
except urllib.error.HTTPError as e:
    print(f"ERROR: {e.read().decode()}")
    sys.exit(1)

access_token  = resp.get("access_token", "")
refresh_token = resp.get("refresh_token", "")
expires_in    = resp.get("expires_in", "?")
rt_expires    = resp.get("refresh_token_expires_in", "?")

if not access_token:
    print(f"ERROR: No access token in response: {resp}")
    sys.exit(1)

with open(TOKEN_FILE, "w") as f:
    f.write(access_token)
with open(REFRESH_FILE, "w") as f:
    f.write(refresh_token)

print(f"\n✅ SUCCESS!")
print(f"   Access token:  valid for {expires_in}s (~2 hours)")
print(f"   Refresh token: valid for {rt_expires}s (~18 months)")
print(f"   Saved to: {TOKEN_FILE}")
print(f"   Refresh saved to: {REFRESH_FILE}")
print(f"\nThe script will now auto-refresh using the refresh token.")
