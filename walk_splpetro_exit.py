"""
Walks the single real gate-passed signal from the fixed pullback
strategy replay (SPLPETRO SELL, 2026-08-07 13:30) to its actual
historical outcome, reusing the exact tested exit engine from
exit_replay_20260807.py (_walk_exit, _recompute_stop_target) rather
than reimplementing it.

Run:
    BOT_DIR=~/kite_trading_bot python3 walk_splpetro_exit.py
"""

import os
import sys
from pathlib import Path

import pandas as pd

BOT_DIR = Path(os.path.expanduser(os.environ.get("BOT_DIR", "~/kite_trading_bot"))).resolve()
sys.path.insert(0, str(BOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg
from auth import get_kite_client
from data_feed import fetch_candles, get_instrument_token

import exit_replay_20260807 as walker

TARGET_DATE = pd.Timestamp("2026-08-07")


def main():
    kite = get_kite_client()

    signal = {
        "symbol": "SPLPETRO",
        "exchange": "NSE",
        "direction": "SELL",
        "timestamp": pd.Timestamp("2026-08-07 13:30:00"),
        "entry": 699.10,
        "stop": 708.17,   # strategy-computed, will be recomputed via fixed_levels_from_fill
        "target": 676.42,  # strategy-computed dynamic 2.5x R:R -- this one IS preserved (Option B)
    }

    token = get_instrument_token(kite, "SPLPETRO", "NSE")
    df5_raw = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME,
                            from_date=TARGET_DATE.to_pydatetime(),
                            to_date=(TARGET_DATE + pd.Timedelta(days=1)).to_pydatetime(),
                            trim_incomplete=False)

    if df5_raw.empty:
        raise SystemExit("FATAL: could not fetch SPLPETRO 5-minute data for 2026-08-07.")

    print(f"Fetched {len(df5_raw)} 5-minute candles for SPLPETRO on 2026-08-07.")
    print(f"Signal: SELL entry={signal['entry']} strategy-stop={signal['stop']} "
          f"strategy-target={signal['target']} (2.5x R:R)")

    recomputed_stop, recomputed_target = walker._recompute_stop_target(signal)
    print(f"Production-actual levels (ENABLE_FIXED_TARGET={getattr(cfg, 'ENABLE_FIXED_TARGET', None)}): "
          f"stop={recomputed_stop:.2f} (recomputed from STOP_LOSS_PERCENT) "
          f"target={recomputed_target:.2f} (preserved dynamic R:R)")

    result = walker._walk_exit(kite, signal, {"SPLPETRO": df5_raw})

    print("\n" + "=" * 70)
    print("RESULT")
    print("=" * 70)
    for k, v in result.items():
        print(f"  {k}: {v}")

    if result.get("exit_reason") in ("STOP_HIT", "TARGET_HIT", "SQUARE_OFF"):
        pnl_per_share = (signal["entry"] - result["exit_price"])  # SELL: profit if exit < entry
        print(f"\nP&L per share (1 share, gross, no costs/slippage): {pnl_per_share:+.2f}")
        capital = getattr(cfg, "CAPITAL", None)
        risk_pct = getattr(cfg, "RISK_PER_TRADE_PCT", None)
        if capital and risk_pct:
            risk_amount = capital * (risk_pct / 100)
            risk_per_share = abs(signal["entry"] - recomputed_stop)
            if risk_per_share > 0:
                qty = int(risk_amount / risk_per_share)
                print(f"Production-sized quantity (capital={capital}, risk_per_trade_pct={risk_pct}%, "
                      f"risk_per_share={risk_per_share:.2f}): {qty} shares")
                print(f"Production-sized P&L (gross, no costs/slippage): {pnl_per_share * qty:+.2f}")


if __name__ == "__main__":
    main()
