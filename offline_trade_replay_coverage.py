#!/usr/bin/env python3
"""Offline replay coverage harvester for historical equity trades.

Read-only. Scans local runtime/signal/validation/replay data for candle-like
OHLCV records and maps them to trade_history.jsonl. It DOES NOT call Kite,
place orders, or modify live trading state.

Outputs:
  runtime/offline_replay_253/coverage.csv
  runtime/offline_replay_253/summary.json
  runtime/offline_replay_253/sources.json

The purpose is to establish which historical trades can be replayed exactly
from local data before any counterfactual P&L is claimed.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
TRADE_PATH = ROOT / "trade_history.jsonl"
OUT = ROOT / "runtime" / "offline_replay_253"

SCAN_DIRS = [
    ROOT / "runtime",
    ROOT / "signal_logs",
    ROOT / "validation_events",
]

SKIP_PARTS = {"venv", ".git", "node_modules", "__pycache__", "offline_replay_253"}
ALLOWED_SUFFIXES = {".json", ".jsonl", ".csv"}

OHLC_KEYS = {
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c", "ltp", "last_price"),
    "volume": ("volume", "vol", "v"),
}
TIME_KEYS = (
    "timestamp", "datetime", "date_time", "time", "candle_time",
    "candle_start", "candle_close", "signal_candle_start", "signal_candle_close",
)
SYMBOL_KEYS = ("symbol", "tradingsymbol", "trading_symbol", "ticker")


def safe_float(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def first_key(d: Dict[str, Any], keys: Iterable[str]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def normalize_symbol(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().upper()
    return s or None


def parse_date(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    m = re.search(r"(20\d{2}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def filename_date(p: Path) -> Optional[str]:
    return parse_date(p.name)


def looks_like_candle(d: Dict[str, Any]) -> bool:
    lk = {str(k).lower(): k for k in d.keys()}
    required = []
    for logical in ("open", "high", "low", "close"):
        found = None
        for alias in OHLC_KEYS[logical]:
            if alias in lk:
                found = lk[alias]
                break
        required.append(found)
    if any(x is None for x in required):
        return False
    vals = [safe_float(d[k]) for k in required]
    return all(v is not None for v in vals)


def extract_candle(d: Dict[str, Any], inherited_symbol: Optional[str], inherited_date: Optional[str]) -> Optional[Dict[str, Any]]:
    if not looks_like_candle(d):
        return None
    lk = {str(k).lower(): k for k in d.keys()}
    vals: Dict[str, Optional[float]] = {}
    for logical, aliases in OHLC_KEYS.items():
        actual = next((lk[a] for a in aliases if a in lk), None)
        vals[logical] = safe_float(d.get(actual)) if actual else None
    if None in (vals["open"], vals["high"], vals["low"], vals["close"]):
        return None
    sym = first_key(d, SYMBOL_KEYS)
    symbol = normalize_symbol(sym) or inherited_symbol
    ts = first_key(d, TIME_KEYS)
    date = parse_date(ts) or inherited_date
    return {
        "symbol": symbol,
        "date": date,
        "timestamp": None if ts is None else str(ts),
        **vals,
    }


def walk_json(obj: Any, inherited_symbol: Optional[str] = None, inherited_date: Optional[str] = None) -> Iterator[Dict[str, Any]]:
    if isinstance(obj, dict):
        symbol = normalize_symbol(first_key(obj, SYMBOL_KEYS)) or inherited_symbol
        date = parse_date(first_key(obj, TIME_KEYS)) or inherited_date
        c = extract_candle(obj, symbol, date)
        if c is not None:
            yield c
        for k, v in obj.items():
            child_symbol = symbol
            # Many replay JSONs are keyed by symbol.
            if isinstance(k, str) and re.fullmatch(r"[A-Z0-9&\-]{2,25}", k.upper()):
                if isinstance(v, (dict, list)):
                    child_symbol = k.upper()
            yield from walk_json(v, child_symbol, date)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_json(v, inherited_symbol, inherited_date)


def scan_json_file(p: Path) -> Iterator[Dict[str, Any]]:
    try:
        obj = json.loads(p.read_text(errors="ignore"))
    except Exception:
        return
    yield from walk_json(obj, inherited_date=filename_date(p))


def scan_jsonl_file(p: Path) -> Iterator[Dict[str, Any]]:
    inherited_date = filename_date(p)
    try:
        fh = p.open(errors="ignore")
    except Exception:
        return
    with fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            yield from walk_json(obj, inherited_date=inherited_date)


def scan_csv_file(p: Path) -> Iterator[Dict[str, Any]]:
    inherited_date = filename_date(p)
    try:
        fh = p.open(errors="ignore", newline="")
    except Exception:
        return
    with fh:
        try:
            reader = csv.DictReader(fh)
            for row in reader:
                if not isinstance(row, dict):
                    continue
                c = extract_candle(row, normalize_symbol(first_key(row, SYMBOL_KEYS)), inherited_date)
                if c is not None:
                    yield c
        except Exception:
            return


def candidate_files() -> List[Path]:
    out: List[Path] = []
    for base in SCAN_DIRS:
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in ALLOWED_SUFFIXES:
                continue
            if any(part in SKIP_PARTS for part in p.parts):
                continue
            # avoid scanning the authoritative trade log itself as candle data
            if p.resolve() == TRADE_PATH.resolve():
                continue
            out.append(p)
    return sorted(set(out))


def load_trades() -> List[Dict[str, Any]]:
    trades: List[Dict[str, Any]] = []
    for i, line in enumerate(TRADE_PATH.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            x = json.loads(line)
        except Exception:
            continue
        x["_line"] = i
        trades.append(x)
    return trades


def main() -> None:
    if not TRADE_PATH.exists():
        raise SystemExit(f"Missing {TRADE_PATH}")
    OUT.mkdir(parents=True, exist_ok=True)
    trades = load_trades()
    trade_keys = {(str(t.get("date")), normalize_symbol(t.get("symbol"))) for t in trades}
    wanted_symbols = {s for _, s in trade_keys if s}
    wanted_dates = {d for d, _ in trade_keys if d}

    coverage = defaultdict(lambda: {"count": 0, "sources": set(), "timestamps": set(), "has_volume": False})
    source_stats: Dict[str, Dict[str, Any]] = {}

    files = candidate_files()
    print(f"Trades: {len(trades)} | symbols: {len(wanted_symbols)} | dates: {len(wanted_dates)}")
    print(f"Scanning {len(files)} local data files ...")

    for idx, p in enumerate(files, start=1):
        suffix = p.suffix.lower()
        if suffix == ".json":
            it = scan_json_file(p)
        elif suffix == ".jsonl":
            it = scan_jsonl_file(p)
        elif suffix == ".csv":
            it = scan_csv_file(p)
        else:
            continue
        total = matched = 0
        pairs = set()
        for c in it:
            total += 1
            symbol = normalize_symbol(c.get("symbol"))
            date = c.get("date")
            if symbol not in wanted_symbols or date not in wanted_dates:
                continue
            key = (date, symbol)
            if key not in trade_keys:
                continue
            matched += 1
            pairs.add(key)
            rec = coverage[key]
            rec["count"] += 1
            rec["sources"].add(str(p.relative_to(ROOT)))
            if c.get("timestamp"):
                rec["timestamps"].add(str(c["timestamp"]))
            if c.get("volume") is not None:
                rec["has_volume"] = True
        if total or matched:
            source_stats[str(p.relative_to(ROOT))] = {
                "candle_like_records": total,
                "matched_records": matched,
                "matched_trade_symbol_dates": len(pairs),
            }
        if idx % 50 == 0:
            print(f"  scanned {idx}/{len(files)} files")

    rows = []
    status_counts = Counter()
    covered_trade_count = 0
    for t in trades:
        date = str(t.get("date"))
        symbol = normalize_symbol(t.get("symbol"))
        rec = coverage.get((date, symbol), None)
        n = 0 if rec is None else int(rec["count"])
        unique_ts = 0 if rec is None else len(rec["timestamps"])
        has_volume = False if rec is None else bool(rec["has_volume"])
        # 20 bars are required by the current breakout structure. 40+ unique
        # timestamps gives useful pre/post path headroom; 20-39 is partial.
        if unique_ts >= 40 and has_volume:
            status = "EXACT_CANDIDATE"
            covered_trade_count += 1
        elif unique_ts >= 20:
            status = "PARTIAL_CANDIDATE"
        elif n > 0:
            status = "SPARSE"
        else:
            status = "NO_LOCAL_CANDLES"
        status_counts[status] += 1
        rows.append({
            "trade_line": t.get("_line"),
            "date": date,
            "time": t.get("time"),
            "symbol": symbol,
            "direction": t.get("direction"),
            "qty": t.get("qty"),
            "entry": t.get("entry"),
            "exit": t.get("exit"),
            "actual_net_pnl": t.get("pnl"),
            "actual_result": t.get("result"),
            "candle_records": n,
            "unique_timestamps": unique_ts,
            "has_volume": has_volume,
            "source_count": 0 if rec is None else len(rec["sources"]),
            "sources": "|".join(sorted(rec["sources"])) if rec else "",
            "coverage_status": status,
        })

    with (OUT / "coverage.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "trade_count": len(trades),
        "unique_symbols": len(wanted_symbols),
        "unique_dates": len(wanted_dates),
        "files_scanned": len(files),
        "coverage_status": dict(status_counts),
        "exact_candidate_trades": covered_trade_count,
        "note": (
            "EXACT_CANDIDATE means local OHLCV appears sufficient for a proper replay, "
            "not that current-logic P&L has already been computed. This tool is coverage-only."
        ),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    (OUT / "sources.json").write_text(json.dumps(source_stats, indent=2))

    print("\n===== OFFLINE REPLAY COVERAGE =====")
    print(json.dumps(summary, indent=2))
    print(f"\nWrote: {OUT / 'coverage.csv'}")
    print(f"Wrote: {OUT / 'summary.json'}")
    print(f"Wrote: {OUT / 'sources.json'}")

    print("\n===== BEST LOCAL CANDLE SOURCES =====")
    best = sorted(source_stats.items(), key=lambda kv: (kv[1]["matched_trade_symbol_dates"], kv[1]["matched_records"]), reverse=True)
    for name, s in best[:25]:
        print(f"{s['matched_trade_symbol_dates']:4} pairs | {s['matched_records']:8} matched | {s['candle_like_records']:8} candle-like | {name}")


if __name__ == "__main__":
    main()
