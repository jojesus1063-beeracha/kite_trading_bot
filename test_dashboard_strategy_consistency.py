import json
import re
from pathlib import Path

import config as cfg
from trade_levels import (
    fixed_levels_from_fill,
    reward_risk_ratio,
)
import config as runtime_config


def check(name, condition):
    if not condition:
        raise AssertionError(name)

    print("PASS:", name)


user_config = json.loads(
    Path("user_config.json").read_text()
)

main_source = Path("main.py").read_text()
strategy_source = Path("strategy.py").read_text()
dashboard_source = Path("configure_app.py").read_text()

check(
    "Dashboard stop value reaches config.py",
    abs(
        float(cfg.STOP_LOSS_PERCENT)
        - float(user_config["sl_buffer_pct"])
    ) < 1e-12,
)

check(
    "Trading mode setting is a valid boolean",
    isinstance(
        runtime_config.PAPER_TRADING,
        bool,
    ),
)

buy_stop, buy_target = fixed_levels_from_fill(
    "BUY",
    100.0,
    0.45,
    0.70,
)

check(
    "BUY stop is 0.45% below confirmed fill",
    abs(buy_stop - 99.55) < 1e-9,
)

check(
    "BUY target is 0.70% above confirmed fill",
    abs(buy_target - 100.70) < 1e-9,
)

sell_stop, sell_target = fixed_levels_from_fill(
    "SELL",
    100.0,
    0.45,
    0.70,
)

check(
    "SELL stop is 0.45% above confirmed fill",
    abs(sell_stop - 100.45) < 1e-9,
)

check(
    "SELL target is 0.70% below confirmed fill",
    abs(sell_target - 99.30) < 1e-9,
)

check(
    "Configured reward:risk satisfies 1.5",
    reward_risk_ratio(
        user_config["sl_buffer_pct"],
        user_config["profit_target_percent"],
    )
    >= float(user_config["risk_reward_min"]),
)

check(
    "Main calculates levels from confirmed fill",
    bool(
        re.search(
            r"fixed_levels_from_fill\("
            r"\s*signal\.direction,"
            r"\s*confirmed_entry_price,",
            main_source,
            re.S,
        )
    ),
)

check(
    "Position stores recalculated stop",
    '"stop": stop_price,' in main_source,
)

check(
    "Old signal-price target calculation removed",
    "signal.entry_price * (1 + pct)"
    not in main_source,
)

check(
    "Engulfing removed from active strategy",
    "bullish_engulfing" not in strategy_source
    and "bearish_engulfing" not in strategy_source,
)

check(
    "Entry description uses configured EMA",
    "EMA{cfg.ENTRY_EMA}" in strategy_source,
)

check(
    "Alignment filter is fail closed",
    'signal.market_alignment not in '
    '("ALIGNED", "STRONG_ALIGNMENT")'
    in main_source,
)

check(
    "Dashboard exposes fixed stop-loss input",
    'name="sl_buffer_pct"' in dashboard_source
    and "Fixed stop-loss (%)" in dashboard_source,
)

print()
print("All dashboard-strategy consistency checks passed.")
