#!/usr/bin/env python3
"""Read-only Kite backtest for classical chart-pattern breakouts.

The detector uses only completed candles available at the decision time.
Orders are simulated at the next 3-minute candle open. Nothing is written to
broker state and no order API is called.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from auth import get_kite_client

try:
    from costs import net_pnl_for_trade
except ImportError:
    net_pnl_for_trade = None


IST = ZoneInfo("Asia/Kolkata")
INTERVAL_MINUTES = 3
LOOKBACK = 40
PIVOT_ORDER = 2


@dataclass(frozen=True)
class Signal:
    symbol: str
    pattern: str
    family: str
    direction: str
    signal_time: pd.Timestamp
    boundary: float
    opposite_boundary: float
    pattern_height: float
    volume_ratio: float | None
    vwap_aligned: bool | None


def _numbers(values: Iterable) -> np.ndarray:
    return pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(dtype=float)


def extract_symbols(payload) -> list[str]:
    """Tolerate the different watchlist report schemas used by the bot."""
    found: list[str] = []

    def walk(value, parent_key=""):
        if isinstance(value, dict):
            for key in ("symbol", "tradingsymbol"):
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    found.append(item.strip().upper().removeprefix("NSE:"))
            for key, item in value.items():
                walk(item, str(key).lower())
        elif isinstance(value, list):
            for item in value:
                walk(item, parent_key)
        elif isinstance(value, str) and parent_key in {
            "symbols", "watchlist", "selected", "selected_symbols", "top_symbols"
        }:
            found.append(value.strip().upper().removeprefix("NSE:"))

    walk(payload)
    return list(dict.fromkeys(s for s in found if s))


def load_watchlist(path: Path) -> list[str]:
    symbols = extract_symbols(json.loads(path.read_text(encoding="utf-8")))
    if not symbols:
        raise SystemExit(f"No symbols found in {path}")
    # The historical selector report also embeds the full evaluated universe.
    # Its selected top-N shortlist is written first; cap this replay to the
    # frozen 60 names rather than silently scanning every embedded candidate.
    return symbols[:60]


def fetch_frame(kite, token: int, start: datetime, end: datetime) -> pd.DataFrame:
    last_error = None
    for attempt in range(3):
        try:
            rows = kite.historical_data(token, start, end, "3minute")
            frame = pd.DataFrame(rows)
            if frame.empty:
                return frame
            frame["date"] = pd.to_datetime(frame["date"])
            if frame["date"].dt.tz is None:
                frame["date"] = frame["date"].dt.tz_localize(IST)
            else:
                frame["date"] = frame["date"].dt.tz_convert(IST)
            columns = ["date", "open", "high", "low", "close", "volume"]
            frame = frame[columns].sort_values("date").drop_duplicates("date")
            for col in columns[1:]:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
            frame = frame.dropna(subset=["open", "high", "low", "close"])
            typical = (frame["high"] + frame["low"] + frame["close"]) / 3
            day = frame["date"].dt.date
            pv = typical * frame["volume"].fillna(0)
            cumulative_volume = frame["volume"].fillna(0).groupby(day).cumsum()
            frame["vwap"] = pv.groupby(day).cumsum() / cumulative_volume.replace(0, np.nan)
            return frame.reset_index(drop=True)
        except Exception as exc:  # Kite errors are runtime-specific.
            last_error = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Kite historical_data failed: {last_error}")


def pivot_indices(values: np.ndarray, mode: str, order: int = PIVOT_ORDER) -> list[int]:
    result = []
    for i in range(order, len(values) - order):
        center = values[i]
        if not np.isfinite(center):
            continue
        neighbors = np.r_[values[i - order:i], values[i + 1:i + order + 1]]
        if mode == "high" and center >= np.nanmax(neighbors):
            result.append(i)
        elif mode == "low" and center <= np.nanmin(neighbors):
            result.append(i)
    return result


def line_fit(indices: list[int], values: np.ndarray):
    if len(indices) < 2:
        return None
    x = np.asarray(indices[-3:], dtype=float)
    y = values[x.astype(int)]
    if not np.all(np.isfinite(y)):
        return None
    slope, intercept = np.polyfit(x, y, 1)
    return float(slope), float(intercept)


def line_value(line, x: float) -> float:
    return float(line[0] * x + line[1])


def separated(indices: list[int], minimum: int = 4) -> bool:
    return len(indices) >= 2 and all(b - a >= minimum for a, b in zip(indices, indices[1:]))


def similar(values: list[float], tolerance: float) -> bool:
    mean = float(np.mean(values)) if values else 0.0
    return bool(mean and (max(values) - min(values)) / mean <= tolerance)


def crossed(
    close: float,
    boundary: float,
    direction: str,
    prior_close: float | None = None,
    prior_boundary: float | None = None,
    buffer_pct=0.0005,
) -> bool:
    """Require a fresh boundary cross, not mere residence beyond the line."""
    prior_boundary = boundary if prior_boundary is None else prior_boundary
    if direction == "BUY":
        now_beyond = close > boundary * (1 + buffer_pct)
        was_inside = prior_close is None or prior_close <= prior_boundary * (1 + buffer_pct)
    else:
        now_beyond = close < boundary * (1 - buffer_pct)
        was_inside = prior_close is None or prior_close >= prior_boundary * (1 - buffer_pct)
    return bool(now_beyond and was_inside)


def make_signal(symbol, name, family, direction, row, boundary, opposite, height, hist):
    vol_mean = pd.to_numeric(hist["volume"], errors="coerce").tail(20).mean()
    volume_ratio = None if not vol_mean or pd.isna(vol_mean) else float(row["volume"] / vol_mean)
    vwap = row.get("vwap")
    vwap_aligned = None if pd.isna(vwap) else bool(
        row["close"] > vwap if direction == "BUY" else row["close"] < vwap
    )
    return Signal(
        symbol=symbol,
        pattern=name,
        family=family,
        direction=direction,
        signal_time=row["date"],
        boundary=float(boundary),
        opposite_boundary=float(opposite),
        pattern_height=max(float(height), float(row["close"]) * 0.001),
        volume_ratio=volume_ratio,
        vwap_aligned=vwap_aligned,
    )


def detect_pattern(symbol: str, hist: pd.DataFrame, row: pd.Series) -> Signal | None:
    """Return one highest-priority pattern breakout from prior completed bars."""
    if len(hist) < 24:
        return None
    work = hist.tail(LOOKBACK).reset_index(drop=True)
    highs, lows, closes = (_numbers(work[c]) for c in ("high", "low", "close"))
    hi_idx = pivot_indices(highs, "high")
    lo_idx = pivot_indices(lows, "low")
    if len(hi_idx) < 2 or len(lo_idx) < 2:
        return None
    hi_line, lo_line = line_fit(hi_idx, highs), line_fit(lo_idx, lows)
    if hi_line is None or lo_line is None:
        return None
    x = len(work)
    upper, lower = line_value(hi_line, x), line_value(lo_line, x)
    price = float(row["close"])
    prior_price = float(work.iloc[-1]["close"])
    upper_prior = line_value(hi_line, x - 1)
    lower_prior = line_value(lo_line, x - 1)
    scale = max(float(np.nanmean(closes[-20:])), 1e-9)
    hi_slope, lo_slope = hi_line[0] / scale, lo_line[0] / scale
    height = max(upper - lower, scale * 0.001)

    # Reversals get priority. Pivots are fully confirmed before the breakout.
    last_hi3, last_lo3 = hi_idx[-3:], lo_idx[-3:]
    if len(last_hi3) == 3 and separated(last_hi3):
        a, b, c = [highs[j] for j in last_hi3]
        shoulder_mean = (a + c) / 2
        neckline = min(lows[last_hi3[0]:last_hi3[2] + 1])
        if abs(a - c) / shoulder_mean <= 0.006 and b > shoulder_mean * 1.006 and crossed(price, neckline, "SELL", prior_price, neckline):
            return make_signal(symbol, "HEAD_AND_SHOULDERS", "bearish_reversal", "SELL", row, neckline, max(a, c), b - neckline, work)
        if similar([a, b, c], 0.004):
            neckline = min(lows[last_hi3[0]:last_hi3[2] + 1])
            if crossed(price, neckline, "SELL", prior_price, neckline):
                return make_signal(symbol, "TRIPLE_TOP", "bearish_reversal", "SELL", row, neckline, max(a, b, c), max(a, b, c) - neckline, work)
    if len(last_lo3) == 3 and separated(last_lo3):
        a, b, c = [lows[j] for j in last_lo3]
        shoulder_mean = (a + c) / 2
        neckline = max(highs[last_lo3[0]:last_lo3[2] + 1])
        if abs(a - c) / shoulder_mean <= 0.006 and b < shoulder_mean * 0.994 and crossed(price, neckline, "BUY", prior_price, neckline):
            return make_signal(symbol, "INVERTED_HEAD_AND_SHOULDERS", "bullish_reversal", "BUY", row, neckline, min(a, c), neckline - b, work)
        if similar([a, b, c], 0.004):
            neckline = max(highs[last_lo3[0]:last_lo3[2] + 1])
            if crossed(price, neckline, "BUY", prior_price, neckline):
                return make_signal(symbol, "TRIPLE_BOTTOM", "bullish_reversal", "BUY", row, neckline, min(a, b, c), neckline - min(a, b, c), work)
    if separated(hi_idx[-2:]):
        a, b = [highs[j] for j in hi_idx[-2:]]
        neckline = min(lows[hi_idx[-2]:hi_idx[-1] + 1])
        if similar([a, b], 0.0035) and crossed(price, neckline, "SELL", prior_price, neckline):
            return make_signal(symbol, "DOUBLE_TOP", "bearish_reversal", "SELL", row, neckline, max(a, b), max(a, b) - neckline, work)
    if separated(lo_idx[-2:]):
        a, b = [lows[j] for j in lo_idx[-2:]]
        neckline = max(highs[lo_idx[-2]:lo_idx[-1] + 1])
        if similar([a, b], 0.0035) and crossed(price, neckline, "BUY", prior_price, neckline):
            return make_signal(symbol, "DOUBLE_BOTTOM", "bullish_reversal", "BUY", row, neckline, min(a, b), neckline - min(a, b), work)

    # Triangles and wedges based on regression through confirmed swing points.
    flat = 0.00020
    trend = 0.00008
    if abs(hi_slope) <= flat and lo_slope > trend and crossed(price, upper, "BUY", prior_price, upper_prior):
        return make_signal(symbol, "ASCENDING_TRIANGLE", "bullish_continuation", "BUY", row, upper, lower, height, work)
    if abs(lo_slope) <= flat and hi_slope < -trend and crossed(price, lower, "SELL", prior_price, lower_prior):
        return make_signal(symbol, "DESCENDING_TRIANGLE", "bearish_continuation", "SELL", row, lower, upper, height, work)
    if hi_slope < -trend and lo_slope > trend:
        if crossed(price, upper, "BUY", prior_price, upper_prior):
            return make_signal(symbol, "BULLISH_SYMMETRICAL_TRIANGLE", "bullish_continuation", "BUY", row, upper, lower, height, work)
        if crossed(price, lower, "SELL", prior_price, lower_prior):
            return make_signal(symbol, "BEARISH_SYMMETRICAL_TRIANGLE", "bearish_continuation", "SELL", row, lower, upper, height, work)
    old_width = line_value(hi_line, max(0, x - 10)) - line_value(lo_line, max(0, x - 10))
    converging = height < old_width * 0.85
    if converging and hi_slope < -trend and lo_slope < -trend and crossed(price, upper, "BUY", prior_price, upper_prior):
        return make_signal(symbol, "FALLING_WEDGE", "bullish_reversal", "BUY", row, upper, lower, max(old_width, height), work)
    if converging and hi_slope > trend and lo_slope > trend and crossed(price, lower, "SELL", prior_price, lower_prior):
        return make_signal(symbol, "RISING_WEDGE", "bearish_reversal", "SELL", row, lower, upper, max(old_width, height), work)

    # Flags: a strong 8-bar impulse followed by a 12-bar countertrend channel.
    if len(work) >= 24:
        impulse_start = float(work.iloc[-20]["close"])
        impulse_end = float(work.iloc[-12]["close"])
        impulse = (impulse_end - impulse_start) / impulse_start
        flag = work.tail(12).reset_index(drop=True)
        flag_hi = np.polyfit(np.arange(12), _numbers(flag["high"]), 1)
        flag_lo = np.polyfit(np.arange(12), _numbers(flag["low"]), 1)
        flag_upper = float(np.polyval(flag_hi, 12))
        flag_lower = float(np.polyval(flag_lo, 12))
        flag_upper_prior = float(np.polyval(flag_hi, 11))
        flag_lower_prior = float(np.polyval(flag_lo, 11))
        parallel = abs(flag_hi[0] - flag_lo[0]) / scale <= 0.00025
        if impulse >= 0.012 and parallel and flag_hi[0] < 0 and flag_lo[0] < 0 and crossed(price, flag_upper, "BUY", prior_price, flag_upper_prior):
            return make_signal(symbol, "BULLISH_FLAG", "bullish_continuation", "BUY", row, flag_upper, flag_lower, abs(impulse_end - impulse_start), work)
        if impulse <= -0.012 and parallel and flag_hi[0] > 0 and flag_lo[0] > 0 and crossed(price, flag_lower, "SELL", prior_price, flag_lower_prior):
            return make_signal(symbol, "BEARISH_FLAG", "bearish_continuation", "SELL", row, flag_lower, flag_upper, abs(impulse_end - impulse_start), work)

    # The image labels parallel countertrend channels as bullish/bearish wedges.
    if converging is False and hi_slope < -trend and lo_slope < -trend and crossed(price, upper, "BUY", prior_price, upper_prior):
        return make_signal(symbol, "BULLISH_WEDGE_CHANNEL", "bullish_continuation", "BUY", row, upper, lower, height, work)
    if converging is False and hi_slope > trend and lo_slope > trend and crossed(price, lower, "SELL", prior_price, lower_prior):
        return make_signal(symbol, "BEARISH_WEDGE_CHANNEL", "bearish_continuation", "SELL", row, lower, upper, height, work)
    return None


def discover_signals(symbol: str, frame: pd.DataFrame, start_date, end_date) -> list[Signal]:
    signals = []
    prior_key = None
    for i in range(LOOKBACK, len(frame) - 1):
        row = frame.iloc[i]
        if not (start_date <= row["date"].date() <= end_date):
            continue
        if not (clock_time(9, 30) <= row["date"].time() <= clock_time(14, 57)):
            continue
        day_frame = frame.loc[
            (frame["date"].dt.date == row["date"].date()) &
            (frame["date"] < row["date"])
        ]
        signal = detect_pattern(symbol, day_frame, row)
        if signal is None:
            continue
        key = (signal.pattern, signal.direction)
        if key == prior_key:
            continue
        signals.append(signal)
        prior_key = key
    return signals


def trade_costs(direction, qty, entry, exit_price):
    if net_pnl_for_trade is not None:
        return net_pnl_for_trade(direction, qty, entry, exit_price)
    gross = (exit_price - entry) * qty * (1 if direction == "BUY" else -1)
    turnover = (entry + exit_price) * qty
    estimated = turnover * 0.00055
    return {"gross_pnl": gross, "costs": estimated, "net_pnl": gross - estimated}


def simulate(frame: pd.DataFrame, signal: Signal, variant: str, capital: float):
    after = frame.loc[frame["date"] > signal.signal_time].copy()
    after = after.loc[after["date"].dt.date == signal.signal_time.date()]
    if after.empty:
        return None
    entry_row = after.iloc[0]
    entry = float(entry_row["open"])
    sign = 1 if signal.direction == "BUY" else -1
    if variant == "fixed":
        stop = entry * (1 - sign * 0.0045)
        target = entry * (1 + sign * 0.0070)
    else:
        raw_stop = signal.opposite_boundary
        buffer = entry * 0.001
        stop = raw_stop - buffer if signal.direction == "BUY" else raw_stop + buffer
        target = entry + sign * signal.pattern_height
    risk_per_share = (entry - stop) * sign
    reward_per_share = (target - entry) * sign
    risk_pct = risk_per_share / entry * 100
    rr = reward_per_share / risk_per_share if risk_per_share > 0 else -1
    if risk_per_share <= 0 or reward_per_share <= 0:
        return None
    if variant == "structural" and (risk_pct < 0.15 or risk_pct > 2.0 or rr < 1.5):
        return None
    qty = int((capital * 0.002) / risk_per_share)
    if qty <= 0:
        return None
    exit_price, exit_time, reason = None, None, None
    for _, bar in after.iterrows():
        stop_hit = float(bar["low"]) <= stop if signal.direction == "BUY" else float(bar["high"]) >= stop
        target_hit = float(bar["high"]) >= target if signal.direction == "BUY" else float(bar["low"]) <= target
        if stop_hit:  # conservative when both occur in the same candle
            exit_price, exit_time, reason = stop, bar["date"], "STOP"
            break
        if target_hit:
            exit_price, exit_time, reason = target, bar["date"], "TARGET"
            break
        if bar["date"].time() >= clock_time(15, 9):
            exit_price, exit_time, reason = float(bar["close"]), bar["date"], "EOD"
            break
    if exit_price is None:
        last = after.iloc[-1]
        exit_price, exit_time, reason = float(last["close"]), last["date"], "EOD"
    pnl = trade_costs(signal.direction, qty, entry, exit_price)
    return {
        **asdict(signal),
        "signal_time": signal.signal_time.isoformat(),
        "variant": variant,
        "entry_time": entry_row["date"].isoformat(),
        "exit_time": exit_time.isoformat(),
        "entry": entry,
        "stop": stop,
        "target": target,
        "risk_pct": risk_pct,
        "reward_risk": rr,
        "qty": qty,
        "exit": exit_price,
        "exit_reason": reason,
        **{k: float(v) for k, v in pnl.items()},
    }


def stats(rows):
    trades = len(rows)
    wins = sum(r["net_pnl"] > 0 for r in rows)
    losses = sum(r["net_pnl"] < 0 for r in rows)
    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / trades * 100 if trades else 0.0,
        "gross_pnl": sum(r["gross_pnl"] for r in rows),
        "costs": sum(r["costs"] for r in rows),
        "net_pnl": sum(r["net_pnl"] for r in rows),
        "profit_factor": (
            sum(max(r["net_pnl"], 0) for r in rows) /
            abs(sum(min(r["net_pnl"], 0) for r in rows))
            if any(r["net_pnl"] < 0 for r in rows) else None
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist", default="runtime/historical_all_nse_watchlist_20260814_0927.json")
    parser.add_argument("--from-date", default="2026-08-10")
    parser.add_argument("--to-date", default="2026-08-14")
    parser.add_argument("--capital", type=float, default=5000.0)
    parser.add_argument("--output", default="runtime/chart_pattern_replay_20260810_20260814.json")
    parser.add_argument("--csv", default="runtime/chart_pattern_replay_20260810_20260814.csv")
    args = parser.parse_args()

    start_date = pd.Timestamp(args.from_date).date()
    end_date = pd.Timestamp(args.to_date).date()
    symbols = load_watchlist(Path(args.watchlist))
    kite = get_kite_client()
    tokens = {
        str(item.get("tradingsymbol")): int(item["instrument_token"])
        for item in kite.instruments("NSE")
        if item.get("instrument_type") == "EQ"
    }
    fetch_start = datetime.combine(start_date - timedelta(days=10), clock_time(9, 15), IST)
    fetch_end = datetime.combine(end_date, clock_time(15, 30), IST)
    signals, frames, failures = [], {}, {}
    for number, symbol in enumerate(symbols, 1):
        token = tokens.get(symbol)
        if token is None:
            failures[symbol] = "TOKEN_NOT_FOUND"
            continue
        try:
            frame = fetch_frame(kite, token, fetch_start, fetch_end)
            frames[symbol] = frame
            found = discover_signals(symbol, frame, start_date, end_date)
            signals.extend(found)
            print(f"FETCH {number}/{len(symbols)} NSE:{symbol} candles={len(frame)} signals={len(found)}")
            time.sleep(0.35)
        except Exception as exc:
            failures[symbol] = str(exc)
            print(f"FAIL {number}/{len(symbols)} NSE:{symbol} {exc}")

    signals.sort(key=lambda s: (s.signal_time, s.symbol, s.pattern))
    trades = []
    daily_count = Counter()
    last_exit = {}
    for signal in signals:
        day = signal.signal_time.date().isoformat()
        for variant in ("fixed", "structural"):
            key = (variant, day, signal.symbol)
            if daily_count[(variant, day)] >= 100:
                continue
            previous_exit = last_exit.get(key)
            if previous_exit is not None and signal.signal_time <= previous_exit:
                continue
            trade = simulate(frames[signal.symbol], signal, variant, args.capital)
            if trade is None:
                continue
            trades.append(trade)
            daily_count[(variant, day)] += 1
            last_exit[key] = pd.Timestamp(trade["exit_time"])

    by_variant = {}
    by_pattern = {}
    by_day = {}
    for variant in ("fixed", "structural"):
        subset = [r for r in trades if r["variant"] == variant]
        by_variant[variant] = stats(subset)
        for pattern in sorted({r["pattern"] for r in subset}):
            by_pattern[f"{variant}:{pattern}"] = stats([r for r in subset if r["pattern"] == pattern])
        for day in sorted({r["signal_time"][:10] for r in subset}):
            by_day[f"{variant}:{day}"] = stats([r for r in subset if r["signal_time"].startswith(day)])

    report = {
        "method": "point-in-time classical pattern breakout replay",
        "universe": str(args.watchlist),
        "date_range": [args.from_date, args.to_date],
        "timeframe": "3minute",
        "entry": "next candle open",
        "intrabar_policy": "stop before target",
        "capital": args.capital,
        "risk_per_trade_pct": 0.2,
        "signals": len(signals),
        "fetch_failures": failures,
        "summary_by_variant": by_variant,
        "summary_by_pattern": by_pattern,
        "summary_by_day": by_day,
        "trades": trades,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    pd.DataFrame(trades).to_csv(args.csv, index=False)

    print("\nCHART PATTERN REPLAY")
    print(f"WATCHLIST={len(symbols)} SIGNALS={len(signals)} FAILURES={len(failures)}")
    for variant, result in by_variant.items():
        print(
            f"{variant.upper():10s} trades={result['trades']:3d} "
            f"wins={result['wins']:3d} losses={result['losses']:3d} "
            f"win_rate={result['win_rate']:6.2f}% "
            f"gross=Rs {result['gross_pnl']:+.2f} costs=Rs {result['costs']:.2f} "
            f"net=Rs {result['net_pnl']:+.2f} PF={result['profit_factor']}"
        )
    print("\nPER PATTERN")
    for key, result in sorted(by_pattern.items()):
        print(f"{key:48s} trades={result['trades']:3d} win_rate={result['win_rate']:6.2f}% net=Rs {result['net_pnl']:+.2f}")
    print(f"\nJSON={args.output}")
    print(f"CSV={args.csv}")


if __name__ == "__main__":
    main()
