#!/usr/bin/env python3
"""Read-only/broker-flat preflight and atomic watchlist handoff for live mode."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LIVE_ACK_ENV = "KITE_LIVE_COMBINED_ACK"
LIVE_ACK_VALUE = "I_ACCEPT_REAL_ORDERS"

IST = ZoneInfo("Asia/Kolkata")
STRATEGY_NAME = "FULL_ZERODHA_CLEAN_TOP120_MOMENTUM"
EXPECTED_WATCHLIST_SIZE = 120
TERMINAL_ORDER_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED"}


def require_live_acknowledgement() -> None:
    if os.environ.get(LIVE_ACK_ENV) != LIVE_ACK_VALUE:
        raise RuntimeError(
            f"SAFETY BLOCK: {LIVE_ACK_ENV} must equal {LIVE_ACK_VALUE}"
        )


def load_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return data


def validate_live_config(data: dict) -> None:
    if data.get("paper_trading") is not False:
        raise RuntimeError("SAFETY BLOCK: user_config paper_trading must be false")


def validate_selector_artifacts(report: dict, payload: dict) -> list[dict]:
    if report.get("status") != "success":
        raise RuntimeError("Selector report is not successful")
    if report.get("strategy") != STRATEGY_NAME:
        raise RuntimeError("Unexpected selector strategy")
    if report.get("mode") != "READ_ONLY":
        raise RuntimeError("Live handoff accepts only a READ_ONLY selector run")
    generated = datetime.fromisoformat(report["generated_at"]).astimezone(IST)
    if generated.date() != datetime.now(IST).date():
        raise RuntimeError("Selector report was not generated today")

    selected = report.get("selected") or []
    watchlist = payload.get("watchlist") or []
    if (
        len(selected) != EXPECTED_WATCHLIST_SIZE
        or len(watchlist) != EXPECTED_WATCHLIST_SIZE
    ):
        raise RuntimeError(
            f"Expected exactly {EXPECTED_WATCHLIST_SIZE} symbols; "
            f"selected={len(selected)} output={len(watchlist)}"
        )
    if payload.get("status") != "success" or payload.get("strategy") != STRATEGY_NAME:
        raise RuntimeError("Selector payload contract mismatch")
    if any(row.get("ordinary_equity_clean") is not True for row in selected):
        raise RuntimeError("Selected row is missing ordinary-equity-clean evidence")

    normalized = [
        {
            "symbol": str(row.get("symbol") or "").strip().upper(),
            "exchange": str(row.get("exchange") or "").strip().upper(),
        }
        for row in watchlist
    ]
    symbols = [row["symbol"] for row in normalized]
    if any(not symbol for symbol in symbols):
        raise RuntimeError("Blank watchlist symbol")
    if len(symbols) != len(set(symbols)):
        raise RuntimeError("Duplicate symbol across NSE/BSE")
    if any(row["exchange"] not in {"NSE", "BSE"} for row in normalized):
        raise RuntimeError("Non-NSE/BSE watchlist row")
    return normalized


def validate_local_flat(project: Path) -> None:
    positions_path = project / "open_positions.json"
    if positions_path.exists():
        state = load_json_object(positions_path)
        if state.get("date") == datetime.now().strftime("%Y-%m-%d") and (
            state.get("positions") or {}
        ):
            raise RuntimeError("Local open_positions.json contains today's exposure")

    pending_path = project / "pending_orders.json"
    if pending_path.exists():
        pending = load_json_object(pending_path)
        unresolved = [row for row in pending.get("orders", []) if not row.get("resolved")]
        if unresolved:
            raise RuntimeError(
                f"Local pending_orders.json has {len(unresolved)} unresolved operation(s)"
            )

    stops_path = project / "protective_stops.json"
    if stops_path.exists():
        stops = load_json_object(stops_path)
        unresolved = [row for row in stops.get("stops", []) if not row.get("resolved")]
        if unresolved:
            raise RuntimeError(
                f"Local protective_stops.json has {len(unresolved)} unresolved stop(s)"
            )


def validate_broker_flat(kite) -> None:
    snapshots = kite.positions()
    positions = []
    if isinstance(snapshots, dict):
        positions.extend(snapshots.get("net") or [])
        positions.extend(snapshots.get("day") or [])
    active_mis = [
        row
        for row in positions
        if str(row.get("product") or "").upper() == "MIS"
        and int(row.get("quantity") or 0) != 0
    ]
    if active_mis:
        symbols = sorted({str(row.get("tradingsymbol") or "?") for row in active_mis})
        raise RuntimeError(f"Broker has active MIS exposure: {symbols}")

    orders = kite.orders()
    active_orders = [
        row
        for row in (orders or [])
        if str(row.get("product") or "").upper() == "MIS"
        and str(row.get("status") or "").upper() not in TERMINAL_ORDER_STATUSES
    ]
    if active_orders:
        symbols = sorted({str(row.get("tradingsymbol") or "?") for row in active_orders})
        raise RuntimeError(f"Broker has active MIS order(s): {symbols}")


def atomic_apply_watchlist(config_path: Path, watchlist: list[dict], backup_dir: Path) -> Path:
    data = load_json_object(config_path)
    validate_live_config(data)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"user_config-before-live-top120-{stamp}.json"
    shutil.copy2(config_path, backup)

    updated = dict(data)
    updated["watchlist"] = watchlist
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(updated, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, config_path)
    finally:
        if os.path.exists(temporary_name):
            os.remove(temporary_name)
    return backup


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    project = Path(__file__).resolve().parent
    runtime = project / "runtime" / "live_watchlist"
    parser.add_argument("--config", type=Path, default=project / "user_config.json")
    parser.add_argument("--report", type=Path, default=runtime / "latest_report.json")
    parser.add_argument("--payload", type=Path, default=runtime / "latest_watchlist.json")
    parser.add_argument("--project", type=Path, default=project)
    parser.add_argument("--apply-watchlist", action="store_true")
    parser.add_argument("--check-broker-flat", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require_live_acknowledgement()
    config = load_json_object(args.config)
    validate_live_config(config)
    validate_local_flat(args.project)
    report = load_json_object(args.report)
    payload = load_json_object(args.payload)
    watchlist = validate_selector_artifacts(report, payload)

    if args.check_broker_flat:
        from auth import get_kite_client
        validate_broker_flat(get_kite_client())

    backup = None
    if args.apply_watchlist:
        backup = atomic_apply_watchlist(
            args.config,
            watchlist,
            args.report.parent / "config_backups",
        )

    print("PASS: combined LIVE preflight")
    print("Watchlist count:", len(watchlist))
    print("Broker flat check:", "PASS" if args.check_broker_flat else "SKIPPED")
    print("Config update:", "APPLIED" if args.apply_watchlist else "READ_ONLY")
    if backup is not None:
        print("Configuration backup:", backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
