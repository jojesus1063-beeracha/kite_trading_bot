"""
Validates candle_engine.py's candle-building logic against REAL
historical market data, instead of synthetic test data.

How it works:
1. Fetch real 1-minute historical candles for a symbol over the past
   few trading days (this is what real ticks would have produced,
   compressed to one point per minute -- a reasonable stand-in since
   we don't have historical tick-by-tick data available).
2. Feed each 1-minute candle's close price through
   candle_engine.SymbolCandleBuilder exactly as a live tick would
   arrive (using the candle's own timestamp and close as the "last
   traded price").
3. Compare the resulting BUILT 5-minute candles against Kite's own
   REAL 5-minute historical candles for the same period.
4. Do the same for 15-minute (built from 3 finalized 5-min candles).

This is read-only historical data -- no live WebSocket connection, no
paper or live trading, no market hours required. Safe to run any time.

Usage:
    python3 validate_against_historical.py RELIANCE
    python3 validate_against_historical.py RELIANCE --days 3
    python3 validate_against_historical.py RELIANCE TCS HDFCBANK INFY
"""

import sys
import argparse
from datetime import datetime, timedelta

import pandas as pd

from auth import get_kite_client
from data_feed import fetch_candles, get_instrument_token
from candle_engine import SymbolCandleBuilder, combine_5m_into_15m
import config as cfg


def validate_symbol(kite, symbol: str, exchange: str, days: int):
    print(f"\n{'=' * 70}")
    print(f"Validating {exchange}:{symbol} against {days} day(s) of real historical data")
    print(f"{'=' * 70}")

    try:
        token = get_instrument_token(kite, symbol, exchange)
    except Exception as e:
        print(f"FAIL: could not resolve instrument token -- {e}")
        return False

    to_date = datetime.now()
    from_date = to_date - timedelta(days=days)

    # Real 1-minute candles, used as a stand-in for tick-by-tick data.
    minute_df = fetch_candles(kite, token, "minute", from_date=from_date, to_date=to_date, trim_incomplete=False)
    if minute_df.empty:
        print("FAIL: no 1-minute historical data returned (market may be closed with no recent data, "
              "or lookback window too short)")
        return False
    print(f"Fetched {len(minute_df)} real 1-minute candles as tick stand-ins")

    # Ground truth: Kite's own real 5-minute and 15-minute candles.
    real_5m = fetch_candles(kite, token, "5minute", from_date=from_date, to_date=to_date, trim_incomplete=False)
    real_15m = fetch_candles(kite, token, "15minute", from_date=from_date, to_date=to_date, trim_incomplete=False)
    if real_5m.empty or real_15m.empty:
        print("FAIL: no real 5-min/15-min historical data returned for comparison")
        return False

    # -- Build 5-min candles by feeding each 1-min candle's O/H/L/C ---------
    # -- through the exact same builder used for live ticks -----------------
    # Feeding all 4 points per minute (not just the close) preserves that
    # minute's true open/high/low -- using only the close would make the
    # built candle's "open" field differ from Kite's real one by roughly
    # the first minute's open-close spread, which looks like a bug but
    # isn't one; it's information genuinely lost by under-sampling.
    builder = SymbolCandleBuilder(symbol, interval_minutes=5)
    for _, row in minute_df.iterrows():
        base = row["date"]
        for offset_sec, price in [
            (0, float(row["open"])),
            (15, float(row["high"])),
            (30, float(row["low"])),
            (45, float(row["close"])),
        ]:
            tick = {
                "exchange_timestamp": base + pd.Timedelta(seconds=offset_sec),
                "last_price": price,
                "volume_traded": None,  # cumulative session volume isn't derivable from 1-min OHLC alone; volume is not compared
            }
            builder.add_tick(tick)
    built_5m = builder.finalized_df()

    if built_5m.empty:
        print("FAIL: builder produced zero finalized 5-min candles from the 1-min data")
        return False
    print(f"Built {len(built_5m)} 5-minute candles from the 1-minute data")

    # -- Compare built 5-min candles against Kite's real 5-min candles ------
    real_5m_indexed = real_5m.set_index(real_5m["date"].dt.tz_localize(None) if real_5m["date"].dt.tz is not None
                                          else real_5m["date"])
    price_tolerance = 0.01  # rupees -- should match closely since both derive from the same trades
    mismatches = []
    compared = 0
    for _, built_row in built_5m.iterrows():
        built_date = built_row["date"]
        built_date_naive = built_date.tz_localize(None) if getattr(built_date, "tzinfo", None) is not None else built_date
        if built_date_naive not in real_5m_indexed.index:
            continue
        real_row = real_5m_indexed.loc[built_date_naive]
        compared += 1
        for field in ("open", "high", "low", "close"):
            delta = abs(built_row[field] - real_row[field])
            if delta > price_tolerance:
                mismatches.append((built_date, field, built_row[field], real_row[field], delta))

    print(f"\n5-MINUTE RESULT: {compared} candles matched to a real Kite candle by timestamp, "
          f"{len(mismatches)} field mismatches beyond Rs{price_tolerance} tolerance")
    if mismatches:
        print("First few mismatches:")
        for m in mismatches[:5]:
            print(f"  {m[0]} field={m[1]} built={m[2]} real={m[3]} delta={m[4]:.4f}")

    # -- Same for 15-minute, built from the 5-min candles just built --------
    built_15m_list = combine_5m_into_15m(built_5m.to_dict("records"))
    if not built_15m_list:
        print("\n15-MINUTE RESULT: no complete 15-min groups formed (not enough 5-min data)")
    else:
        real_15m_indexed = real_15m.set_index(real_15m["date"].dt.tz_localize(None) if real_15m["date"].dt.tz is not None
                                                else real_15m["date"])
        mismatches_15 = []
        compared_15 = 0
        for c in built_15m_list:
            d = c["date"]
            d_naive = d.tz_localize(None) if getattr(d, "tzinfo", None) is not None else d
            if d_naive not in real_15m_indexed.index:
                continue
            real_row = real_15m_indexed.loc[d_naive]
            compared_15 += 1
            for field in ("open", "high", "low", "close"):
                delta = abs(c[field] - real_row[field])
                if delta > price_tolerance:
                    mismatches_15.append((d, field, c[field], real_row[field], delta))

        print(f"\n15-MINUTE RESULT: {compared_15} candles matched to a real Kite candle by timestamp, "
              f"{len(mismatches_15)} field mismatches beyond Rs{price_tolerance} tolerance")
        if mismatches_15:
            print("First few mismatches:")
            for m in mismatches_15[:5]:
                print(f"  {m[0]} field={m[1]} built={m[2]} real={m[3]} delta={m[4]:.4f}")

    total_mismatches = len(mismatches) + (len(mismatches_15) if built_15m_list else 0)
    return total_mismatches == 0


def main():
    parser = argparse.ArgumentParser(description="Validate candle_engine against real historical data")
    parser.add_argument("symbols", nargs="+", help="Symbol(s) to validate, e.g. RELIANCE TCS")
    parser.add_argument("--days", type=int, default=2, help="Days of history to use (default 2)")
    parser.add_argument("--exchange", default="NSE")
    args = parser.parse_args()

    kite = get_kite_client()

    results = {}
    for symbol in args.symbols:
        results[symbol] = validate_symbol(kite, symbol, args.exchange, args.days)

    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")
    for symbol, ok in results.items():
        print(f"  {symbol}: {'PASS -- built candles match real historical data exactly' if ok else 'MISMATCHES FOUND -- see above'}")

    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
