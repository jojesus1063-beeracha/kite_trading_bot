#!/usr/bin/env python3
"""PAPER-only launcher for the three-zone ADX direction policy.

This wrapper reuses the current PAPER risk/exit stack and changes only ADX's
directional interpretation:
- ADX <20       -> NORMAL EMA9/EMA21
- 20 <= ADX <40 -> REVERSED EMA9/EMA21
- ADX >=40      -> NORMAL EMA9/EMA21

ADX never blocks an entry. Existing 0.20% risk, disabled frequency caps,
5% daily-loss halt, 0.75% emergency stop, MAE/MFE, hybrid and 40-minute
dead-trade rules remain inherited from paper_50pct_risk_launcher.py.
"""
from __future__ import annotations

import json
import logging

import paper_50pct_risk_launcher as current

logger = logging.getLogger("paper_three_zone_adx_launcher")


def install_three_zone_adx_policy() -> None:
    if not bool(getattr(current.cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: three-zone ADX launcher requires PAPER_TRADING=True")

    import paper_contrarian_launcher as base

    current.cfg.PAPER_ADX_LOW_NORMAL_BELOW = 20.0
    current.cfg.PAPER_ADX_REVERSE_FROM = 20.0
    current.cfg.PAPER_ADX_HIGH_NORMAL_FROM = 40.0
    current.cfg.PAPER_ADX_BLOCK_LOW = 0.0
    current.cfg.PAPER_ADX_BLOCK_HIGH = 0.0

    base._adx_entry_blocked = lambda adx: False

    def allow_symbol_guard(symbol, now=None, log_path=None):
        return True, {
            "paper_only": True,
            "active": False,
            "decision": "ALLOW",
            "reason": "PAPER_FREQUENCY_GUARDS_DISABLED",
        }

    base._paper_entry_guard = allow_symbol_guard

    def three_zone_ema_direction(df, adx=None):
        if df is None or df.empty or "close" not in df.columns or len(df) < base.EMA_SLOW:
            return None, None, None

        close = base.pd.to_numeric(df["close"], errors="coerce")
        e9 = close.ewm(span=base.EMA_FAST, adjust=False).mean().iloc[-1]
        e21 = close.ewm(span=base.EMA_SLOW, adjust=False).mean().iloc[-1]
        if base.pd.isna(e9) or base.pd.isna(e21):
            return None, None, None

        if adx is None:
            normal = False
        else:
            value = float(adx)
            normal = value < 20.0 or value >= 40.0

        if e9 > e21:
            direction = "BUY" if normal else "SELL"
        elif e9 < e21:
            direction = "SELL" if normal else "BUY"
        else:
            direction = None
        return direction, float(e9), float(e21)

    base.ema_direction = three_zone_ema_direction

    # Base evaluator's single-threshold label cannot represent the low-NORMAL
    # zone. Keep >=40 labeling correct, then sanitize persisted audit events.
    base.ADX_REGIME_THRESHOLD = 40.0 - 1e-9
    original_append = base._append
    if not getattr(original_append, "_three_zone_adx_sanitizer", False):
        def append_three_zone(payload):
            if isinstance(payload, dict) and payload.get("event") == "ENTRY_EVALUATION":
                adx = payload.get("adx")
                if adx is None:
                    regime = "REVERSED_ADX_UNAVAILABLE"
                else:
                    value = float(adx)
                    regime = "NORMAL" if value < 20.0 or value >= 40.0 else "REVERSED"
                payload["adx_regime"] = regime
                payload["adx_blocks_trade"] = False
                policy = payload.setdefault("directional_policy", {})
                policy.clear()
                policy.update({
                    "adx_lt_20": "NORMAL_EMA",
                    "adx_20_to_lt_40": "REVERSED_EMA",
                    "adx_gte_40": "NORMAL_EMA",
                    "adx_unavailable": "REVERSED_EMA_FAILSAFE",
                    "normal_ema9_gt_ema21": "BUY",
                    "normal_ema9_lt_ema21": "SELL",
                    "reversed_ema9_gt_ema21": "SELL",
                    "reversed_ema9_lt_ema21": "BUY",
                    "rsi_gte_70": "BUY_OVERRIDE",
                    "rsi_lte_30": "SELL_OVERRIDE",
                    "rsi_30_70": "PASS_ADX_EMA_DIRECTION",
                    "adx_entry_block": "OFF",
                })
            original_append(payload)

        append_three_zone._three_zone_adx_sanitizer = True
        base._append = append_three_zone

    original_snapshot = base._config_snapshot
    if not getattr(original_snapshot, "_three_zone_adx_snapshot", False):
        def snapshot_three_zone():
            original_snapshot()
            try:
                data = json.loads(base.CONFIG_AUDIT.read_text(encoding="utf-8"))
                data.update({
                    "PAPER_ADX_ROLE": "DIRECTION_ONLY_NEVER_BLOCK",
                    "PAPER_ADX_POLICY": "ADX<20_NORMAL__20<=ADX<40_REVERSE__ADX>=40_NORMAL",
                    "PAPER_ADX_ENTRY_BLOCK": "DISABLED",
                    "PAPER_ENTRY_CAP": "DISABLED",
                    "PAPER_PER_SYMBOL_COMPLETED_ENTRY_CAP": "DISABLED",
                    "PAPER_LOSS_REENTRY_COOLDOWN": "DISABLED",
                })
                base.CONFIG_AUDIT.write_text(
                    json.dumps(data, indent=2, default=str), encoding="utf-8"
                )
            except Exception as exc:
                logger.warning("Could not augment three-zone PAPER audit: %s", exc)

        snapshot_three_zone._three_zone_adx_snapshot = True
        base._config_snapshot = snapshot_three_zone

    logger.warning(
        "PAPER THREE-ZONE ADX ACTIVE: ADX<20 NORMAL; 20<=ADX<40 REVERSE; ADX>=40 NORMAL; no ADX entry block"
    )


# Replace only the ADX policy installer used by the verified current launcher.
current.install_direction_only_adx_policy = install_three_zone_adx_policy


if __name__ == "__main__":
    current.main()
