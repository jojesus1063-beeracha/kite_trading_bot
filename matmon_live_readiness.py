#!/usr/bin/env python3
"""Read-only Matmon production-candidate readiness preflight.

This script never enables live mode, never mutates user configuration, and
never submits/modifies/cancels an order. Optional broker checks are read-only.
"""
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path

import config as cfg
import matmon_live_candidate_launcher as candidate
import paper_matmon_launcher as paper_matmon

TERMINAL_ORDER_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED"}


def _check(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def validate_code_contract(project: Path) -> list[str]:
    errors: list[str] = []
    candidate_source = inspect.getsource(candidate)
    paper_source = inspect.getsource(paper_matmon)

    _check("paper_contrarian_launcher" not in paper_source, "paper launcher depends on legacy contrarian code", errors)
    _check("install_two_indicator_patch" not in paper_source, "paper launcher installs legacy strategy patch", errors)
    _check("evaluate_microstructure(direction, clean.ticks)" in paper_source, "paper launcher does not reuse CLEAN ticks for microstructure", errors)
    _check("not_before=di_passed_at" in paper_source, "paper launcher does not enforce post-DI tick freshness", errors)

    forbidden_candidate_tokens = (
        "place_order(",
        "modify_order(",
        "cancel_order(",
        "place_entry_order(",
        "combined_live_launcher",
        "LIVE_ACK_VALUE",
    )
    for token in forbidden_candidate_tokens:
        _check(token not in candidate_source, f"live-candidate exposes forbidden execution token: {token}", errors)

    _check((project / "matmon_entry_policy.py").exists(), "missing matmon_entry_policy.py", errors)
    _check((project / "matmon_quote_confirmation.py").exists(), "missing matmon_quote_confirmation.py", errors)
    _check((project / "matmon_microstructure.py").exists(), "missing matmon_microstructure.py", errors)
    return errors


def validate_runtime_contract() -> list[str]:
    errors: list[str] = []
    try:
        candidate.assert_dry_run_contract(cfg)
    except SystemExit as exc:
        errors.append(str(exc))

    _check(bool(getattr(cfg, "PAPER_TRADING", False)), "PAPER_TRADING is not True", errors)
    _check(str(getattr(cfg, "WS_CANDLE_MODE", "")).lower() == "shadow", "WS_CANDLE_MODE is not shadow", errors)
    _check(str(getattr(cfg, "ENTRY_TIMEFRAME", "")) == "3minute", "ENTRY_TIMEFRAME is not 3minute", errors)
    _check(bool(getattr(cfg, "CHECK_MARGIN_BEFORE_ENTRY", True)), "CHECK_MARGIN_BEFORE_ENTRY is disabled", errors)
    return errors


def validate_local_state(project: Path) -> list[str]:
    errors: list[str] = []
    for filename, collection in (
        ("pending_orders.json", "orders"),
        ("protective_stops.json", "stops"),
    ):
        path = project / filename
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"cannot read {filename}: {exc}")
            continue
        unresolved = [row for row in payload.get(collection, []) if not row.get("resolved")]
        if unresolved:
            errors.append(f"{filename} contains {len(unresolved)} unresolved record(s)")
    return errors


def validate_broker_flat() -> list[str]:
    """Read-only broker exposure check; does not place or alter orders."""
    errors: list[str] = []
    from auth import get_kite_client

    kite = get_kite_client()
    snapshots = kite.positions()
    positions = []
    if isinstance(snapshots, dict):
        positions.extend(snapshots.get("net") or [])
        positions.extend(snapshots.get("day") or [])
    active_mis = [
        row for row in positions
        if str(row.get("product") or "").upper() == "MIS"
        and int(row.get("quantity") or 0) != 0
    ]
    if active_mis:
        symbols = sorted({str(row.get("tradingsymbol") or "?") for row in active_mis})
        errors.append(f"broker has active MIS exposure: {symbols}")

    orders = kite.orders()
    active_orders = [
        row for row in (orders or [])
        if str(row.get("product") or "").upper() == "MIS"
        and str(row.get("status") or "").upper() not in TERMINAL_ORDER_STATUSES
    ]
    if active_orders:
        symbols = sorted({str(row.get("tradingsymbol") or "?") for row in active_orders})
        errors.append(f"broker has active MIS order(s): {symbols}")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-broker-flat", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project = Path(__file__).resolve().parent

    sections = {
        "code_contract": validate_code_contract(project),
        "runtime_contract": validate_runtime_contract(),
        "local_state": validate_local_state(project),
    }
    if args.check_broker_flat:
        sections["broker_flat"] = validate_broker_flat()

    all_errors = [error for errors in sections.values() for error in errors]
    for name, errors in sections.items():
        print(f"{name}: {'PASS' if not errors else 'BLOCKED'}")
        for error in errors:
            print(f"  - {error}")

    print("execution_boundary: DRY_RUN_ONLY")
    print("real_order_capability_exercised: False")
    if all_errors:
        print("MATMON_PRODUCTION_CANDIDATE_READINESS=BLOCKED")
        return 1
    print("MATMON_PRODUCTION_CANDIDATE_READINESS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
