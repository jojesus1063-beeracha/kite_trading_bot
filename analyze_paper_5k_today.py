#!/usr/bin/env python3

import json
import csv
from pathlib import Path
from datetime import datetime
from collections import Counter, defaultdict

DATE = "2026-08-26"
SESSION_START = datetime.fromisoformat("2026-08-26T10:55:09")

ROOT = Path(".")
TRADE_FILES = [
    ROOT / "trade_history.jsonl",
    ROOT / "runtime" / "trade_history.jsonl",
]

AUDIT_FILE = ROOT / "runtime" / "paper_audit" / "entry_audit.jsonl"


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def first(d, *keys, default=None):
    for key in keys:
        if isinstance(d, dict) and key in d and d[key] is not None:
            return d[key]
    return default


def as_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_dt(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        return value

    s = str(value).strip()

    # ISO first
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        pass

    # Common formats
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%d-%m-%Y %H:%M:%S",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue

    return None


def record_dt(r):
    candidates = [
        "datetime",
        "timestamp",
        "time",
        "exit_time",
        "entry_time",
        "created_at",
        "date_time",
    ]

    for k in candidates:
        if k in r:
            dt = parse_dt(r.get(k))
            if dt:
                return dt

    # Sometimes date/time are split
    date = first(r, "date", "trade_date")
    tm = first(r, "time", "exit_time")

    if date and tm:
        dt = parse_dt(f"{date} {tm}")
        if dt:
            return dt

    return None


def read_jsonl(path):
    rows = []

    if not path.exists():
        return rows

    with path.open(errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            try:
                obj = json.loads(line)
            except Exception:
                continue

            if isinstance(obj, dict):
                obj["_source_file"] = str(path)
                obj["_line_no"] = line_no
                rows.append(obj)

    return rows


# ---------------------------------------------------------
# Find trade history
# ---------------------------------------------------------

trade_file = next((p for p in TRADE_FILES if p.exists()), None)

print("=" * 95)
print("₹5,000 PAPER SESSION ANALYSIS")
print("=" * 95)
print(f"Session start : {SESSION_START}")
print(f"Date          : {DATE}")
print()

if trade_file:
    print(f"Trade history : {trade_file}")
else:
    print("Trade history : NOT FOUND in normal locations")

print(f"Entry audit   : {AUDIT_FILE if AUDIT_FILE.exists() else 'NOT FOUND'}")
print()


# ---------------------------------------------------------
# TRADE ANALYSIS
# ---------------------------------------------------------

trades = read_jsonl(trade_file) if trade_file else []

session_trades = []

for r in trades:
    dt = record_dt(r)

    if not dt:
        continue

    if dt < SESSION_START:
        continue

    # Paper-only where mode field exists
    mode = str(first(r, "mode", "trading_mode", default="")).upper()

    if mode and "LIVE" in mode:
        continue

    session_trades.append(r)


def get_symbol(r):
    return str(first(
        r,
        "symbol",
        "tradingsymbol",
        "instrument",
        default="UNKNOWN"
    ))


def get_direction(r):
    return str(first(
        r,
        "direction",
        "side",
        "transaction_type",
        default=""
    )).upper()


def get_qty(r):
    return as_float(first(
        r,
        "qty",
        "quantity",
        "filled_quantity",
        default=0
    )) or 0


def get_entry(r):
    return as_float(first(
        r,
        "entry_price",
        "entry",
        "average_entry_price",
        default=None
    ))


def get_exit(r):
    return as_float(first(
        r,
        "exit_price",
        "exit",
        "average_exit_price",
        default=None
    ))


def get_net_pnl(r):
    # Prefer explicit net values
    v = as_float(first(
        r,
        "net_pnl",
        "net_profit",
        "pnl_net",
        "realized_net_pnl",
        default=None
    ))

    if v is not None:
        return v

    # Fall back to reported P&L
    v = as_float(first(
        r,
        "pnl",
        "profit",
        "realized_pnl",
        "profit_loss",
        default=None
    ))

    return v


def get_gross_pnl(r):
    return as_float(first(
        r,
        "gross_pnl",
        "gross_profit",
        "pnl_gross",
        default=None
    ))


def get_cost(r):
    return as_float(first(
        r,
        "charges",
        "cost",
        "transaction_cost",
        "brokerage_and_charges",
        default=None
    ))


def get_exit_reason(r):
    return str(first(
        r,
        "exit_reason",
        "reason",
        "result",
        "exit_type",
        default="UNKNOWN"
    ))


print("=" * 95)
print("1. CLOSED / RECORDED TRADE LEGS AFTER ₹5K RESET")
print("=" * 95)

if not session_trades:
    print("No trade-history records found after session start.")
else:
    net_values = []
    gross_values = []
    costs = []

    wins = 0
    losses = 0
    breakeven = 0

    symbol_pnl = defaultdict(float)
    symbol_count = Counter()
    exit_reasons = Counter()

    print(
        f"{'TIME':8} "
        f"{'SYMBOL':15} "
        f"{'DIR':5} "
        f"{'QTY':>7} "
        f"{'ENTRY':>10} "
        f"{'EXIT':>10} "
        f"{'NET PNL':>11} "
        f"{'EXIT REASON'}"
    )
    print("-" * 95)

    for r in sorted(session_trades, key=lambda x: record_dt(x) or SESSION_START):
        dt = record_dt(r)
        symbol = get_symbol(r)
        direction = get_direction(r)
        qty = get_qty(r)
        entry = get_entry(r)
        exit_px = get_exit(r)
        pnl = get_net_pnl(r)
        gross = get_gross_pnl(r)
        cost = get_cost(r)
        reason = get_exit_reason(r)

        if pnl is not None:
            net_values.append(pnl)
            symbol_pnl[symbol] += pnl

            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            else:
                breakeven += 1

        if gross is not None:
            gross_values.append(gross)

        if cost is not None:
            costs.append(cost)

        symbol_count[symbol] += 1
        exit_reasons[reason] += 1

        print(
            f"{dt.strftime('%H:%M:%S'):8} "
            f"{symbol[:15]:15} "
            f"{direction[:5]:5} "
            f"{qty:7.0f} "
            f"{entry if entry is not None else 0:10.2f} "
            f"{exit_px if exit_px is not None else 0:10.2f} "
            f"{pnl if pnl is not None else 0:11.2f} "
            f"{reason}"
        )

    total_net = sum(net_values)
    total_gross = sum(gross_values) if gross_values else None
    total_costs = sum(costs) if costs else None

    print()
    print("SUMMARY")
    print("-" * 95)
    print(f"Recorded exit legs       : {len(session_trades)}")
    print(f"Winning exit legs        : {wins}")
    print(f"Losing exit legs         : {losses}")
    print(f"Breakeven exit legs      : {breakeven}")

    resolved = wins + losses + breakeven

    if resolved:
        print(f"Exit-leg win rate        : {wins / resolved * 100:.2f}%")

    if total_gross is not None:
        print(f"Gross realized P&L       : ₹{total_gross:,.2f}")

    if total_costs is not None:
        print(f"Recorded charges/costs   : ₹{total_costs:,.2f}")

    print(f"Net/recorded P&L         : ₹{total_net:,.2f}")
    print(f"Return on ₹5,000         : {total_net / 5000 * 100:+.2f}%")
    print(f"₹5,000 + realized P&L    : ₹{5000 + total_net:,.2f}")

    print()
    print("P&L BY SYMBOL")
    print("-" * 60)

    for symbol, pnl in sorted(
        symbol_pnl.items(),
        key=lambda x: x[1],
        reverse=True
    ):
        print(
            f"{symbol:20} "
            f"legs={symbol_count[symbol]:3d} "
            f"net={pnl:+10.2f}"
        )

    print()
    print("EXIT REASONS")
    print("-" * 60)

    for reason, n in exit_reasons.most_common():
        print(f"{reason:40} {n:4d}")


# ---------------------------------------------------------
# POSITION SIZE / ₹5K REALISM
# ---------------------------------------------------------

print()
print("=" * 95)
print("2. POSITION-SIZING CHECK AGAINST ₹5,000")
print("=" * 95)

sizing_rows = []

for r in session_trades:
    entry = get_entry(r)
    qty = get_qty(r)

    if entry and qty:
        notional = entry * qty
        sizing_rows.append((
            get_symbol(r),
            qty,
            entry,
            notional,
            notional / 5000
        ))

if not sizing_rows:
    print("No entry-price/quantity combinations available.")
else:
    seen = set()

    for symbol, qty, entry, notional, multiple in sorted(
        sizing_rows,
        key=lambda x: x[3],
        reverse=True
    ):
        # Avoid repeating identical split exit legs
        key = (symbol, qty, entry)

        if key in seen:
            continue

        seen.add(key)

        flag = ""

        if notional > 5000:
            flag = "  <-- ABOVE CASH CAPITAL"

        print(
            f"{symbol:16} "
            f"qty={qty:7.0f} "
            f"entry=₹{entry:9.2f} "
            f"notional=₹{notional:11,.2f} "
            f"capital_multiple={multiple:5.2f}x"
            f"{flag}"
        )


# ---------------------------------------------------------
# ENTRY AUDIT / LOGIC PERFORMANCE
# ---------------------------------------------------------

print()
print("=" * 95)
print("3. ENTRY LOGIC / FILTER PERFORMANCE")
print("=" * 95)

audit = read_jsonl(AUDIT_FILE)

session_audit = []

for r in audit:
    dt = record_dt(r)

    if dt and dt >= SESSION_START:
        session_audit.append(r)

print(f"Audit records after reset: {len(session_audit):,}")

if not session_audit:
    print("No post-reset audit records found.")
else:
    decisions = Counter()
    blocks = Counter()
    directions = Counter()
    symbols_evaluated = Counter()
    final_directions = Counter()

    depth_pass = 0
    depth_fail = 0
    entries_detected = 0

    for r in session_audit:
        symbol = str(first(r, "symbol", default="UNKNOWN"))
        symbols_evaluated[symbol] += 1

        decision = first(
            r,
            "decision",
            "status",
            "outcome",
            "entry_decision",
            default=None
        )

        if decision:
            decisions[str(decision)] += 1

        raw_dir = first(
            r,
            "raw_direction",
            "raw",
            "direction",
            default=None
        )

        if raw_dir:
            directions[str(raw_dir)] += 1

        final_dir = first(
            r,
            "final_direction",
            "final",
            default=None
        )

        if final_dir:
            final_directions[str(final_dir)] += 1

        reason = first(
            r,
            "block_reason",
            "rejection_reason",
            "reason",
            default=None
        )

        if reason:
            if isinstance(reason, list):
                for x in reason:
                    blocks[str(x)] += 1
            else:
                blocks[str(reason)] += 1

        depth = first(
            r,
            "depth_confirmed",
            "depth_confirmation",
            "depth_pass",
            default=None
        )

        if depth is True:
            depth_pass += 1
        elif depth is False:
            depth_fail += 1

        entered = first(
            r,
            "entered",
            "entry_taken",
            "trade_taken",
            default=False
        )

        if entered is True:
            entries_detected += 1

    print()
    print("DECISIONS / OUTCOMES")
    print("-" * 60)

    if decisions:
        for k, v in decisions.most_common(20):
            print(f"{k:45} {v:6d}")
    else:
        print("No standard decision field detected.")

    print()
    print("RAW DIRECTIONS")
    print("-" * 60)

    for k, v in directions.most_common():
        print(f"{k:25} {v:6d}")

    print()
    print("FINAL DIRECTIONS")
    print("-" * 60)

    for k, v in final_directions.most_common():
        print(f"{k:25} {v:6d}")

    if depth_pass or depth_fail:
        print()
        print("DEPTH CONFIRMATION")
        print("-" * 60)
        print(f"Passed : {depth_pass}")
        print(f"Failed : {depth_fail}")

        total_depth = depth_pass + depth_fail

        if total_depth:
            print(f"Pass % : {depth_pass / total_depth * 100:.2f}%")

    print()
    print("MOST COMMON BLOCK / REASON FIELDS")
    print("-" * 95)

    if blocks:
        for reason, n in blocks.most_common(30):
            print(f"{reason[:75]:75} {n:6d}")
    else:
        print("No standard block/reason fields detected.")

    print()
    print("MOST EVALUATED SYMBOLS")
    print("-" * 60)

    for symbol, n in symbols_evaluated.most_common(20):
        print(f"{symbol:20} {n:6d}")


# ---------------------------------------------------------
# OPEN POSITIONS
# ---------------------------------------------------------

print()
print("=" * 95)
print("4. END-OF-SESSION / CURRENT OPEN POSITIONS")
print("=" * 95)

pos_file = ROOT / "open_positions.json"

if not pos_file.exists():
    print("No open_positions.json -> no currently persisted positions.")
else:
    try:
        data = json.loads(pos_file.read_text())
        positions = data.get("positions", {})

        if not positions:
            print("open_positions.json exists but contains zero positions.")
        else:
            print(f"Persisted open positions: {len(positions)}")

            for symbol, p in positions.items():
                direction = first(p, "direction", "side", default="")
                qty = as_float(first(p, "qty", "quantity", default=0)) or 0
                entry = as_float(first(p, "entry_price", "entry", default=None))

                notional = entry * qty if entry is not None else None

                print(
                    f"{symbol:18} "
                    f"{str(direction):5} "
                    f"qty={qty:7.0f} "
                    f"entry={entry if entry is not None else 0:9.2f} "
                    f"notional={notional if notional is not None else 0:11.2f}"
                )

    except Exception as e:
        print(f"Could not parse open_positions.json: {e}")


print()
print("=" * 95)
print("INTERPRETATION CHECKLIST")
print("=" * 95)
print("1. Compare total winning P&L versus total stopped-loss P&L.")
print("2. Check whether a small number of stops erased many winners.")
print("3. Check P&L by symbol for repeated-entry behaviour.")
print("4. Check depth-confirmation pass/fail frequency.")
print("5. Inspect the most frequent block reasons.")
print("6. Review position notionals above ₹5,000.")
print("7. Do not count scalp + runner legs as independent strategy entries.")
print("=" * 95)
