#!/usr/bin/env python3
"""Replay 10-Aug-2026 and 11-Aug-2026 with the proposed three-zone ADX policy.

Analysis only. Reuses the verified 0.20% risk / current PAPER exit-stack replay.
No orders can be placed by this script.

Policy:
- ADX <20      => NORMAL EMA9/EMA21 direction
- 20 <= ADX <40 => REVERSED EMA9/EMA21 direction
- ADX >=40     => NORMAL EMA9/EMA21 direction
- RSI >=70 BUY override; RSI <=30 SELL override
- ADX never blocks an entry
- no daily entry-count cap
- no completed-trades-per-symbol cap
- no post-loss cooldown
- no max-open-position frequency cap in replay
- 5% realized daily-loss halt retained
- risk/trade 0.20%
- emergency stop 0.75%
- current MAE/MFE/hybrid/dead-trade/square-off rules retained

Missing/unavailable ADX preserves the prior fail-safe behavior and uses REVERSED
EMA direction because no ADX zone can be established.
"""
from __future__ import annotations

import math
import builtins

import replay_direction_only_two_days_20260810_11 as replay


def three_zone_direction(adx, ema9, ema21, rsi):
    """Return final direction for NORMAL / REVERSE / NORMAL ADX zones."""
    if adx is None or not math.isfinite(float(adx)):
        normal = False
    else:
        value = float(adx)
        normal = value < 20.0 or value >= 40.0

    if ema9 > ema21:
        base_direction = "BUY" if normal else "SELL"
    elif ema9 < ema21:
        base_direction = "SELL" if normal else "BUY"
    else:
        return None, None, None

    override = None
    if rsi is not None and math.isfinite(float(rsi)):
        if float(rsi) >= replay.base.RSI_OVERBOUGHT:
            override = "BUY"
        elif float(rsi) <= replay.base.RSI_OVERSOLD:
            override = "SELL"

    return override or base_direction, base_direction, override


# The existing two-day harness already implements the exact requested risk,
# sizing, costs, emergency stop, MAE/MFE, hybrid and square-off mechanics. Only
# replace its direction selector so this test isolates the new ADX regime.
replay.base.proposed_direction = three_zone_direction


# Keep the reused harness output truthful by replacing its old policy captions.
def policy_print(*args, **kwargs):
    mapped = []
    for value in args:
        if isinstance(value, str):
            value = value.replace(
                "ADX <20 REVERSE | ADX >=20 NORMAL | no ADX/frequency caps | emergency stop 0.75%",
                "ADX <20 NORMAL | 20<=ADX<40 REVERSE | ADX >=40 NORMAL | no ADX/frequency caps | emergency stop 0.75%",
            )
            value = value.replace(
                "TWO-DAY COMPARISON — VERIFIED PAPER POLICY",
                "TWO-DAY COMPARISON — THREE-ZONE ADX PAPER POLICY",
            )
        mapped.append(value)
    builtins.print(*mapped, **kwargs)


replay.print = policy_print


if __name__ == "__main__":
    policy_print(
        "THREE-ZONE ADX REPLAY: ADX<20 NORMAL | 20<=ADX<40 REVERSE | ADX>=40 NORMAL | ADX BLOCK=OFF"
    )
    replay.main()
