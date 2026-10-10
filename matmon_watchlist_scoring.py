#!/usr/bin/env python3
"""Matmon HaElohim pre-market watchlist scoring.

This module is selection/ranking ONLY. It never places, modifies, or
cancels a broker order, never touches PAPER_TRADING or live-acknowledgement
logic, and never changes Matmon's EMA3/EMA15 -> DI14 -> CLEAN quote ->
microstructure entry/exit authorization. Its only job is choosing which
MATMON_WATCHLIST_SIZE symbols Matmon gets to observe intraday.

Two scoring models are provided side by side:

  score_model_a() -- the CURRENT production formula, unchanged:
      45% liquidity (min-max) + 25% volume (min-max)
      + 20% spread quality + 10% abs(imbalance)

  score_model_b() -- a RESEARCH-ONLY proposed formula:
      25% liquidity + 25% activity + 20% displacement
      + 15% spread + 15% directional pressure
      all percentile-normalized across the eligible universe.

  Model B is not wired into the operational watchlist by anything in this
  module. Promoting it requires historical replay evidence -- see
  matmon_watchlist_replay.py.
"""
from __future__ import annotations

import math
from typing import Any

# ---------------------------------------------------------------------------
# Hard eligibility -- unchanged semantics from matmon_preopen_top120.evaluate(),
# extracted here so eligibility and ranking are clearly separate concerns.
# ---------------------------------------------------------------------------


def num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def depth_side_value(levels: list[dict] | None) -> float:
    total = 0.0
    for level in levels or []:
        if not isinstance(level, dict):
            continue
        total += num(level.get("price")) * num(level.get("quantity"))
    return total


