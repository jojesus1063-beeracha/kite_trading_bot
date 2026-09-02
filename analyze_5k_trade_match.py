#!/usr/bin/env python3

import json
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict, Counter

START = datetime.fromisoformat("2026-08-26T10:55:09")

AUDIT_FILE = Path("runtime/paper_audit/entry_audit.jsonl")
TRADE_FILE = Path("trade_history.jsonl")


def load_jsonl(path):
    out = []
    if not path.exists():
        return out

    with path.open(errors="replace") as f:
        for line in f:
            try:
                x = json.loads(line)
            except Exception:
                continue
            if isinstance(x, dict):
                out.append(x)
    return out


def parse_dt(v):
    if not v:
        return None

    s = str(v).strip()

    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.replace(tzinfo=None)
    except Exception:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass

    return None


def first(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


def fnum(v):
    try:
        return float(v)
    except Exception:
        return None


def trade_dt(r):
    for k in (
        "entry_time",
        "datetime",
        "timestamp",
        "time",
        "created_at",
    ):
        d = parse_dt(r.get(k))
        if d:
            return d

    date = first(r, "date", "trade_date")
    tm = first(r, "entry_time", "time")

    if date and tm:
        return parse_dt(f"{date} {tm}")

    return None


def exit_dt(r):
    for k in (
        "exit_time",
        "datetime",
        "timestamp",
        "time",
        "created_at",
    ):
        d = parse_dt(r.get(k))
        if d:
            return d

    date = first(r, "date", "trade_date")
    tm = first(r, "exit_time", "time")

    if date and tm:
        return parse_dt(f"{date} {tm}")

    return None


def sym(r):
    return str(first(
        r,
        "symbol",
        "tradingsymbol",
        "instrument",
        default="UNKNOWN"
    ))


def direction(r):
    return str(first(
        r,
        "direction",
        "side",
        "transaction_type",
        default=""
    )).upper()


def qty(r):
    return fnum(first(
        r,
        "qty",
        "quantity",
        "filled_quantity",
        default=0
    )) or 0


def entry_px(r):
    return fnum(first(
        r,
        "entry_price",
        "entry",
        "average_entry_price",
        default=None
    ))


def exit_px(r):
    return fnum(first(
        r,
        "exit_price",
        "exit",
        "average_exit_price",
        default=None
    ))


def pnl(r):
    for k in (
        "net_pnl",
        "net_profit",
        "pnl_net",
        "realized_net_pnl",
        "pnl",
        "profit",
        "realized_pnl",
        "profit_loss",
    ):
        v = fnum(r.get(k))
        if v is not None:
            return v
    return None


def reason(r):
    return str(first(
        r,
        "exit_reason",
        "reason",
        "result",
        "exit_type",
        default="UNKNOWN"
    ))


# ------------------------------------------------------------
# Load audit
# ------------------------------------------------------------

audit_all = load_jsonl(AUDIT_FILE)

audit = []

for r in audit_all:
    dt = parse_dt(r.get("logged_at"))

    if not dt or dt < START:
        continue

    if r.get("paper_only") is not True:
        continue

    if str(r.get("execution_mode", "")).upper() != "PAPER":
        continue

    r["_dt"] = dt
    audit.append(r)


audit_by_symbol = defaultdict(list)

for r in audit:
    audit_by_symbol[sym(r)].append(r)

for s in audit_by_symbol:
    audit_by_symbol[s].sort(key=lambda x: x["_dt"])


# ------------------------------------------------------------
# Load trade legs
# ------------------------------------------------------------

raw_trades = load_jsonl(TRADE_FILE)

legs = []

for r in raw_trades:
    d = exit_dt(r)

    if not d or d < START:
        continue

    p = pnl(r)

    if p is None:
        continue

    x = dict(r)
    x["_exit_dt"] = d
    legs.append(x)


# ------------------------------------------------------------
# Group split legs into actual entries
#
# Same symbol + direction + entry price within a reasonable
# time cluster are treated as one trade.
# ------------------------------------------------------------

groups = []

for r in sorted(
    legs,
    key=lambda x: (
        sym(x),
        direction(x),
        entry_px(x) or 0,
        x["_exit_dt"]
    )
):
    s = sym(r)
    d = direction(r)
    ep = entry_px(r)

    matched = None

    for g in reversed(groups):
        if g["symbol"] != s:
            continue

        if g["direction"] != d:
            continue

        if ep is None or g["entry_price"] is None:
            continue

        if abs(ep - g["entry_price"]) > 1e-8:
            continue

        # Split legs should be close together.
        if abs((r["_exit_dt"] - g["last_exit"]).total_seconds()) <= 1800:
            matched = g
            break

    if matched is None:
        matched = {
            "symbol": s,
            "direction": d,
            "entry_price": ep,
            "legs": [],
            "first_exit": r["_exit_dt"],
            "last_exit": r["_exit_dt"],
        }
        groups.append(matched)

    matched["legs"].append(r)
    matched["first_exit"] = min(
        matched["first_exit"],
        r["_exit_dt"]
    )
    matched["last_exit"] = max(
        matched["last_exit"],
        r["_exit_dt"]
    )


# ------------------------------------------------------------
# Infer actual entry timing
#
# Prefer explicit entry timestamp if any leg has it.
# Otherwise use nearest audit before first exit, restricted
# to matching symbol/direction and entry price vicinity.
# ------------------------------------------------------------

def explicit_entry_time(group):
    times = []

    for r in group["legs"]:
        for k in ("entry_time", "entry_datetime"):
            d = parse_dt(r.get(k))
            if d:
                times.append(d)

    return min(times) if times else None


def find_matching_audit(group):
    rows = audit_by_symbol.get(group["symbol"], [])

    if not rows:
        return None

    target_dir = group["direction"]
    target_px = group["entry_price"]

    explicit = explicit_entry_time(group)

    cutoff = explicit if explicit else group["first_exit"]

    candidates = []

    for r in rows:
        if r["_dt"] > cutoff:
            break

        rd = str(first(
            r,
            "raw_direction",
            "ema_base_direction",
            default=""
        )).upper()

        if rd and target_dir and rd != target_dir:
            continue

        rp = fnum(r.get("entry_price"))

        price_gap_pct = None

        if (
            target_px is not None
            and rp is not None
            and target_px != 0
        ):
            price_gap_pct = abs(rp - target_px) / target_px * 100

        age_sec = (cutoff - r["_dt"]).total_seconds()

        if age_sec < 0:
            continue

        # Prefer recent observations with price close to actual entry.
        score = age_sec

        if price_gap_pct is not None:
            score += price_gap_pct * 300

        candidates.append((score, age_sec, price_gap_pct, r))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])

    return candidates[0]


