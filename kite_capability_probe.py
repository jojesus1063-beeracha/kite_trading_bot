"""
Phase A -- read-only Kite capability probe.

Purpose: before designing any replay engine around historical_data(),
find out what Kite Connect ACTUALLY returns for this account/segment --
which intervals work, how many rows come back, what fields are present --
rather than assuming.

Guarantees:
  - Uses the exact same auth pattern as fno_bot/launcher.py's
    _get_kite_client() (cfg.API_KEY + cfg.ACCESS_TOKEN_FILE).
  - Calls ONLY read-only endpoints: kite.instruments(), kite.ltp(),
    kite.historical_data(). Never imports or calls place_order,
    modify_order, or cancel_order -- grep this file, they don't appear.
  - Never prints the access token or API secret.
  - Writes results to runtime/kite_capability_probe/<date>.json and
    also prints a human-readable summary to stdout.

Run from the fno_trading_bot repo root:
    python3 kite_capability_probe.py
"""
import os
import sys
import json
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fno_bot.config as cfg
from kiteconnect import KiteConnect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("kite_capability_probe")

IST = ZoneInfo("Asia/Kolkata")
TARGET_DATE = date(2026, 8, 20)

INTERVALS_TO_TEST = ["minute", "3minute", "5minute", "15minute"]

NIFTY_INDEX_TOKEN = 256265  # NSE:NIFTY 50 -- standard, well-known Kite instrument token for the index itself


def get_kite_client():
    kite = KiteConnect(api_key=cfg.API_KEY)
    with open(cfg.ACCESS_TOKEN_FILE) as f:
        access_token = f.read().strip()
    kite.set_access_token(access_token)
    return kite


def find_nifty_atm_option_tokens(kite, target_date: date):
    result = {"ce": None, "pe": None, "error": None}
    try:
        spot = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
    except Exception as e:
        result["error"] = f"ltp fetch failed: {e}"
        return result

    try:
        nfo_instruments = kite.instruments("NFO")
    except Exception as e:
        result["error"] = f"instruments('NFO') fetch failed: {e}"
        return result

    nifty_opts = [
        i for i in nfo_instruments
        if i.get("name") == "NIFTY" and i.get("segment") == "NFO-OPT"
    ]
    if not nifty_opts:
        result["error"] = "no NIFTY options found in NFO instrument dump"
        return result

    expiries = sorted({i["expiry"] for i in nifty_opts if i.get("expiry")})
    valid_expiries = [e for e in expiries if e >= target_date]
    if not valid_expiries:
        result["error"] = f"no NIFTY option expiry on/after {target_date} in current instrument dump"
        return result
    nearest_expiry = valid_expiries[0]

    same_expiry = [i for i in nifty_opts if i.get("expiry") == nearest_expiry]
    strikes = sorted({i["strike"] for i in same_expiry})
    atm_strike = min(strikes, key=lambda s: abs(s - spot))

    ce = next((i for i in same_expiry if i["strike"] == atm_strike and i["instrument_type"] == "CE"), None)
    pe = next((i for i in same_expiry if i["strike"] == atm_strike and i["instrument_type"] == "PE"), None)

    result["spot"] = spot
    result["expiry"] = str(nearest_expiry)
    result["strike"] = atm_strike
    result["ce"] = {"token": ce["instrument_token"], "tradingsymbol": ce["tradingsymbol"]} if ce else None
    result["pe"] = {"token": pe["instrument_token"], "tradingsymbol": pe["tradingsymbol"]} if pe else None
    return result


def probe_one(kite, label: str, instrument_token: int, tradingsymbol: str, interval: str, from_dt, to_dt) -> dict:
    entry = {
        "label": label,
        "instrument_token": instrument_token,
        "tradingsymbol": tradingsymbol,
        "interval": interval,
        "requested_from": from_dt.isoformat(),
        "requested_to": to_dt.isoformat(),
        "success": False,
        "row_count": 0,
        "first_timestamp": None,
        "last_timestamp": None,
        "fields": None,
        "error": None,
    }
    try:
        rows = kite.historical_data(instrument_token, from_dt, to_dt, interval)
        entry["success"] = True
        entry["row_count"] = len(rows)
        if rows:
            entry["first_timestamp"] = str(rows[0].get("date"))
            entry["last_timestamp"] = str(rows[-1].get("date"))
            entry["fields"] = sorted(rows[0].keys())
    except Exception as e:
        entry["error"] = f"{type(e).__name__}: {e}"
    return entry


def main():
    logger.info(f"Starting Kite capability probe for {TARGET_DATE} (read-only, no order calls)")
    kite = get_kite_client()

    day_start = datetime.combine(TARGET_DATE, datetime.min.time()).replace(tzinfo=IST, hour=9, minute=15)
    day_end = datetime.combine(TARGET_DATE, datetime.min.time()).replace(tzinfo=IST, hour=15, minute=30)

    results = {
        "probe_run_at": datetime.now(IST).isoformat(),
        "target_date": str(TARGET_DATE),
        "underlying_used": "NIFTY 50 (index token, well-known constant)",
        "option_lookup": None,
        "historical_data_tests": [],
    }

    for interval in INTERVALS_TO_TEST:
        results["historical_data_tests"].append(
            probe_one(kite, "NIFTY 50 index", NIFTY_INDEX_TOKEN, "NIFTY 50", interval, day_start, day_end)
        )

    option_lookup = find_nifty_atm_option_tokens(kite, TARGET_DATE)
    results["option_lookup"] = option_lookup

    if option_lookup.get("ce") and option_lookup.get("pe"):
        for leg in ("ce", "pe"):
            token = option_lookup[leg]["token"]
            symbol = option_lookup[leg]["tradingsymbol"]
            for interval in INTERVALS_TO_TEST:
                results["historical_data_tests"].append(
                    probe_one(kite, f"NIFTY ATM {leg.upper()}", token, symbol, interval, day_start, day_end)
                )
    else:
        results["option_note"] = (
            "Could not resolve a CE/PE pair for the option probe -- see option_lookup.error."
        )

    out_dir = os.path.join("runtime", "kite_capability_probe")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{TARGET_DATE}.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print("\n" + "=" * 80)
    print(f"KITE CAPABILITY PROBE -- {TARGET_DATE}")
    print("=" * 80)
    for t in results["historical_data_tests"]:
        status = "OK" if t["success"] else "FAIL"
        print(f"[{status}] {t['label']:20s} interval={t['interval']:10s} rows={t['row_count']:4d} "
              f"first={t['first_timestamp']} last={t['last_timestamp']} error={t['error']}")
    print("-" * 80)
    print(f"Option lookup: {json.dumps(option_lookup, default=str)}")
    print(f"\nFull results saved to: {out_path}")
    print("No order-placement, modification, or cancellation function was called by this script.")


if __name__ == "__main__":
    main()