def hard_eligibility(
    row: dict,
    quote: dict | None,
    *,
    min_price: float,
    max_price: float,
    max_spread_pct: float,
    min_depth_value: float,
) -> tuple[dict | None, str | None]:
    """Return (candidate, None) if eligible, else (None, rejection_reason).

    A candidate that fails here never enters ranking -- ranking factors
    (gap, imbalance, etc.) never act as additional hard gates.
    """
    if not isinstance(quote, dict):
        return None, "missing_quote"

    last = num(quote.get("last_price"))
    if not (min_price <= last <= max_price):
        return None, "price_out_of_range"

    depth = quote.get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []
    if not buys or not sells:
        return None, "missing_depth_side"

    bid = num(buys[0].get("price"))
    ask = num(sells[0].get("price"))
    if bid <= 0 or ask <= 0 or ask < bid:
        return None, "invalid_bid_ask"

    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None, "invalid_mid"

    spread_pct = ((ask - bid) / mid) * 100.0
    if spread_pct > max_spread_pct:
        return None, "spread_too_wide"

    buy_value = depth_side_value(buys)
    sell_value = depth_side_value(sells)
    depth_value = buy_value + sell_value
    if depth_value < min_depth_value:
        return None, "insufficient_depth"

    volume = num(quote.get("volume"))
    ohlc = quote.get("ohlc") or {}
    prev_close = num(ohlc.get("close"))
    gap_pct = ((last - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0

    # Suspicious-quote flag: bid==ask is either an extremely tight/locked
    # market or a stale/invalid snapshot. Persist the flag; do not silently
    # reward it with a perfect spread score in ranking (see score_model_b).
    suspicious_locked_quote = bid == ask

    buy_sell_signed = (
        (buy_value - sell_value) / depth_value if depth_value > 0 else 0.0
    )

    # Best-effort CAS/call-auction field capture. Field names are NOT
    # confirmed to exist in Kite's quote payload during 09:00-09:15 -- these
    # are observational passthroughs only, never used in scoring, and are
    # simply absent (None) if Kite doesn't expose them. Do not assume any
    # of these are populated until verified against a real quote dump.
    cas_observational = {
        key: quote.get(key)
        for key in (
            "oi", "oi_day_high", "oi_day_low",  # present for some segments;
            # kept only in case a given instrument/segment surfaces them.
        )
        if key in quote
    } or None

    candidate = {
        "symbol": row["symbol"],
        "exchange": row["exchange"],
        "last_price": round(last, 4),
        "bid": round(bid, 4),
        "ask": round(ask, 4),
        "spread_pct": round(spread_pct, 6),
        "buy_depth_value": round(buy_value, 2),
        "sell_depth_value": round(sell_value, 2),
        "depth_value": round(depth_value, 2),
        "imbalance": round(abs(buy_sell_signed), 6),
        "imbalance_signed": round(buy_sell_signed, 6),
        "imbalance_direction": (
            "BUY" if buy_sell_signed > 0 else "SELL" if buy_sell_signed < 0 else "FLAT"
        ),
        "volume": int(volume),
        "gap_pct": round(gap_pct, 6),
        "suspicious_locked_quote": suspicious_locked_quote,
        "cas_observational": cas_observational,
    }
    return candidate, None


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def minmax_normalize(rows: list[dict], key: str, out_key: str | None = None) -> None:
    out_key = out_key or (key + "_norm")
    values = [num(r.get(key)) for r in rows]
    if not values:
        return
    lo, hi = min(values), max(values)
    for r in rows:
        v = num(r.get(key))
        r[out_key] = (v - lo) / (hi - lo) if hi > lo else 0.0


def percentile_normalize(rows: list[dict], key: str, out_key: str | None = None) -> None:
    """Rank-based 0..1 normalization -- resistant to extreme outliers,
    unlike min-max where one extreme value compresses everyone else toward 0.
    Ties receive the same averaged rank, so equal underlying values never
    get pushed to different ends of the 0..1 range.
    """
    out_key = out_key or (key + "_pctnorm")
    n = len(rows)
    if n == 0:
        return
    if n == 1:
        rows[0][out_key] = 1.0
        return

    values = [num(r.get(key)) for r in rows]
    order = sorted(range(n), key=lambda i: values[i])

    # Average-rank tie handling: identical values share the mean of the
    # positions they'd otherwise occupy.
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1

    for idx in range(n):
        rows[idx][out_key] = ranks[idx] / (n - 1)


def winsorize(values: list[float], lower_pct: float = 0.05, upper_pct: float = 0.95) -> list[float]:
    if not values:
        return values
    s = sorted(values)
    n = len(s)
    lo = s[max(0, int(round(lower_pct * (n - 1))))]
    hi = s[min(n - 1, int(round(upper_pct * (n - 1))))]
    return [min(max(v, lo), hi) for v in values]


# ---------------------------------------------------------------------------
# Model A -- CURRENT production formula, unchanged.
# ---------------------------------------------------------------------------


def score_model_a(candidates: list[dict]) -> None:
    for c in candidates:
        c["_liq_a"] = math.log1p(c["depth_value"])
        c["_vol_a"] = math.log1p(max(0.0, c["volume"]))
    minmax_normalize(candidates, "_liq_a", "_liq_a_norm")
    minmax_normalize(candidates, "_vol_a", "_vol_a_norm")
    for c in candidates:
        # A locked bid/ask snapshot is often stale rather than genuinely
        # cost-free.  Never award it the old perfect-spread bonus.
        spread_quality = (
            0.0
            if c.get("suspicious_locked_quote")
            else max(0.0, 1.0 - (c["spread_pct"] / _MAX_SPREAD_FOR_QUALITY))
        )
        c["model_a_score"] = round(
            45.0 * c["_liq_a_norm"]
            + 25.0 * c["_vol_a_norm"]
            + 20.0 * spread_quality
            + 10.0 * c["imbalance"],
            6,
        )


_MAX_SPREAD_FOR_QUALITY = 0.25  # matches matmon_preopen_top120.MAX_SPREAD_PCT


# ---------------------------------------------------------------------------
# Model B -- RESEARCH-ONLY proposed formula. Not wired to production
# selection anywhere. Weights are hypotheses, not assumed-optimal values.
# ---------------------------------------------------------------------------

MODEL_B_WEIGHTS = {
    "liquidity": 0.25,
    "activity": 0.25,
    "displacement": 0.20,
    "spread": 0.15,
    "pressure": 0.15,
}


def score_model_b(candidates: list[dict]) -> None:
    if not candidates:
        return

    # Liquidity: log(rupee depth), percentile-normalized.
    for c in candidates:
        c["_liq_b"] = math.log1p(c["depth_value"])
    percentile_normalize(candidates, "_liq_b", "liquidity_score")

    # Activity: raw volume proxy (log), percentile-normalized. True RVOL is
    # not calculable at full-universe pre-open scale -- see the report.
    for c in candidates:
        c["_act_b"] = math.log1p(max(0.0, c["volume"]))
    percentile_normalize(candidates, "_act_b", "activity_score")

    # Displacement: abs(gap_pct), winsorized before normalizing so one 15%
    # abnormal gap doesn't dominate the whole distribution.
    gaps = [abs(c["gap_pct"]) for c in candidates]
    clipped = winsorize(gaps)
    for c, g in zip(candidates, clipped):
        c["_disp_b"] = g
    percentile_normalize(candidates, "_disp_b", "displacement_score")

    # Spread: tighter is better. bid==ask ("suspicious_locked_quote") is
    # explicitly NOT given the maximum score -- it's treated as an unverified
    # quote state, not rewarded as perfect execution quality.
    for c in candidates:
        if c["suspicious_locked_quote"]:
            quality = 0.5  # neutral, not maximal, until verified trustworthy
        else:
            quality = max(0.0, 1.0 - (c["spread_pct"] / _MAX_SPREAD_FOR_QUALITY))
        c["_spread_b"] = quality
    percentile_normalize(candidates, "_spread_b", "spread_score")

    # Pressure: abs(signed imbalance), percentile-normalized. Sign is
    # persisted separately (imbalance_direction) for later analysis only --
    # never used as an entry/direction gate here or anywhere downstream.
    for c in candidates:
        c["_press_b"] = abs(c["imbalance_signed"])
    percentile_normalize(candidates, "_press_b", "pressure_score")

    for c in candidates:
        c["model_b_score"] = round(
            MODEL_B_WEIGHTS["liquidity"] * c["liquidity_score"]
            + MODEL_B_WEIGHTS["activity"] * c["activity_score"]
            + MODEL_B_WEIGHTS["displacement"] * c["displacement_score"]
            + MODEL_B_WEIGHTS["spread"] * c["spread_score"]
            + MODEL_B_WEIGHTS["pressure"] * c["pressure_score"],
            6,
        )
