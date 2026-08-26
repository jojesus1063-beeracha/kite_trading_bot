"""Atomic PAPER state persistence for crash-safe session limits/positions."""
import json
import os
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from .position import OptionPosition


_DATETIME_FIELDS = ("entry_time", "time_of_mfe", "time_of_mae", "final_exit_time")


def _position_to_state(position: OptionPosition) -> dict:
    row = asdict(position)
    for field in _DATETIME_FIELDS:
        value = row[field]
        row[field] = value.isoformat() if value is not None else None
    return row


def _position_from_state(row: dict) -> OptionPosition:
    values = dict(row)
    for field in _DATETIME_FIELDS:
        value = values.get(field)
        values[field] = datetime.fromisoformat(value) if value else None
    return OptionPosition(**values)


def save_state(path: str, *, engine, seen_signal_ids) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "positions": [_position_to_state(value) for value in engine.positions.values()],
        "trades_by_date": {key.isoformat(): value for key, value in engine.trades_by_date.items()},
        "realized_pnl": engine.realized_pnl,
        "seen_signal_ids": sorted(seen_signal_ids),
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def restore_state(path: str, *, engine, signal_source) -> bool:
    target = Path(path)
    if not target.exists():
        return False
    try:
        payload = json.loads(target.read_text())
        if payload.get("version") != 1:
            raise ValueError("unsupported state version")
        positions = [_position_from_state(row) for row in payload.get("positions", [])]
        engine.positions = {position.position_id: position for position in positions}
        engine.closed_positions = [position for position in positions if not position.is_open]
        engine.trades_by_date = {
            date.fromisoformat(key): int(value)
            for key, value in payload.get("trades_by_date", {}).items()
        }
        engine.realized_pnl = float(payload.get("realized_pnl", 0.0))
        signal_source.restore_seen_ids(payload.get("seen_signal_ids", []))
        return True
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"F&O v1 state is corrupt; refusing to start: {exc}") from exc


def save_status(path: str, *, state: str, engine, socket_state: str, now: datetime) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": now.isoformat(), "mode": "PAPER", "state": state,
        "socket_state": socket_state,
        "open_positions": [position.to_record() for position in engine.open_positions],
        "trades_today": engine.trades_by_date.get(now.date(), 0),
        "available_capital": engine.available_capital,
        "realized_pnl": engine.realized_pnl,
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(temporary, target)
