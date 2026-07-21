"""
Kite Connect authentication.

Kite Connect access tokens expire every day, and Zerodha's login flow
requires a manual browser step (there's no fully headless login for
individual developer apps) — you visit a login URL, log in with your
Zerodha credentials + 2FA, and Kite redirects back with a
`request_token` that this script exchanges for an `access_token`.

Typical usage each morning before market open:
    python auth.py
This prints a login URL, waits for you to paste back the redirect URL
(or just the request_token), and saves the access token to
ACCESS_TOKEN_FILE for the rest of the day's scripts to use.
"""

from urllib.parse import urlparse, parse_qs

from kiteconnect import KiteConnect

import config as cfg


def generate_session() -> str:
    kite = KiteConnect(api_key=cfg.API_KEY)
    print("Log in using this URL, then paste the full redirect URL below:")
    print(kite.login_url())

    raw = input("\nPaste redirect URL (or just the request_token): ").strip()

    if raw.startswith("http"):
        parsed = urlparse(raw)
        request_token = parse_qs(parsed.query)["request_token"][0]
    else:
        request_token = raw

    session = kite.generate_session(request_token, api_secret=cfg.API_SECRET)
    access_token = session["access_token"]

    with open(cfg.ACCESS_TOKEN_FILE, "w") as f:
        f.write(access_token)

    print(f"\nSaved access token to {cfg.ACCESS_TOKEN_FILE}")
    return access_token


def get_kite_client() -> KiteConnect:
    """Load a KiteConnect client using the token saved by generate_session()."""
    kite = KiteConnect(api_key=cfg.API_KEY)
    with open(cfg.ACCESS_TOKEN_FILE) as f:
        access_token = f.read().strip()
    kite.set_access_token(access_token)
    return kite


if __name__ == "__main__":
    generate_session()
