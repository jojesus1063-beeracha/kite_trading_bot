#!/usr/bin/env python3

import json
from pathlib import Path
from datetime import datetime
from collections import Counter, defaultdict

START = datetime.fromisoformat("2026-08-26T10:55:09")
AUDIT = Path("runtime/paper_audit/entry_audit.jsonl")
TRADES = Path("trade_history.jsonl")


def parse_dt(v):
    if not v:
        return None
    try:
        # Convert timezone-aware value to naive local wall-clock
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d.replace(tzinfo=None)
    except Exception:
        return None


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


def pct(n, d):
    return (100.0 * n / d) if d else 0.0


audit_all = load_jsonl(AUDIT)

audit = []
for r in audit_all:
    dt = parse_dt(r.get("logged_at"))
    if (
        dt
        and dt >= START
        and r.get("paper_only") is True
        and str(r.get("execution_mode", "")).upper() == "PAPER"
    ):
        r["_dt"] = dt
        audit.append(r)


print("=" * 100)
print("₹5,000 PAPER LOGIC DEEP AUDIT")
print("=" * 100)
print("Start:", START)
print("Total audit file records:", len(audit_all))
print("Post-reset audit records:", len(audit))
print()


# ----------------------------------------------------------------
# 1. Raw signal activity
# ----------------------------------------------------------------

directions = Counter()
decisions = Counter()
adx_regimes = Counter()
symbols = Counter()

for r in audit:
    directions[str(r.get("raw_direction"))] += 1
    decisions[str(r.get("decision"))] += 1
    adx_regimes[str(r.get("adx_regime"))] += 1
    symbols[str(r.get("symbol"))] += 1


print("=" * 100)
print("1. RAW SIGNAL ACTIVITY")
print("=" * 100)

print("Directions:")
for k, v in directions.most_common():
    print(f"  {k:25} {v:6}  {pct(v,len(audit)):6.2f}%")

print("\nDecisions:")
for k, v in decisions.most_common():
    print(f"  {k:40} {v:6}")

print("\nADX regimes:")
for k, v in adx_regimes.most_common():
    print(f"  {k:30} {v:6}  {pct(v,len(audit)):6.2f}%")


# ----------------------------------------------------------------
# 2. Observational entry-timing filters
# ----------------------------------------------------------------

timing_class = Counter()
timing_blockers = Counter()

for r in audit:
    obs = r.get("entry_timing_observation") or {}
    cls = obs.get("classification")
    if cls:
        timing_class[str(cls)] += 1

    detail = obs.get("detail") or {}

    for reason in detail.get("blocking_reasons") or []:
        timing_blockers[str(reason)] += 1


print()
print("=" * 100)
print("2. ENTRY-TIMING OBSERVATION")
print("=" * 100)

for k, v in timing_class.most_common():
    print(f"{k:30} {v:6}  {pct(v,len(audit)):6.2f}%")

print("\nMost common timing warnings:")
for k, v in timing_blockers.most_common(20):
    print(f"{k:50} {v:6}")


# ----------------------------------------------------------------
# 3. Breakout diagnostics
# ----------------------------------------------------------------

breakout_status = Counter()
breakout_failed = Counter()

for r in audit:
    b = r.get("breakout_diagnostics") or {}

    status = b.get("status")
    if status:
        breakout_status[str(status)] += 1

    for reason in b.get("failed_components") or []:
        breakout_failed[str(reason)] += 1


print()
print("=" * 100)
print("3. BREAKOUT OBSERVATION")
print("=" * 100)

for k, v in breakout_status.most_common():
    print(f"{k:20} {v:6}  {pct(v,len(audit)):6.2f}%")

print("\nFailed breakout components:")
for k, v in breakout_failed.most_common():
    print(f"{k:40} {v:6}")


# ----------------------------------------------------------------
# 4. Price action
# ----------------------------------------------------------------

pa_scores = []
range_market = Counter()
bos = Counter()

for r in audit:
    p = r.get("price_action_observation") or {}

    score = p.get("score")
    if isinstance(score, (int, float)):
        pa_scores.append(float(score))

    detail = p.get("detail") or {}

    range_market[str(detail.get("range_market"))] += 1
    bos[str(detail.get("bos"))] += 1


print()
print("=" * 100)
print("4. PRICE ACTION OBSERVATION")
print("=" * 100)

if pa_scores:
    print(f"Average PA score : {sum(pa_scores)/len(pa_scores):.2f}")
    print(f"Positive score   : {sum(x > 0 for x in pa_scores)}")
    print(f"Zero score       : {sum(x == 0 for x in pa_scores)}")
    print(f"Negative score   : {sum(x < 0 for x in pa_scores)}")

print("\nRange-market observations:")
for k, v in range_market.most_common():
    print(f"  {k:10} {v:6}")

print("\nBOS observations:")
for k, v in bos.most_common():
    print(f"  {k:10} {v:6}")


# ----------------------------------------------------------------
# 5. Legacy / observational volume
# ----------------------------------------------------------------

volume_pass_12 = 0
volume_total = 0

for r in audit:
    obs = r.get("observations") or {}
    legacy = obs.get("legacy_entry_assessment") or {}

    v = legacy.get("would_volume_pass_1_2x")

    if v is not None:
        volume_total += 1
        if str(v).lower() == "true":
            volume_pass_12 += 1


print()
print("=" * 100)
print("5. OBSERVATIONAL VOLUME FILTER")
print("=" * 100)

print(f"Would pass 1.2x volume : {volume_pass_12}")
print(f"Would fail 1.2x volume : {volume_total-volume_pass_12}")

