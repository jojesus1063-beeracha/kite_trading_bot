"""
Chronological reconstruction (date set by TARGET_DATE below) -- PHASE 1: entry discovery only.

Faithfully reconstructs, candle-by-candle, no look-ahead:
    3m upstream candidate (ADX regime + EMA9/21 + RSI Wilder override)
    -> PA hard gate (real get_price_action_score(), all 8 sub-detectors)
    -> MA hard gate (real compute_market_alignment(), explicit-date NIFTY
       + sector fetches -- bypasses the datetime.now() hazard in
       get_market_trend_diagnostic()/get_sector_trend_diagnostic())
    -> 5m Master Candlestick gate (real candlestick_engine.evaluate_trade_entry(),
       real resample_completed_3m_to_5m(), persistent WAITING state per symbol,
       max 2-bar wait, NEXT_OPEN fail-closed)

Every source function/constant is exactly the one verified this session:
    ADX block [20,30); regime split ADX>40 normal / <=40 reversed
    EMA 9/21; RSI Wilder(14), >=70 BUY / <=30 SELL
    PA: get_price_action_score() -- +15/-15/+10/+10/+5/+10/-20/-25, score<=0 blocks
    MA: compute_market_alignment() -- +-1 per trend, -2..+2 -> 5 labels,
        MISALIGNED/STRONG_MISALIGNMENT blocks. Unmapped sector -> "Sideways"
        (NOT "UNKNOWN" -- that branch is confirmed dead code for this path),
        contributes 0 to the alignment score like a genuine Sideways sector.
    Candlestick: PAPER_MASTER_CANDLESTICK_MIN_RR=2.0, MAX_WAIT_BARS=2,
        NEXT_OPEN="FAIL_CLOSED"

Deliberately does NOT reuse get_market_trend_diagnostic() or
get_sector_trend_diagnostic() directly -- both anchor to datetime.now()
internally, confirmed this session. Every 15m fetch here uses an explicit
to_date bounded at-or-before the simulated decision timestamp.

This is PHASE 1 ONLY: outputs confirmed entries (symbol, direction, time,
entry/stop/target, quantity). No exit simulation, no P&L. Phase 2 will
walk each confirmed entry forward through the verified exit stack
(emergency stop -> ATR trailing -> structure break -> trend reversal ->
CP9 -> MAE -> MFE/time -> EOD 15:08) separately, once this stage's
candidate list is reviewed.

Read-only. Never writes state, never places orders.

Run:
    BOT_DIR=~/kite_trading_bot python3 replay_aug12_phase1_entries.py
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

BOT_DIR = Path(os.path.expanduser(os.environ.get("BOT_DIR", "~/kite_trading_bot"))).resolve()
if not BOT_DIR.exists():
    raise SystemExit(f"BOT_DIR does not exist: {BOT_DIR}")
sys.path.insert(0, str(BOT_DIR))

import config as cfg
from auth import get_kite_client
from data_feed import fetch_candles, get_instrument_token
from indicators import add_indicators

from strategy import get_trend
from market_trend import (
    NIFTY50_TOKEN, SECTOR_INDEX_TOKENS, SECTOR_MAP, classify_trend,
    compute_market_alignment,
)
from price_action import evaluate_price_action
from candlestick_engine import evaluate_trade_entry, CandlestickEngine, EngineConfig, GateState

TARGET_DATE = pd.Timestamp("2026-08-13")
WARMUP_DAYS = 15  # enough for EMA21/RSI14/ADX warmup on 3m/15m data

EMA_FAST, EMA_SLOW = 9, 21
RSI_PERIOD = 14
RSI_OVERBOUGHT, RSI_OVERSOLD = 70.0, 30.0
ADX_BLOCK_LOW, ADX_BLOCK_HIGH = 20.0, 30.0
ADX_REGIME_THRESHOLD = 40.0

MASTER_TICK_DEFAULT = 0.05


def price_action_blocks(score: float) -> bool:
    """Exact copy of main.py's price_action_blocks_entry() threshold --
    score<=0 blocks. Kept local since importing main.py directly would
    pull in live-trading machinery this replay must never touch."""
    try:
        return float(score) <= 0.0
    except (TypeError, ValueError):
        return True


def market_alignment_blocks(alignment: str) -> bool:
    return alignment in ("MISALIGNED", "STRONG_MISALIGNMENT")


def calculate_rsi_wilder(closes: pd.Series, period: int = RSI_PERIOD):
    c = pd.to_numeric(closes, errors="coerce")
    d = c.diff()
    g = d.clip(lower=0.0)
    l = -d.clip(upper=0.0)
    ag = g.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    al = l.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    if pd.isna(ag.iloc[-1]) or pd.isna(al.iloc[-1]):
        return None
    if al.iloc[-1] == 0:
        return 100.0 if ag.iloc[-1] > 0 else 50.0
    rs = ag.iloc[-1] / al.iloc[-1]
    return float(100 - (100 / (1 + rs)))


def ema_regime_direction(closes: pd.Series, adx):
    if len(closes) < EMA_SLOW:
        return None, None, None
    c = pd.to_numeric(closes, errors="coerce")
    e9 = c.ewm(span=EMA_FAST, adjust=False).mean().iloc[-1]
    e21 = c.ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1]
    if pd.isna(e9) or pd.isna(e21):
        return None, None, None
    normal_regime = adx is not None and adx > ADX_REGIME_THRESHOLD
    if e9 > e21:
        direction = "BUY" if normal_regime else "SELL"
    elif e9 < e21:
        direction = "SELL" if normal_regime else "BUY"
    else:
        direction = None
    return direction, float(e9), float(e21)


def rsi_override_direction(rsi):
    if rsi is None:
        return None
    if rsi >= RSI_OVERBOUGHT:
        return "BUY"
    if rsi <= RSI_OVERSOLD:
        return "SELL"
    return None


def fetch_bounded(kite, token, interval, as_of, warmup_days=WARMUP_DAYS):
    """Explicit-date fetch, NEVER uses fetch_candles' datetime.now() default.
    to_date is always at-or-before the simulated decision timestamp."""
    start = (as_of - timedelta(days=warmup_days)).to_pydatetime()
    end = as_of.to_pydatetime()
    return fetch_candles(kite, token, interval, from_date=start, to_date=end, trim_incomplete=False)


def historical_market_trend(kite, as_of):
    """Bypasses get_market_trend_diagnostic() -- explicit date bound."""
    df = fetch_bounded(kite, NIFTY50_TOKEN, cfg.TREND_TIMEFRAME, as_of)
    if df.empty:
        return "Sideways"
    df, _ = add_indicators(df, df.copy(), cfg)
    return classify_trend(df, cfg)


def historical_sector_trend(kite, symbol, as_of, sector_cache):
    """Bypasses get_sector_trend_diagnostic() -- explicit date bound.
    Unmapped/missing-token symbols return "Sideways" (confirmed real
    behavior; NOT "Sideways"->"UNKNOWN" -- that branch never fires here)."""
    sector_name = SECTOR_MAP.get(symbol)
    if sector_name is None:
        return "Sideways"
    token = SECTOR_INDEX_TOKENS.get(sector_name)
    if token is None:
        return "Sideways"
    cache_key = (sector_name, as_of)
    if cache_key in sector_cache:
        return sector_cache[cache_key]
    df = fetch_bounded(kite, token, cfg.TREND_TIMEFRAME, as_of)
    if df.empty:
        trend = "Sideways"
    else:
        df, _ = add_indicators(df, df.copy(), cfg)
        trend = classify_trend(df, cfg)
    sector_cache[cache_key] = trend
    return trend


def within_entry_window(ts, no_entry_before, no_entry_after):
    t = pd.Timestamp(ts).time()
    start = datetime.strptime(no_entry_before, "%H:%M").time()
    end = datetime.strptime(no_entry_after, "%H:%M").time()
    return start <= t <= end


def main():
    kite = get_kite_client()

    # CRITICAL: match the real launcher's actual runtime config exactly.
    # config.py's SOURCE DEFAULTS are NOT necessarily what ran on the target date -- the real
    # launcher's apply_tested_paper_overrides() sets these explicitly at
    # startup. Without this, evaluate_price_action() would silently return
    # (0, {"enabled": False}) for every candidate (ENABLE_PRICE_ACTION
    # defaults to False), and price_action_blocks(0) would then block
    # everything, always -- a completely different and wrong PA gate
    # state than what actually ran. Caught by direct verification against
    # the real main.py functions before this script was ever run.
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY ABORT: this reconstruction requires PAPER_TRADING=True")
    cfg.ENABLE_PRICE_ACTION = True
    cfg.ENABLE_MARKET_ALIGNMENT_FILTER = True

    watchlist_path = BOT_DIR / "runtime" / "auto_watchlist" / "latest_watchlist.json"
    import json
    with open(watchlist_path) as f:
        wl_data = json.load(f)
    generated_at = str(wl_data.get("generated_at", ""))
    if not generated_at.startswith(str(TARGET_DATE.date())):
        raise SystemExit(
            f"SAFETY ABORT: latest_watchlist.json generated_at={generated_at!r} "
            f"does not match target date {TARGET_DATE.date()} -- refusing to use "
            f"an unverified watchlist."
        )
    watchlist = wl_data.get("watchlist", [])
    symbols = [(w.get("symbol") if isinstance(w, dict) else w,
                w.get("exchange", "NSE") if isinstance(w, dict) else "NSE")
               for w in watchlist]

    no_entry_before = getattr(cfg, "NO_ENTRY_BEFORE", "09:30")
    no_entry_after = getattr(cfg, "NO_ENTRY_AFTER", "15:05")

    print("=" * 100)
    print(f"CHRONOLOGICAL RECONSTRUCTION -- PHASE 1: ENTRY DISCOVERY ONLY -- TARGET DATE {TARGET_DATE.date()}")
    print("=" * 100)
    print(f"Watchlist verified: generated_at={generated_at}, {len(symbols)} symbols")
    print(f"Entry window: {no_entry_before}-{no_entry_after}")
    print("NOTE: this stage does NOT simulate exits or P&L. Output is the")
    print("confirmed-entry candidate list only, for review before Phase 2.")
    print()

    engine = CandlestickEngine(EngineConfig(risk_pct=0.20))  # internal qty unused; see verified trace
    sector_cache = {}
    rejection_counts = Counter()
    confirmed_entries = []

    start_fetch = (TARGET_DATE - pd.Timedelta(days=WARMUP_DAYS)).to_pydatetime()
    end_fetch = (TARGET_DATE + pd.Timedelta(days=1)).to_pydatetime()

    for idx, (symbol, exchange) in enumerate(symbols, 1):
        print(f"[{idx:02d}/{len(symbols)}] {symbol}", flush=True)
        try:
            token = get_instrument_token(kite, symbol, exchange)
            df_3m_raw = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME,
                                      from_date=start_fetch, to_date=end_fetch, trim_incomplete=False)
            df_15m_raw = fetch_candles(kite, token, cfg.TREND_TIMEFRAME,
                                       from_date=start_fetch, to_date=end_fetch, trim_incomplete=False)
            if df_3m_raw.empty or df_15m_raw.empty:
                rejection_counts["NO_DATA"] += 1
                continue

            df_15m, df_3m = add_indicators(df_15m_raw, df_3m_raw, cfg)
            today_3m = df_3m[df_3m["date"].dt.date == TARGET_DATE.date()]

            for row_index in today_3m.index:
                candle_ts = df_3m.loc[row_index, "date"]
                if not within_entry_window(candle_ts, no_entry_before, no_entry_after):
                    continue
                if row_index < EMA_SLOW:
                    continue

                three_m_slice = df_3m.loc[:row_index]
                fifteen_slice = df_15m[df_15m["date"] <= candle_ts]
                if fifteen_slice.empty:
                    rejection_counts["NO_15M_CONTEXT"] += 1
                    continue

                stock_adx = fifteen_slice["adx"].dropna()
                adx_val = float(stock_adx.iloc[-1]) if not stock_adx.empty else None

                if adx_val is not None and ADX_BLOCK_LOW <= adx_val < ADX_BLOCK_HIGH:
                    rejection_counts["ADX_20_30_BLOCK"] += 1
                    continue

                base_dir, e9, e21 = ema_regime_direction(three_m_slice["close"], adx_val)
                if base_dir is None:
                    rejection_counts["EMA_UNAVAILABLE_OR_EQUAL"] += 1
                    continue
                rsi_val = calculate_rsi_wilder(three_m_slice["close"])
                override = rsi_override_direction(rsi_val)
                direction = override or base_dir

                # -- PA hard gate --
                five_m_partial = df_3m.loc[:row_index]  # resampled below
                from paper_5m_master_full_capital import resample_completed_3m_to_5m
                df_5m_for_pa = resample_completed_3m_to_5m(three_m_slice)
                if df_5m_for_pa.empty:
                    rejection_counts["PA_NO_5M_DATA"] += 1
                    continue
                pa_score, pa_detail = evaluate_price_action(df_5m_for_pa, direction, cfg)
                if price_action_blocks(pa_score):
                    rejection_counts["PA_HARD_GATE"] += 1
                    continue

                # -- MA hard gate (explicit-date bounded, no look-ahead) --
                market_trend = historical_market_trend(kite, candle_ts)
                sector_trend = historical_sector_trend(kite, symbol, candle_ts, sector_cache)
                alignment = compute_market_alignment(direction, market_trend, sector_trend)
                if market_alignment_blocks(alignment):
                    rejection_counts["MA_HARD_GATE"] += 1
                    continue

                # -- 5m Master Candlestick gate --
                result = evaluate_trade_entry(
                    symbol, df_5m_for_pa, direction,
                    float(getattr(cfg, "CAPITAL", 0.0) or 0.0),
                    MASTER_TICK_DEFAULT, engine=engine,
                )
                if result.state == GateState.NO_PATTERN:
                    rejection_counts["CANDLESTICK_NO_PATTERN"] += 1
                    continue
                if result.state == GateState.WAITING:
                    rejection_counts["CANDLESTICK_WAITING"] += 1
                    continue
                if result.state == GateState.CONFIRMED and result.plan is not None:
                    if result.plan.trigger.value == "NEXT_OPEN":
                        rejection_counts["UNSAFE_NEXT_OPEN_EXCLUDED"] += 1
                        continue
                    confirmed_entries.append({
                        "symbol": symbol, "direction": direction,
                        "timestamp": candle_ts,
                        "entry": float(result.plan.entry_price),
                        "stop": float(result.plan.stop_price),
                        "target": float(result.plan.target_price),
                        "pattern": result.plan.pattern.value,
                        "market_trend": market_trend, "sector_trend": sector_trend,
                        "alignment": alignment, "pa_score": pa_score,
                    })

        except Exception as exc:
            rejection_counts["ERROR"] += 1
            print(f"  ERROR: {exc}")

    print("\n" + "=" * 100)
    print("PHASE 1 RESULT -- CONFIRMED ENTRY CANDIDATES")
    print("=" * 100)
    for r in sorted(confirmed_entries, key=lambda x: x["timestamp"]):
        print(f"{r['timestamp']:%H:%M}  {r['symbol']:<14} {r['direction']:<4} "
              f"entry={r['entry']:.2f} stop={r['stop']:.2f} target={r['target']:.2f} "
              f"pattern={r['pattern']} MA={r['alignment']}")

    print(f"\nTotal confirmed entries: {len(confirmed_entries)}")
    print("\nRejection breakdown:")
    for reason, count in rejection_counts.most_common():
        print(f"  {count:5d}  {reason}")

    import json as json_out
    output_path = BOT_DIR / f"phase1_entries_{TARGET_DATE.date()}.json"
    serializable = [
        {**e, "timestamp": e["timestamp"].isoformat()} for e in confirmed_entries
    ]
    with open(output_path, "w") as f:
        json_out.dump(serializable, f, indent=2)
    print(f"\nConfirmed entries written to: {output_path}")
    print(f"Feed this directly into Phase 2: --entries {output_path}")


if __name__ == "__main__":
    main()
