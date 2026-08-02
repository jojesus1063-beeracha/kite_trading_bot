from copy import deepcopy

import pandas as pd

import main as main_module


passed = 0
failed = 0


def check(name, condition):
    global passed, failed

    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


class FakeRisk:
    def __init__(self):
        self.results = []

    def record_trade_result(self, pnl):
        self.results.append(pnl)


def complete_result(
    qty,
    average_price,
    status="COMPLETE",
    pending=False,
):
    return {
        "success": qty > 0,
        "order_id": "EXIT-ORDER",
        "operation_id": None,
        "status": status,
        "reason": None,
        "requested_quantity": 10,
        "filled_quantity": qty,
        "average_price": average_price,
        "exit_confirmation_pending": pending,
        "resolved": not pending,
    }


def run_case(exit_result):
    originals = {
        "fetch_candles": main_module.fetch_candles,
        "sleep": main_module.time.sleep,
        "place_exit_order": main_module.place_exit_order,
        "record_trade": main_module.record_trade,
        "save_positions": main_module.save_positions,
        "fixed_target": getattr(
            main_module.cfg,
            "ENABLE_FIXED_TARGET",
            False,
        ),
        "paper": main_module.cfg.PAPER_TRADING,
    }

    trades = []
    saves = []

    open_positions = {
        "TEST": {
            "direction": "BUY",
            "qty": 10,
            "entry": 100.0,
            "stop": 95.0,
            "target": 105.0,
            "exchange": "NSE",
            "peak_price": 100.0,
            "tight_mode": False,
            "entry_time": None,
        }
    }

    risk = FakeRisk()

    try:
        main_module.cfg.ENABLE_FIXED_TARGET = True
        main_module.cfg.PAPER_TRADING = False

        main_module.fetch_candles = (
            lambda *args, **kwargs: pd.DataFrame(
                [
                    {
                        "date": pd.Timestamp(
                            "2026-08-02 10:00"
                        ),
                        "open": 109.0,
                        "high": 111.0,
                        "low": 108.0,
                        "close": 110.0,
                        "volume": 1000,
                    }
                ]
            )
        )

        main_module.time.sleep = lambda *args: None

        main_module.place_exit_order = (
            lambda *args, **kwargs: deepcopy(
                exit_result
            )
        )

        def fake_record_trade(*args, **kwargs):
            trades.append(
                {
                    "args": args,
                    "kwargs": kwargs,
                }
            )

        main_module.record_trade = fake_record_trade

        main_module.save_positions = (
            lambda positions, *args, **kwargs:
            saves.append(deepcopy(positions))
        )

        status = main_module.check_position_exit(
            object(),
            "TEST",
            {"TEST": 123},
            {"TEST": "NSE"},
            open_positions,
            risk,
            check_trend=False,
        )

        return {
            "status": status,
            "positions": open_positions,
            "trades": trades,
            "risk": risk,
            "saves": saves,
        }

    finally:
        main_module.fetch_candles = originals[
            "fetch_candles"
        ]
        main_module.time.sleep = originals["sleep"]
        main_module.place_exit_order = originals[
            "place_exit_order"
        ]
        main_module.record_trade = originals[
            "record_trade"
        ]
        main_module.save_positions = originals[
            "save_positions"
        ]
        main_module.cfg.ENABLE_FIXED_TARGET = originals[
            "fixed_target"
        ]
        main_module.cfg.PAPER_TRADING = originals[
            "paper"
        ]


print("--- Full Confirmed Exit ---")

full = run_case(
    complete_result(
        qty=10,
        average_price=108.50,
    )
)

check(
    "1. Full confirmed fill removes position",
    "TEST" not in full["positions"],
)

check(
    "1b. Full exit records exactly one trade",
    len(full["trades"]) == 1,
)

check(
    "1c. Recorded quantity is confirmed quantity",
    full["trades"][0]["args"][2] == 10,
)

check(
    "1d. Recorded exit uses broker average price",
    full["trades"][0]["args"][4] == 108.50,
)

check(
    "1e. Risk manager receives one confirmed P&L",
    len(full["risk"].results) == 1,
)


print("\n--- Terminal Partial Exit ---")

partial = run_case(
    complete_result(
        qty=4,
        average_price=108.25,
        status="PARTIALLY_FILLED",
    )
)

check(
    "2. Partial exit keeps position open",
    "TEST" in partial["positions"],
)

check(
    "2b. Partial exit leaves only 6 shares",
    partial["positions"]["TEST"]["qty"] == 6,
)

check(
    "2c. Partial exit records only 4 shares",
    partial["trades"][0]["args"][2] == 4,
)

check(
    "2d. Partial exit records broker price",
    partial["trades"][0]["args"][4] == 108.25,
)

check(
    "2e. Status clearly says PARTIAL EXIT",
    "PARTIAL EXIT" in partial["status"],
)


print("\n--- Unresolved Partial Exit ---")

pending_partial = run_case(
    complete_result(
        qty=3,
        average_price=108.10,
        status="TIMEOUT",
        pending=True,
    )
)

check(
    "3. Pending partial reduces quantity by confirmed fill",
    pending_partial["positions"]["TEST"]["qty"] == 7,
)

check(
    "3b. Pending partial records only confirmed 3 shares",
    pending_partial["trades"][0]["args"][2] == 3,
)

check(
    "3c. Pending state is persisted on position",
    pending_partial["positions"]["TEST"][
        "exit_confirmation_pending"
    ] is True,
)


print("\n--- Zero-Fill Pending Exit ---")

zero_pending = run_case(
    complete_result(
        qty=0,
        average_price=None,
        status="TIMEOUT",
        pending=True,
    )
)

check(
    "4. Zero-fill pending exit keeps all 10 shares",
    zero_pending["positions"]["TEST"]["qty"] == 10,
)

check(
    "4b. Zero-fill pending exit records no trade",
    len(zero_pending["trades"]) == 0,
)

check(
    "4c. Zero-fill pending exit records no P&L",
    len(zero_pending["risk"].results) == 0,
)

check(
    "4d. Status clearly says EXIT PENDING",
    "EXIT PENDING" in zero_pending["status"],
)


print("\nResults: "
      + str(passed)
      + " passed, "
      + str(failed)
      + " failed")

if failed:
    raise SystemExit(1)