if volume_total:
    print(f"Pass rate              : {pct(volume_pass_12,volume_total):.2f}%")


# ----------------------------------------------------------------
# 6. ADX hypotheticals
# ----------------------------------------------------------------

adx20 = 0
adx25 = 0
adx_total = 0

for r in audit:
    adx = r.get("adx")

    try:
        adx = float(adx)
    except Exception:
        continue

    adx_total += 1

    if adx >= 20:
        adx20 += 1

    if adx >= 25:
        adx25 += 1


print()
print("=" * 100)
print("6. ADX DISTRIBUTION")
print("=" * 100)

print(f"ADX >= 20 : {adx20:6} / {adx_total} ({pct(adx20,adx_total):.2f}%)")
print(f"ADX >= 25 : {adx25:6} / {adx_total} ({pct(adx25,adx_total):.2f}%)")
print(f"ADX < 20  : {adx_total-adx20:6} / {adx_total} ({pct(adx_total-adx20,adx_total):.2f}%)")


# ----------------------------------------------------------------
# 7. Actual traded symbols vs their nearest audit observations
# ----------------------------------------------------------------

trades = load_jsonl(TRADES)

post_trades = []

for t in trades:
    candidate_times = [
        t.get("datetime"),
        t.get("timestamp"),
        t.get("exit_time"),
        t.get("time"),
    ]

    dt = None
    for v in candidate_times:
        dt = parse_dt(v)
        if dt:
            break

    # Trade log formats can sometimes store date + time separately.
    if not dt and t.get("date") and t.get("time"):
        try:
            dt = datetime.fromisoformat(f"{t['date']}T{t['time']}")
        except Exception:
            pass

    if dt and dt >= START:
        t["_dt"] = dt
        post_trades.append(t)


trade_symbols = sorted({
    str(t.get("symbol") or t.get("tradingsymbol"))
    for t in post_trades
    if t.get("symbol") or t.get("tradingsymbol")
})


print()
print("=" * 100)
print("7. TRADED SYMBOL OBSERVATIONAL PROFILE")
print("=" * 100)

print("Traded symbols found:", ", ".join(trade_symbols))


for sym in trade_symbols:
    rows = [r for r in audit if str(r.get("symbol")) == sym]

    if not rows:
        print(f"\n{sym}: NO POST-RESET AUDIT ROW")
        continue

    timing_valid = 0
    breakout_pass = 0
    adx20_pass = 0
    volume12_pass = 0
    positive_pa = 0

    for r in rows:

        timing = r.get("entry_timing_observation") or {}
        if str(timing.get("classification")).upper() == "VALID":
            timing_valid += 1

        b = r.get("breakout_diagnostics") or {}
        if str(b.get("status")).upper() == "PASS":
            breakout_pass += 1

        try:
            if float(r.get("adx")) >= 20:
                adx20_pass += 1
        except Exception:
            pass

        legacy = (
            (r.get("observations") or {})
            .get("legacy_entry_assessment") or {}
        )

        if str(legacy.get("would_volume_pass_1_2x")).lower() == "true":
            volume12_pass += 1

        p = r.get("price_action_observation") or {}

        try:
            if float(p.get("score")) > 0:
                positive_pa += 1
        except Exception:
            pass

    n = len(rows)

    print(f"\n{sym}")
    print(f"  audit observations   : {n}")
    print(f"  timing VALID         : {timing_valid}/{n}")
    print(f"  breakout PASS        : {breakout_pass}/{n}")
    print(f"  ADX >=20             : {adx20_pass}/{n}")
    print(f"  volume >=1.2x        : {volume12_pass}/{n}")
    print(f"  PA score >0          : {positive_pa}/{n}")


# ----------------------------------------------------------------
# 8. Interesting counterfactual counts
# ----------------------------------------------------------------

tests = {
    "ADX >=20": lambda r: (
        isinstance(r.get("adx"), (int, float))
        and float(r["adx"]) >= 20
    ),

    "ADX >=25": lambda r: (
        isinstance(r.get("adx"), (int, float))
        and float(r["adx"]) >= 25
    ),

    "Entry timing VALID": lambda r: (
        str(
            (r.get("entry_timing_observation") or {})
            .get("classification")
        ).upper() == "VALID"
    ),

    "Breakout PASS": lambda r: (
        str(
            (r.get("breakout_diagnostics") or {})
            .get("status")
        ).upper() == "PASS"
    ),

    "PA score >0": lambda r: (
        isinstance(
            (r.get("price_action_observation") or {}).get("score"),
            (int, float)
        )
        and (r.get("price_action_observation") or {}).get("score") > 0
    ),

    "Volume >=1.2x": lambda r: (
        str(
            (
                (r.get("observations") or {})
                .get("legacy_entry_assessment") or {}
            ).get("would_volume_pass_1_2x")
        ).lower() == "true"
    ),
}


print()
print("=" * 100)
print("8. IF EACH OBSERVATIONAL FILTER HAD BEEN A HARD GATE")
print("=" * 100)

for name, fn in tests.items():

    passed = sum(1 for r in audit if fn(r))

    print(
        f"{name:25} "
        f"PASS={passed:5} "
        f"BLOCK={len(audit)-passed:5} "
        f"PASS_RATE={pct(passed,len(audit)):6.2f}%"
    )


print()
print("=" * 100)
print("IMPORTANT")
print("=" * 100)
print("These are signal/evaluation counts, NOT hypothetical P&L.")
print("A filter looking selective does not prove it improves profitability.")
print("Next step is to match each actual entry to its nearest pre-entry")
print("audit observation and compare winners versus losers.")
print("=" * 100)
