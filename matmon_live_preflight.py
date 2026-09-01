#!/usr/bin/env python3
"""Read-only preflight for arming Matmon LIVE mode.

Reuses the strategy-agnostic checks already built and relied on for the
combined-strategy live launcher (config paper_trading=false, no unresolved
local state, broker flat of active MIS exposure) instead of duplicating
them. Skips live_combined_preflight's Momentum/RVOL selector-watchlist
validation, which is specific to that strategy's daily selector artifact and
does not apply to Matmon's per-tick scan.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from live_combined_preflight import (
    LIVE_ACK_ENV,
    LIVE_ACK_VALUE,
    load_json_object,
    require_live_acknowledgement,
    validate_broker_flat,
    validate_live_config,
    validate_local_flat,
)

STRATEGY_NAME = "MATMON_HAELOHIM"


def validate_matmon_config(data: dict) -> None:
    validate_live_config(data)  # paper_trading must be false


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    project = Path(__file__).resolve().parent
    parser.add_argument("--config", type=Path, default=project / "user_config.json")
    parser.add_argument("--project", type=Path, default=project)
    parser.add_argument("--check-broker-flat", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require_live_acknowledgement()
    config = load_json_object(args.config)
    validate_matmon_config(config)
    validate_local_flat(args.project)

    if args.check_broker_flat:
        from auth import get_kite_client
        validate_broker_flat(get_kite_client())

    print(f"PASS: {STRATEGY_NAME} live preflight")
    print("Broker flat check:", "PASS" if args.check_broker_flat else "SKIPPED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