# ------------------------------------------------------------
# Feature extraction
# ------------------------------------------------------------

def features(r):
    timing = r.get("entry_timing_observation") or {}
    tdetail = timing.get("detail") or {}

    breakout = r.get("breakout_diagnostics") or {}

    pa = r.get("price_action_observation") or {}
    pad = pa.get("detail") or {}

    observations = r.get("observations") or {}
    legacy = observations.get("legacy_entry_assessment") or {}

    volume_ratio = fnum(legacy.get("volume_ratio"))

    if volume_ratio is None:
        volume_ratio = fnum(breakout.get("volume_ratio"))

    ema9 = fnum(r.get("ema9"))
    ema21 = fnum(r.get("ema21"))
    entry = fnum(r.get("entry_price"))

    ema_gap_pct = None

    if ema9 is not None and ema21 is not None and entry:
        ema_gap_pct = abs(ema9 - ema21) / entry * 100

    return {
        "adx": fnum(r.get("adx")),
        "adx_regime": str(r.get("adx_regime")),
        "timing": str(timing.get("classification")),
        "timing_blockers": list(tdetail.get("blocking_reasons") or []),
        "extension_atr": fnum(tdetail.get("extension_atr")),
        "body_ratio": fnum(tdetail.get("body_to_range_ratio")),
        "volume_accel": fnum(tdetail.get("volume_acceleration")),

        "breakout": str(breakout.get("status")),
        "structure_break": bool(breakout.get("structure_break")),
        "breakout_volume_pass": bool(breakout.get("volume_pass")),
        "atr_pass": bool(breakout.get("atr_pass")),
        "clv_pass": bool(breakout.get("clv_pass")),
        "clv": fnum(breakout.get("clv_value")),
        "atr_multiple": fnum(breakout.get("atr_multiple")),

        "pa_score": fnum(pa.get("score")),
        "range_market": bool(pad.get("range_market")),
        "bos": bool(pad.get("bos")),
        "sr_blocked": bool(pad.get("sr_blocked")),

        "volume_ratio": volume_ratio,

        "ema9": ema9,
        "ema21": ema21,
        "ema_gap_pct": ema_gap_pct,
    }


# ------------------------------------------------------------
# Build matched trade table
# ------------------------------------------------------------

matched = []

for g in groups:
    total_pnl = sum(pnl(x) or 0 for x in g["legs"])

    m = find_matching_audit(g)

    row = {
        "symbol": g["symbol"],
        "direction": g["direction"],
        "entry_price": g["entry_price"],
        "qty_total": sum(qty(x) for x in g["legs"]),
        "legs": len(g["legs"]),
        "net_pnl": total_pnl,
        "result": (
            "WIN" if total_pnl > 0
            else "LOSS" if total_pnl < 0
            else "FLAT"
        ),
        "exit_reasons": ",".join(reason(x) for x in g["legs"]),
        "first_exit": g["first_exit"],
        "match": None,
    }

    if m:
        score, age_sec, gap_pct, ar = m

        row["match"] = {
            "audit_dt": ar["_dt"],
            "age_sec": age_sec,
            "price_gap_pct": gap_pct,
            "features": features(ar),
        }

    matched.append(row)


