"""
Counterfactual/shadow capture (spec #22): even when the bot does NOT
trade, keep recording what happened, so evidence accumulates for
later evaluation of candidate signals, targets, and stops.

Pure state-tracking class (no I/O) + a thin JSONL writer, same split
as the rest of this package. Runs unconditionally every session,
independent of MODE -- SHADOW, PAPER, and LIVE sessions all produce
this data, since it costs nothing and answers exactly the question
spec's FINAL OBJECTIVE poses: "would this have worked, with real
prices, after the fact."
"""
import json
import os
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fno_bot.json_safe import json_safe

logger = logging.getLogger("fno.shadow_log")

IST = ZoneInfo("Asia/Kolkata")
SHADOW_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shadow_logs")


@dataclass
class ShadowTracker:
    """One instance per signal-evaluation moment (whether or not a
    trade was actually taken). Tracks CE and PE price at each
    configured horizon relative to `start_monotonic`, plus running
    MFE/MAE for each leg, so target/stop counterfactuals can be
    replayed after the fact for ANY (leg, target_pct, stop_pct)
    combination -- not just the ones actually configured live."""
    start_monotonic: float
    horizons_seconds: tuple
    reference_ce_price: float
    reference_pe_price: float
    captured: dict = field(default_factory=dict)          # horizon -> {"ce": float, "pe": float}
    ce_max: float = None
    ce_min: float = None
    pe_max: float = None
    pe_min: float = None

    def __post_init__(self):
        self.ce_max = self.ce_max if self.ce_max is not None else self.reference_ce_price
        self.ce_min = self.ce_min if self.ce_min is not None else self.reference_ce_price
        self.pe_max = self.pe_max if self.pe_max is not None else self.reference_pe_price
        self.pe_min = self.pe_min if self.pe_min is not None else self.reference_pe_price

    def pending_horizons(self, now_monotonic: float) -> list:
        elapsed = now_monotonic - self.start_monotonic
        return [h for h in sorted(self.horizons_seconds) if elapsed >= h and h not in self.captured]

    def update(self, now_monotonic: float, ce_price: float, pe_price: float):
        """Call on every tick (or every monitoring cycle) with the
        current CE/PE prices -- updates running MFE/MAE and captures
        any horizon whose time has now arrived."""
        self.ce_max = max(self.ce_max, ce_price)
        self.ce_min = min(self.ce_min, ce_price)
        self.pe_max = max(self.pe_max, pe_price)
        self.pe_min = min(self.pe_min, pe_price)
        for horizon in self.pending_horizons(now_monotonic):
            self.captured[horizon] = {"ce": ce_price, "pe": pe_price}

    def is_complete(self) -> bool:
        return len(self.captured) == len(self.horizons_seconds)

    def counterfactual_outcome(self, leg: str, target_pct: float, stop_pct: float) -> Optional[str]:
        """
        Replays "what if we'd bought `leg` (CE or PE) at the reference
        price with this target/stop?" against the captured horizon
        snapshots, in TIME ORDER, so whichever would have fired first
        is reported -- never assumes target beats stop or vice versa.
        Returns "TARGET_HIT" / "STOP_HIT" / "NEITHER_YET" / None (no
        data captured yet for this leg).
        """
        reference = self.reference_ce_price if leg == "CE" else self.reference_pe_price
        if reference <= 0 or not self.captured:
            return None
        target_price = reference * (1 + target_pct / 100)
        stop_price = reference * (1 - stop_pct / 100)

        for horizon in sorted(self.captured.keys()):
            price = self.captured[horizon].get("ce" if leg == "CE" else "pe")
            if price is None:
                continue
            if price <= stop_price:
                return "STOP_HIT"
            if price >= target_price:
                return "TARGET_HIT"
        return "NEITHER_YET"

    def to_record(self) -> dict:
        return {
            "reference_ce_price": self.reference_ce_price, "reference_pe_price": self.reference_pe_price,
            "captured_horizons": {str(k): v for k, v in self.captured.items()},
            "ce_mfe_pct": (self.ce_max - self.reference_ce_price) / self.reference_ce_price * 100
                          if self.reference_ce_price else None,
            "ce_mae_pct": (self.ce_min - self.reference_ce_price) / self.reference_ce_price * 100
                          if self.reference_ce_price else None,
            "pe_mfe_pct": (self.pe_max - self.reference_pe_price) / self.reference_pe_price * 100
                          if self.reference_pe_price else None,
            "pe_mae_pct": (self.pe_min - self.reference_pe_price) / self.reference_pe_price * 100
                          if self.reference_pe_price else None,
        }


def log_shadow_record(record: dict) -> bool:
    """Append-only JSONL, one file per trading day -- same
    fail-safe-never-raise contract as audit/event_log.py (spec #31)."""
    try:
        os.makedirs(SHADOW_LOG_DIR, exist_ok=True)
        path = os.path.join(SHADOW_LOG_DIR, f"shadow_{datetime.now(IST).date().isoformat()}.jsonl")
        full = {"timestamp_ist": datetime.now(IST).isoformat(), **record}
        with open(path, "a") as f:
            f.write(json.dumps(json_safe(full), default=str) + "\n")
        return True
    except Exception as e:
        logger.error(f"log_shadow_record failed: {e}")
        return False
