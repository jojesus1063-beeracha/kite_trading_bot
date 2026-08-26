"""Read-only adapter for accepted equity signals.

The equity bot remains the strategy owner. This adapter tails its append-only
daily signal file and emits each eligible underlying direction exactly once.
"""
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from .engine import UnderlyingSignal

IST = ZoneInfo("Asia/Kolkata")


def _signal_id(row: dict) -> str:
    stable = "|".join(str(row.get(key, "")) for key in (
        "timestamp", "symbol", "direction", "entry_price", "executed",
    ))
    return hashlib.sha256(stable.encode()).hexdigest()


def _parse_timestamp(value, fallback: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return fallback
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


class EquitySignalLogSource:
    def __init__(
        self, directory: str, *, require_executed: bool = True,
        audit_fn: Optional[Callable] = None,
    ):
        self.directory = Path(directory)
        self.require_executed = require_executed
        self.audit = audit_fn or (lambda _event, **_data: None)
        self._seen: set[str] = set()

    @property
    def seen_ids(self) -> set[str]:
        return set(self._seen)

    def restore_seen_ids(self, identifiers) -> None:
        self._seen.update(str(value) for value in identifiers)

    def poll(self, now: datetime) -> list[UnderlyingSignal]:
        path = self.directory / f"signals_{now.date().isoformat()}.jsonl"
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            self.audit("OPTION_SIGNAL_SOURCE_ERROR", reason=str(exc), path=str(path))
            return []

        signals = []
        for line in lines:
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            identifier = _signal_id(row)
            if identifier in self._seen:
                continue
            self._seen.add(identifier)
            if self.require_executed and row.get("executed") is not True:
                continue
            direction = str(row.get("direction", "")).upper()
            symbol = str(row.get("symbol", "")).upper()
            try:
                spot = float(row.get("entry_price"))
            except (TypeError, ValueError):
                spot = 0.0
            if not symbol or direction not in {"BUY", "SELL"} or spot <= 0:
                self.audit(
                    "OPTION_SIGNAL_SOURCE_REJECT", reason="invalid accepted equity signal",
                    symbol=symbol, direction=direction,
                )
                continue
            generated_at = _parse_timestamp(row.get("timestamp"), now)
            if generated_at.date() != now.date():
                continue
            signals.append(UnderlyingSignal(symbol, direction, spot, generated_at))
        return signals