matched.sort(
    key=lambda x: (
        x["match"]["audit_dt"]
        if x["match"]
        else x["first_exit"]
    )
)


# ------------------------------------------------------------
# Output
# ------------------------------------------------------------

print("=" * 120)
print("₹5,000 ACTUAL TRADE ↔ PRE-ENTRY AUDIT MATCH")
print("=" * 120)
print(f"Post-reset audit rows : {len(audit)}")
print(f"Recorded exit legs    : {len(legs)}")
print(f"Grouped actual trades : {len(matched)}")
print()


header = (
    f"{'SYMBOL':14} "
    f"{'DIR':5} "
    f"{'PNL':>9} "
    f"{'RES':5} "
    f"{'ADX':>6} "
    f"{'TIME':>10} "
    f"{'BO':>5} "
    f"{'PA':>5} "
    f"{'VOL':>6} "
    f"{'RANGE':>6} "
    f"{'BOS':>4} "
    f"{'AGE':>6}"
)

print(header)
print("-" * 120)

for t in matched:

    if not t["match"]:
        print(
            f"{t['symbol']:14} "
            f"{t['direction']:5} "
            f"{t['net_pnl']:9.2f} "
            f"{t['result']:5} "
            f"{'NA':>6} "
            f"{'NO MATCH':>10}"
        )
        continue

    f = t["match"]["features"]

    print(
        f"{t['symbol']:14} "
        f"{t['direction']:5} "
        f"{t['net_pnl']:9.2f} "
        f"{t['result']:5} "
        f"{f['adx'] if f['adx'] is not None else 0:6.1f} "
        f"{f['timing'][:10]:>10} "
        f"{f['breakout'][:5]:>5} "
        f"{f['pa_score'] if f['pa_score'] is not None else 0:5.0f} "
        f"{f['volume_ratio'] if f['volume_ratio'] is not None else 0:6.2f} "
        f"{str(f['range_market']):>6} "
        f"{str(f['bos']):>4} "
        f"{t['match']['age_sec']:6.0f}"
    )


# ------------------------------------------------------------
# Winner vs loser averages
# ------------------------------------------------------------

print()
print("=" * 120)
print("WINNERS VS LOSERS")
print("=" * 120)


def avg(rows, feature):
    vals = []

    for t in rows:
        if not t["match"]:
            continue

        v = t["match"]["features"].get(feature)

        if isinstance(v, (int, float)):
            vals.append(float(v))

    return sum(vals) / len(vals) if vals else None


def count_true(rows, feature):
    vals = []

    for t in rows:
        if not t["match"]:
            continue

        vals.append(bool(
            t["match"]["features"].get(feature)
        ))

    return sum(vals), len(vals)


wins = [x for x in matched if x["net_pnl"] > 0 and x["match"]]
losses = [x for x in matched if x["net_pnl"] < 0 and x["match"]]

print(f"Winners matched : {len(wins)}")
print(f"Losers matched  : {len(losses)}")
print()

for feat, label in (
    ("adx", "ADX"),
    ("pa_score", "PA score"),
    ("volume_ratio", "Volume ratio"),
    ("extension_atr", "Entry extension ATR"),
    ("body_ratio", "Body/range ratio"),
    ("volume_accel", "Volume acceleration"),
    ("atr_multiple", "ATR multiple"),
    ("clv", "CLV"),
    ("ema_gap_pct", "EMA9-EMA21 gap %"),
):

    aw = avg(wins, feat)
    al = avg(losses, feat)

    print(
        f"{label:25} "
        f"WIN_AVG={aw if aw is not None else float('nan'):8.3f} "
        f"LOSS_AVG={al if al is not None else float('nan'):8.3f}"
    )


print()
print("BOOLEAN FEATURE COMPARISON")
print("-" * 90)

for feat, label in (
    ("structure_break", "Structure break"),
    ("breakout_volume_pass", "Breakout volume pass"),
    ("atr_pass", "ATR expansion pass"),
    ("clv_pass", "CLV pass"),
    ("range_market", "Range market"),
    ("bos", "BOS"),
    ("sr_blocked", "S/R blocked"),
):

    wt, wn = count_true(wins, feat)
    lt, ln = count_true(losses, feat)

    wp = 100 * wt / wn if wn else 0
    lp = 100 * lt / ln if ln else 0

    print(
        f"{label:25} "
        f"WIN={wt}/{wn} ({wp:5.1f}%) "
        f"LOSS={lt}/{ln} ({lp:5.1f}%)"
    )


