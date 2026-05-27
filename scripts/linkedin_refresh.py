#!/usr/bin/env python3
"""LinkedIn OAuth helper — refresh or re-authorize the LinkedIn access token.

Usage:
    python scripts/linkedin_refresh.py

Tries to refresh using LINKEDIN_REFRESH_TOKEN first. If no refresh token
exists or it's expired, runs the full OAuth flow (opens browser, prompts
for redirect URL, saves new tokens to .env).

Note: the intelligence agent's LinkedIn scraper uses Playwright and does NOT
need this token. This script is for other platform services that use the
LinkedIn API directly.

Requires LINKEDIN_DEV_CLIENT_ID and LINKEDIN_DEV_CLIENT_SECRET in .env.
The redirect URI below must match what's configured in your LinkedIn app:
    https://developers.linkedin.com/apps → Auth → Redirect URLs
"""

import os
import sys
import urllib.parse
import webbrowser

import requests
from dotenv import load_dotenv, set_key

load_dotenv()

CLIENT_ID = os.environ.get("LINKEDIN_DEV_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("LINKEDIN_DEV_CLIENT_SECRET", "")
ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")

# Must match a redirect URL registered in your LinkedIn app settings.
# For local use, add https://localhost to your app's allowed redirect URIs.
REDIRECT_URI = os.environ.get("LINKEDIN_REDIRECT_URI", "https://localhost")

SCOPES = "openid profile email"


def _save(key: str, value: str) -> None:
    set_key(os.path.abspath(ENV_FILE), key, value)
    print(f"  ✓ {key} saved to .env")


def try_refresh() -> bool:
    refresh_token = os.environ.get("LINKEDIN_REFRESH_TOKEN", "")
    if not refresh_token:
        print("No LINKEDIN_REFRESH_TOKEN found — running full OAuth flow.")
        return False

    print("Attempting token refresh...")
    r = requests.post(
        "https://www.linkedin.com/oauth/v2/accessToken",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=10,
    )
    data = r.json()
    if "access_token" in data:
        expires = data.get("expires_in", "?")
        print(f"✓ Refreshed successfully. Token expires in {expires}s (~{int(expires)//86400}d).")
        _save("LINKEDIN_ACCESS_TOKEN", data["access_token"])
        if "refresh_token" in data:
            _save("LINKEDIN_REFRESH_TOKEN", data["refresh_token"])
        return True

    print(f"Refresh failed: {data.get('error_description', data)}")
    print("Falling back to full OAuth flow.")
    return False


def full_oauth() -> None:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": "intelligence-agent",
    }
    auth_url = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(params)

    print(f"\nOpening browser for LinkedIn authorization...")
    print(f"URL: {auth_url}\n")
    webbrowser.open(auth_url)

    print("After approving, you'll be redirected to a URL starting with:")
    print(f"  {REDIRECT_URI}/?code=...\n")
    print("Paste the full redirect URL here (even if it shows an error page):")
    redirect_response = input("> ").strip()

    parsed = urllib.parse.urlparse(redirect_response)
    code = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
    if not code:
        print("No authorization code found in URL. Make sure you copied the full redirect URL.")
        sys.exit(1)

    print("Exchanging code for tokens...")
    r = requests.post(
        "https://www.linkedin.com/oauth/v2/accessToken",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=10,
    )
    data = r.json()
    if "access_token" in data:
        expires = data.get("expires_in", "?")
        print(f"\n✓ Authorization successful. Token expires in {expires}s (~{int(expires)//86400}d).")
        _save("LINKEDIN_ACCESS_TOKEN", data["access_token"])
        if "refresh_token" in data:
            _save("LINKEDIN_REFRESH_TOKEN", data["refresh_token"])
            refresh_expires = data.get("refresh_token_expires_in", "?")
            print(f"  Refresh token expires in {refresh_expires}s (~{int(refresh_expires)//86400}d).")
        else:
            print("  Note: no refresh token returned. Re-run this script when the token expires.")
    else:
        print(f"Token exchange failed: {data}")
        sys.exit(1)


if __name__ == "__main__":
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Error: LINKEDIN_DEV_CLIENT_ID and LINKEDIN_DEV_CLIENT_SECRET must be set in .env")
        sys.exit(1)

    print(f"LinkedIn OAuth — app: {CLIENT_ID}")
    if not try_refresh():
        full_oauth()

    print("\nDone. Run 'python main.py ...' to use the updated token.")
