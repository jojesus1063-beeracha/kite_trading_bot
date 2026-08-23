"""Read-only dashboard contract and append-only direction telemetry."""
from __future__ import annotations

import json
import os
import subprocess
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
PROJECT_DIR = Path(__file__).resolve().parent
SELECTOR_REPORT = PROJECT_DIR / "runtime" / "live_watchlist" / "latest_report.json"
PIPELINE_EVENTS = PROJECT_DIR / "runtime" / "live_combined_audit" / "pipeline_events.jsonl"


def record_pipeline_event(**event) -> None:
    """Append one small JSON event without influencing the trading decision."""
    payload = {
        "recorded_at": datetime.now(IST).isoformat(timespec="seconds"),
        **event,
    }
    try:
        PIPELINE_EVENTS.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(payload, separators=(",", ":"), default=str) + "\n").encode()
        descriptor = os.open(PIPELINE_EVENTS, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, line)
        finally:
            os.close(descriptor)
    except OSError:
        # Dashboard telemetry can never interrupt or alter trading.
        return


def _load_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _recent_jsonl(path: Path, limit: int = 20) -> list[dict]:
    try:
        with path.open(encoding="utf-8") as handle:
            lines = deque(handle, maxlen=max(limit * 4, limit))
    except OSError:
        return []
    rows = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _systemctl(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", *arguments],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return (result.stdout or "").strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _numeric_score_sort(item: tuple[str, int]) -> tuple[int, float, str]:
    """Sort numeric selector score labels descending without trusting report shape."""
    label = item[0]
    try:
        return (0, -float(label), label)
    except (TypeError, ValueError):
        # A malformed observational label must not take down the dashboard.
        return (1, 0.0, label)


def load_pipeline_dashboard() -> dict:
    selector = _load_json(SELECTOR_REPORT)
    selected = selector.get("selected", []) if selector else []
    scores = Counter(str(row.get("score")) for row in selected if row.get("score") is not None)
    generated_at = selector.get("generated_at") if selector else None
    selector_fresh = False
    if generated_at:
        try:
            selector_fresh = datetime.fromisoformat(generated_at).astimezone(IST).date() == datetime.now(IST).date()
        except (TypeError, ValueError):
            pass
    events = _recent_jsonl(PIPELINE_EVENTS, 20)
    return {
        "name": "Momentum/RVOL → EMA9/EMA21 → Market Policy",
        "selector": {
            "status": selector.get("status", "missing") if selector else "awaiting first run",
            "strategy": selector.get("strategy") if selector else "NSE_MOMENTUM_RVOL_TOP120",
            "generated_at": generated_at,
            "fresh_today": selector_fresh,
            "selected_count": len(selected),
            "eligible_count": selector.get("eligible_count") if selector else None,
            "score_counts": dict(sorted(scores.items(), key=_numeric_score_sort)),
            "top": selected[:10],
        },
        "strategy": {
            "raw_signal": "3-minute EMA9/EMA21 only",
            "ema3": "observational",
            "market_policy": "Bearish→BUY; Bullish→raw; Sideways→SELL; Unknown→skip",
            "legacy_filters": "observational only",
        },
        "limits": {
            "risk_per_trade_pct": 2.0,
            "max_trades_per_day": 10,
            "max_open_positions": 1,
            "max_daily_loss_pct": 0.5,
            "max_position_size_pct": 50.0,
            "force_square_off": "15:08 IST",
        },
        "services": {
            "live_bot": _systemctl("is-active", "kitebot-live-combined.service"),
            "watchlist_timer": _systemctl("is-active", "kite-live-watchlist.timer"),
            "stop_timer": _systemctl("is-active", "kitebot-stop.timer"),
            "next_watchlist": _systemctl(
                "show", "kite-live-watchlist.timer", "-p", "NextElapseUSecRealtime", "--value"
            ),
        },
        "recent_decisions": events,
    }