# ------------------------------------------------------------
# Timing categories
# ------------------------------------------------------------

print()
print("TIMING CLASSIFICATION")
print("-" * 90)

for label, rows in (("WIN", wins), ("LOSS", losses)):
    c = Counter(
        x["match"]["features"]["timing"]
        for x in rows
    )

    print(label, dict(c))


# ------------------------------------------------------------
# Counterfactual filter P&L
# ------------------------------------------------------------

print()
print("=" * 120)
print("COUNTERFACTUAL: IF FILTER HAD BEEN REQUIRED")
print("=" * 120)

tests = {
    "ADX >= 20": lambda f: (
        f["adx"] is not None and f["adx"] >= 20
    ),

    "ADX >= 25": lambda f: (
        f["adx"] is not None and f["adx"] >= 25
    ),

    "Timing OPTIMAL/ACCEPTABLE": lambda f: (
        f["timing"] in ("OPTIMAL", "ACCEPTABLE")
    ),

    "Breakout PASS": lambda f: (
        f["breakout"] == "PASS"
    ),

    "PA > 0": lambda f: (
        f["pa_score"] is not None
        and f["pa_score"] > 0
    ),

    "Volume >= 1.2x": lambda f: (
        f["volume_ratio"] is not None
        and f["volume_ratio"] >= 1.2
    ),

    "Not range market": lambda f: (
        not f["range_market"]
    ),

    "BOS present": lambda f: (
        f["bos"]
    ),

    "ADX20 + PA>0": lambda f: (
        f["adx"] is not None
        and f["adx"] >= 20
        and f["pa_score"] is not None
        and f["pa_score"] > 0
    ),

    "ADX20 + Vol1.2": lambda f: (
        f["adx"] is not None
        and f["adx"] >= 20
        and f["volume_ratio"] is not None
        and f["volume_ratio"] >= 1.2
    ),
}


base = [x for x in matched if x["match"]]

base_pnl = sum(x["net_pnl"] for x in base)

print(
    f"{'FILTER':30} "
    f"{'TRADES':>7} "
    f"{'W':>3} "
    f"{'L':>3} "
    f"{'WIN%':>7} "
    f"{'NET PNL':>10} "
    f"{'DELTA':>10}"
)

print("-" * 120)

for name, fn in tests.items():

    kept = []

    for t in base:
        f = t["match"]["features"]

        if fn(f):
            kept.append(t)

    kpnl = sum(x["net_pnl"] for x in kept)
    kw = sum(x["net_pnl"] > 0 for x in kept)
    kl = sum(x["net_pnl"] < 0 for x in kept)

    wr = (
        100 * kw / (kw + kl)
        if kw + kl
        else 0
    )

    print(
        f"{name:30} "
        f"{len(kept):7} "
        f"{kw:3} "
        f"{kl:3} "
        f"{wr:7.2f} "
        f"{kpnl:10.2f} "
        f"{kpnl-base_pnl:+10.2f}"
    )


# ------------------------------------------------------------
# Detailed trade rows
# ------------------------------------------------------------

print()
print("=" * 120)
print("DETAILED MATCHES")
print("=" * 120)

for t in matched:

    print()
    print(
        f"{t['symbol']} {t['direction']} | "
        f"entry={t['entry_price']} | "
        f"legs={t['legs']} | "
        f"net={t['net_pnl']:+.2f} | "
        f"{t['result']} | "
        f"exit={t['exit_reasons']}"
    )

    if not t["match"]:
        print("  NO MATCHING AUDIT OBSERVATION")
        continue

    m = t["match"]
    f = m["features"]

    print(
        f"  matched audit : {m['audit_dt']} "
        f"| age={m['age_sec']:.0f}s "
        f"| price_gap={m['price_gap_pct']}"
    )

    print(
        f"  ADX={f['adx']} "
        f"| timing={f['timing']} "
        f"| breakout={f['breakout']} "
        f"| PA={f['pa_score']} "
        f"| vol={f['volume_ratio']}"
    )

    print(
        f"  range={f['range_market']} "
        f"| BOS={f['bos']} "
        f"| S/R blocked={f['sr_blocked']} "
        f"| EMA gap%={f['ema_gap_pct']}"
    )

    print(
        f"  timing blockers="
        f"{f['timing_blockers']}"
    )


print()
print("=" * 120)
print("IMPORTANT")
print("=" * 120)
print("This matches historical observations to actual trades.")
print("It does NOT prove causality from one trading day.")
print("Filters with better P&L should be replayed over multiple days")
print("before changing live or paper entry policy.")
print("=" * 120)
