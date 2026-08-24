"""
PREMIUM_ROTATION_SHADOW -- cost-aware P&L (reusing fno_bot/reporting/
costs.py's rate structure) and JSONL logging (section 16).

Cost figures inherit the same honesty requirement already established
for INTRADAY_OPTIONS_V1: every net-P&L figure must be labeled as an
estimate using unverified configurable rates, not presented as exact,
until reconciled against a real contract note.
"""
import json
import os
from dataclasses import asdict
from typing import List

from fno_bot.strategies.premium_rotation_session import ClosedTrade, CounterfactualResult

# Same rate structure/shape as fno_bot/reporting/costs.py -- duplicated
# here rather than imported, so a future change to the opening-scalper's
# cost module can't silently change this strategy's numbers without a
# deliberate edit. Rates are equally unverified; same disclaimer applies.
BROKERAGE_PER_ORDER = 20.0
STT_SELL_RATE = 0.001
EXCHANGE_TXN_RATE = 0.0003503
SEBI_CHARGES_RATE = 0.0000005
GST_RATE = 0.18
STAMP_DUTY_BUY_RATE = 0.00003

COST_DISCLAIMER = "ESTIMATED using unverified configurable charge rates -- not reconciled against a real contract note"


def estimate_trade_cost(buy_premium_value: float, sell_premium_value: float) -> float:
    brokerage = BROKERAGE_PER_ORDER * 2
    stt = sell_premium_value * STT_SELL_RATE
    exchange_txn = (buy_premium_value + sell_premium_value) * EXCHANGE_TXN_RATE
    sebi_charges = (buy_premium_value + sell_premium_value) * SEBI_CHARGES_RATE
    gst = GST_RATE * (brokerage + exchange_txn + sebi_charges)
    stamp_duty = buy_premium_value * STAMP_DUTY_BUY_RATE
    return brokerage + stt + exchange_txn + sebi_charges + gst + stamp_duty


def net_pnl_for_closed_trade(trade: ClosedTrade) -> dict:
    buy_value = trade.quantity * trade.entry_price
    sell_value = trade.quantity * trade.exit_price
    gross_pnl = (trade.exit_price - trade.entry_price) * trade.quantity
    costs = estimate_trade_cost(buy_value, sell_value)
    return {
        "gross_pnl": round(gross_pnl, 2),
        "estimated_costs": round(costs, 2),
        "net_pnl_estimate": round(gross_pnl - costs, 2),
        "cost_rates_used": {
            "brokerage_per_order": BROKERAGE_PER_ORDER, "stt_sell_rate": STT_SELL_RATE,
            "exchange_txn_rate": EXCHANGE_TXN_RATE, "sebi_charges_rate": SEBI_CHARGES_RATE,
            "gst_rate": GST_RATE, "stamp_duty_buy_rate": STAMP_DUTY_BUY_RATE,
        },
        "disclaimer": COST_DISCLAIMER,
    }


# --- JSONL logging (section 16) ------------------------------------------

class RotationAuditLog:
    """Append-only JSONL writer. Never raises into the trading path --
    a logging failure must not be allowed to affect strategy decisions,
    matching the equity/F&O bots' existing 'reporting failures logged,
    never raised' principle."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def _write(self, record: dict):
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception:
            pass   # never let a logging failure interrupt the strategy

    def log_observation(self, tick_record) -> None:
        payload = {
            "event": "OBSERVATION",
            "timestamp": tick_record.timestamp,
            "underlying_price": tick_record.underlying_price,
            "ce_price": tick_record.ce_price,
            "pe_price": tick_record.pe_price,
            "position_open": tick_record.position_open,
        }
        if tick_record.eligibility is not None:
            e = tick_record.eligibility
            payload.update({
                "classification": e.classification, "confirmed": e.confirmed,
                "ce_score": e.ce_score, "pe_score": e.pe_score,
                "entry_eligible": e.eligible, "rejection_reasons": e.rejections,
            })
        if tick_record.trade_opened is not None:
            payload["trade_opened"] = tick_record.trade_opened
        self._write(payload)

    def log_trade_closed(self, trade: ClosedTrade, pnl: dict) -> None:
        payload = {"event": "TRADE_CLOSED", **asdict(trade), **pnl}
        self._write(payload)

    def log_counterfactual(self, cf: CounterfactualResult) -> None:
        payload = {"event": "COUNTERFACTUAL", **asdict(cf)}
        self._write(payload)

    def read_all(self) -> List[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
