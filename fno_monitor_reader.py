import json
import subprocess
import threading
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")

ROOT = Path("/home/ubuntu/kite_fno_dynamic_exit_paper")
BOT = ROOT / "fno_bot"

AUDIT_DIR = BOT / "audit_logs"
POSITIONS = BOT / "fno_open_positions.json"
TRADES = BOT / "fno_trade_history.jsonl"

SERVICE = "kitebot-fno-stock-options-paper.service"

_lock = threading.Lock()

_state = {
    "date": None,
    "path": None,
    "offset": 0,
    "evaluations": 0,
    "signals": 0,
    "pressure_present": 0,
    "pressure_missing": 0,
    "reasons": Counter(),
    "latest": deque(maxlen=20),
}

_service_cache = {
    "at": 0.0,
    "active": False,
}


def _service_active():
    now = time.monotonic()

    if now - _service_cache["at"] < 5:
        return _service_cache["active"]

    try:
        p = subprocess.run(
            ["systemctl", "is-active", SERVICE],
            capture_output=True,
            text=True,
            timeout=2,
        )
        active = p.stdout.strip() == "active"
    except Exception:
        active = False

    _service_cache["at"] = now
    _service_cache["active"] = active
    return active


def _reset_for_day(date_str, path):
    _state["date"] = date_str
    _state["path"] = str(path)
    _state["offset"] = 0
    _state["evaluations"] = 0
    _state["signals"] = 0
    _state["pressure_present"] = 0
    _state["pressure_missing"] = 0
    _state["reasons"] = Counter()
    _state["latest"] = deque(maxlen=20)


def _consume_audit():
    date_str = datetime.now(IST).strftime("%Y-%m-%d")
    path = AUDIT_DIR / f"events_{date_str}.jsonl"

    with _lock:
        if (
            _state["date"] != date_str
            or _state["path"] != str(path)
        ):
            _reset_for_day(date_str, path)

        if not path.exists():
            return None

        size = path.stat().st_size

        # File replaced/truncated => rebuild from beginning.
        if size < _state["offset"]:
            _reset_for_day(date_str, path)

        with path.open("r", errors="ignore") as f:
            f.seek(_state["offset"])

            for line in f:
                try:
                    x = json.loads(line)
                except Exception:
                    continue

                if x.get("event") != "PROFESSIONAL_SIGNAL_EVALUATED":
                    continue

                _state["evaluations"] += 1

                direction = x.get("direction")
                if direction:
                    _state["signals"] += 1

                reason = str(x.get("reason") or "NO_REASON")
                _state["reasons"][reason] += 1

                m = x.get("metrics") or {}

                sp = m.get("selected_pressure")
                op = m.get("opposing_pressure")

                if sp is None and op is None:
                    _state["pressure_missing"] += 1
                else:
                    _state["pressure_present"] += 1

                _state["latest"].append({
                    "time": str(x.get("timestamp_ist", ""))[11:19],
                    "symbol": x.get("symbol", "-"),
                    "direction": direction or "-",
                    "reason": reason,
                    "underlying_roc": m.get("underlying_roc_pct"),
                    "ce_roc": m.get("ce_roc_pct"),
                    "pe_roc": m.get("pe_roc_pct"),
                    "volume_delta": m.get("selected_volume_delta"),
                    "oi": m.get("selected_oi"),
                    "selected_pressure": sp,
                    "opposing_pressure": op,
                })

            _state["offset"] = f.tell()

        return path


def _positions():
    if not POSITIONS.exists():
        return []

    try:
        x = json.loads(POSITIONS.read_text())
    except Exception:
        return []

    if isinstance(x, list):
        return x

    if isinstance(x, dict):
        if isinstance(x.get("positions"), list):
            return x["positions"]

        rows = []

        for key, value in x.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("underlying", key)
                rows.append(row)

        return rows

    return []


def _trades_today():
    today = datetime.now(IST).strftime("%Y-%m-%d")

    if not TRADES.exists():
        return []

    rows = []

    try:
        with TRADES.open(errors="ignore") as f:
            for line in f:
                try:
                    x = json.loads(line)
                except Exception:
                    continue

                stamp = str(
                    x.get("timestamp_ist")
                    or x.get("timestamp")
                    or x.get("exit_time")
                    or x.get("date")
                    or ""
                )

                if today in stamp:
                    rows.append(x)

    except Exception:
        pass

    return rows


def load_fno_monitor():
    audit_path = _consume_audit()

    with _lock:
        evaluations = _state["evaluations"]
        signals = _state["signals"]
        pressure_present = _state["pressure_present"]
        pressure_missing = _state["pressure_missing"]
        reasons = _state["reasons"].copy()
        latest = list(_state["latest"])

    audit_age = None

    if audit_path and audit_path.exists():
        mtime = datetime.fromtimestamp(
            audit_path.stat().st_mtime,
            tz=IST,
        )
        audit_age = max(
            0,
            int(
                (
                    datetime.now(IST) - mtime
                ).total_seconds()
            ),
        )

    trades = _trades_today()

    wins = 0
    losses = 0
    pnl_total = 0.0

    for t in trades:
        value = t.get("net_pnl")

        if value is None:
            value = t.get("pnl")

        if value is None:
            try:
                value = (
                    float(t.get("exit_price", 0))
                    - float(t.get("entry_price", 0))
                ) * float(t.get("quantity", 0))
            except Exception:
                value = 0

        try:
            value = float(value or 0)
        except Exception:
            value = 0

        pnl_total += value

        if value > 0:
            wins += 1
        elif value < 0:
            losses += 1

    return {
        "service_active": _service_active(),

        "mode": "PAPER",
        "strategy": "professional_momentum",
        "universe": "ALL_STOCK_OPTIONS",
        "capital": 5000.0,
        "max_trades": 20,

        "evaluations": evaluations,
        "signals": signals,

        "pressure_present": pressure_present,
        "pressure_missing": pressure_missing,

        "audit_age_seconds": audit_age,

        "rejections": [
            {
                "reason": reason,
                "count": count,
                "pct": (
                    count / evaluations * 100
                    if evaluations else 0
                ),
            }
            for reason, count
            in reasons.most_common(10)
        ],

        "latest": list(reversed(latest)),

        "positions": _positions(),
        "trades": trades[-20:],

        "wins": wins,
        "losses": losses,
        "pnl": pnl_total,
    }
