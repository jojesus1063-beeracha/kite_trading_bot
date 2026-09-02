"""Regression tests for the production macro-authorization policy."""

from types import SimpleNamespace

import pandas as pd

from strategy import _macro_authorization


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print("PASS:", name)


cfg = SimpleNamespace(ADX_THRESHOLD=25)
stock_15m = pd.DataFrame()
as_of = pd.Timestamp("2026-08-10 10:00:00")


def authorize(macro_state, direction):
    return _macro_authorization(
        macro_state,
        direction,
        stock_15m,
        as_of,
        cfg,
    )


decision, detail = authorize("BULLISH", "BUY")
check("BULLISH authorizes BUY", decision == "ALLOW")
check("BULLISH BUY keeps normal decision", detail["decision"] == "ALLOW_NORMAL")

decision, detail = authorize("BULLISH", "SELL")
check("BULLISH rejects SELL", decision == "REJECT")
check("BULLISH SELL is an opposing hard reject", detail["reason"] == "NIFTY_OPPOSING")

decision, detail = authorize("BEARISH", "SELL")
check("BEARISH authorizes SELL", decision == "ALLOW")

decision, detail = authorize("BEARISH", "BUY")
check("BEARISH rejects BUY", decision == "REJECT")

for direction in ("BUY", "SELL"):
    decision, detail = authorize("NEUTRAL", direction)
    check(f"genuine NEUTRAL authorizes {direction}", decision == "ALLOW")
    check(
        f"genuine NEUTRAL {direction} is explicitly identified",
        detail["decision"] == "ALLOW_NEUTRAL",
    )

decision, detail = authorize("UNKNOWN", "BUY")
check("unrecognized macro state fails closed", decision == "REJECT")
check(
    "unrecognized macro state is not relabeled NEUTRAL",
    detail["decision"] == "REJECT_INVALID_MACRO_STATE",
)

print("\nMacro neutral-pass integration tests passed.")
