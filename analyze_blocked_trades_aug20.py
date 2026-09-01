#!/usr/bin/env python3

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from kiteconnect import KiteConnect

import config

IST = ZoneInfo("Asia/Kolkata")

DATE = "2026-08-20"

AUDIT = Path(
    "runtime/live_combined_audit/entry_audit.jsonl"
)

TOKEN_FILE = Path("access_token.txt")

STOP_PCT = 0.45 / 100.0

# Hybrid clean-candle structure:
# first partial at 1R, runner at 2R.
ONE_R = 1.0
TWO_R = 2.0

MARKET_END = time(15, 8)

# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------

def parse_dt(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        else:
            dt = dt.astimezone(IST)

        return dt

    except Exception:
        return None


def record_dt(r):
    for key in (
        "logged_at",
        "timestamp",
        "evaluated_at",
        "candle_time",
        "time",
        "datetime",
    ):
        dt = parse_dt(r.get(key))

        if dt:
            return dt

    return None


def extract_direction(r):
    direction = str(
        r.get("direction") or ""
    ).upper()

    if direction in {"BUY", "SELL"}:
        return direction

    detail = r.get("detail") or {}

    direction = str(
        detail.get("direction") or ""
    ).upper()

    return direction


def extract_entry(r):
    candidates = [
        r.get("entry"),
        r.get("entry_price"),
        r.get("close"),
    ]

    detail = r.get("detail") or {}

    candidates.extend([
        detail.get("entry"),
        detail.get("close"),
    ])

    candle = r.get("completed_3m_candle") or {}

    candidates.append(candle.get("close"))

    for value in candidates:
        try:
            value = float(value)

            if value > 0:
                return value

        except Exception:
            pass

    return None


def extract_reasons(r):
    reasons = []

    for reason in r.get("reasons") or []:
        if reason not in reasons:
            reasons.append(reason)

    detail = r.get("detail") or {}

    for reason in detail.get("reasons") or []:
        if reason not in reasons:
            reasons.append(reason)

    return reasons


def breakout_info(r):
    candidates = [
        r.get("breakout_validation"),
        (r.get("detail") or {}).get(
            "breakout_validation"
        ),
        (
            (r.get("candle_eligibility") or {})
            .get("detail", {})
            .get("breakout_validation")
        ),
    ]

    for value in candidates:
        if isinstance(value, dict):
            return value

    return {}


def confirmation_info(r):
    detail = r.get("detail") or {}

    ce = (
        (r.get("candle_eligibility") or {})
        .get("detail", {})
    )

    return (
        ce.get("confirmations")
        or detail.get("confirmations")
        or r.get("confirmations")
        or {}
    )


# ------------------------------------------------------------
# LOAD AUDIT
# ------------------------------------------------------------

rows = []

malformed = 0

with AUDIT.open() as handle:

    for line in handle:

        if not line.strip():
            continue

        try:
            r = json.loads(line)

        except Exception:
            malformed += 1
            continue

        dt = record_dt(r)

        if not dt:
            continue

        if dt.date().isoformat() != DATE:
            continue

        rows.append(r)


print("=" * 100)
print("AUG 20 FULL AUDIT")
print("=" * 100)

print("Audit records :", len(rows))
print("Malformed     :", malformed)


# ------------------------------------------------------------
# NORMALIZE EVALUATIONS
# ------------------------------------------------------------

evaluations = []

for r in rows:

    symbol = str(
        r.get("symbol")
        or r.get("tradingsymbol")
        or ""
    ).upper()

    direction = extract_direction(r)

    if not symbol:
        continue

    reasons = extract_reasons(r)

    decision = str(
        r.get("decision") or ""
    ).upper()

    stage = str(
        r.get("stage") or ""
    ).upper()

    accepted = (
        r.get("accepted") is True
        or decision in {
            "SIGNAL_SELECTED",
            "ACCEPT",
            "ACCEPTED",
        }
    )

    evaluations.append({
        "raw": r,
        "dt": record_dt(r),
        "symbol": symbol,
        "direction": direction,
        "entry": extract_entry(r),
        "reasons": reasons,
        "stage": stage,
        "decision": decision,
        "accepted": accepted,
        "breakout": breakout_info(r),
        "confirmations": confirmation_info(r),
    })


print("Normalized evaluations :", len(evaluations))
print(
    "Unique symbols         :",
    len(set(x["symbol"] for x in evaluations))
)


# ------------------------------------------------------------
# FILTER COUNTS
# ------------------------------------------------------------

reason_counts = Counter()

sole_counts = Counter()

stage_counts = Counter()

for e in evaluations:

    stage_counts[e["stage"]] += 1

    for reason in e["reasons"]:
        reason_counts[reason] += 1

    if len(e["reasons"]) == 1:
        sole_counts[e["reasons"][0]] += 1


print()
print("=" * 100)
print("TOP-LEVEL FILTER IMPACT")
print("=" * 100)

for reason, count in reason_counts.most_common():

    pct = (
        count / len(evaluations) * 100
        if evaluations
        else 0
    )

    print(
        f"{reason:50s}"
        f"{count:7d}"
        f"  {pct:7.2f}%"
    )


print()
print("=" * 100)
print("TRUE SOLE BLOCKERS")
print("=" * 100)

for reason, count in sole_counts.most_common():
    print(f"{reason:50s}{count:7d}")


# ------------------------------------------------------------
# BREAKOUT SUB-GATE ANALYSIS
# ------------------------------------------------------------

sub_counts = Counter()

breakout_pass = 0
breakout_fail = 0

near_breakouts = []

for e in evaluations:

    b = e["breakout"]

    if not b:
        continue

    metrics = b.get("metrics") or {}

    reasons = b.get("reasons") or []

    if b.get("passed") is True:
        breakout_pass += 1

    else:
        breakout_fail += 1

    for reason in reasons:
        sub_counts[reason] += 1

    # Particularly interesting:
    # breakout failed exactly ONE subrule.
    if len(reasons) == 1:

        near_breakouts.append({
            "dt": e["dt"],
            "symbol": e["symbol"],
            "direction": e["direction"],
            "entry": e["entry"],
            "failed": reasons[0],
            "volume_ratio":
                metrics.get("volume_ratio"),
            "atr_multiplier":
                metrics.get("atr_multiplier"),
            "clv":
                metrics.get("clv"),
            "n_high":
                metrics.get("n_period_high"),
            "n_low":
                metrics.get("n_period_low"),
        })


print()
print("=" * 100)
print("BREAKOUT SUB-RULES")
print("=" * 100)

print("Breakout pass:", breakout_pass)
print("Breakout fail:", breakout_fail)

for reason, count in sub_counts.most_common():
    print(f"{reason:45s}{count:7d}")


print()
print("Single-subrule breakout failures:", len(near_breakouts))

for n in near_breakouts[:50]:

    print(
        n["dt"],
        n["symbol"],
        n["direction"],
        "failed=",
        n["failed"],
        "vol=",
        n["volume_ratio"],
        "atr=",
        n["atr_multiplier"],
        "clv=",
        n["clv"],
    )


# ------------------------------------------------------------
# CONNECT TO KITE
# ------------------------------------------------------------

api_key = None

for name in (
    "API_KEY",
    "KITE_API_KEY",
):
    value = getattr(config, name, None)

    if value:
        api_key = value
        break


if not api_key:
    raise SystemExit(
        "API key not found"
    )


kite = KiteConnect(
    api_key=api_key
)

kite.set_access_token(
    TOKEN_FILE.read_text().strip()
)


# ------------------------------------------------------------
# INSTRUMENT TOKEN MAP
# ------------------------------------------------------------

print()
print("Loading NSE instruments...")

instruments = kite.instruments("NSE")

token_map = {
    str(x["tradingsymbol"]).upper():
        int(x["instrument_token"])
    for x in instruments
}


# ------------------------------------------------------------
# GET TODAY'S 3-MINUTE CANDLES
# ------------------------------------------------------------

symbols_needed = sorted({
    e["symbol"]
    for e in evaluations
    if (
        not e["accepted"]
        and e["entry"]
        and e["direction"]
        in {"BUY", "SELL"}
    )
})

candles_by_symbol = {}


for idx, symbol in enumerate(
    symbols_needed,
    1,
):

    token = token_map.get(symbol)

    if not token:
        continue

    print(
        f"Fetching {idx}/{len(symbols_needed)} "
        f"{symbol}"
    )

    start = datetime(
        2026, 8, 20,
        9, 15,
        tzinfo=IST,
    )

    end = datetime(
        2026, 8, 20,
        15, 30,
        tzinfo=IST,
    )

    try:
        candles = kite.historical_data(
            token,
            start,
            end,
            "3minute",
            continuous=False,
            oi=False,
        )

    except Exception as exc:
        print(
            "  FAILED:",
            type(exc).__name__,
            exc,
        )
        continue

    normalized = []

    for c in candles:

        dt = parse_dt(c["date"])

        normalized.append({
            "dt": dt,
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
        })

    candles_by_symbol[symbol] = normalized


# ------------------------------------------------------------
# COUNTERFACTUAL SIMULATION
# ------------------------------------------------------------

def simulate(e):

    symbol = e["symbol"]
    direction = e["direction"]
    entry = e["entry"]
    signal_dt = e["dt"]

    candles = candles_by_symbol.get(
        symbol,
        []
    )

    future = [
        c for c in candles
        if (
            c["dt"] > signal_dt
            and c["dt"].time()
            <= MARKET_END
        )
    ]

    if not future:
        return None

    risk_per_share = (
        entry * STOP_PCT
    )

    if direction == "BUY":

        stop = (
            entry - risk_per_share
        )

        one_r = (
            entry
            + risk_per_share * ONE_R
        )

        two_r = (
            entry
            + risk_per_share * TWO_R
        )

    else:

        stop = (
            entry + risk_per_share
        )

        one_r = (
            entry
            - risk_per_share * ONE_R
        )

        two_r = (
            entry
            - risk_per_share * TWO_R
        )


    one_hit = False
    two_hit = False
    stop_hit = False

    one_time = None
    two_time = None
    stop_time = None

    max_favourable = 0.0
    max_adverse = 0.0


    for c in future:

        if direction == "BUY":

            favourable = (
                c["high"] - entry
            )

            adverse = (
                entry - c["low"]
            )

            hit_stop = (
                c["low"] <= stop
            )

            hit_one = (
                c["high"] >= one_r
            )

            hit_two = (
                c["high"] >= two_r
            )

        else:

            favourable = (
                entry - c["low"]
            )

            adverse = (
                c["high"] - entry
            )

            hit_stop = (
                c["high"] >= stop
            )

            hit_one = (
                c["low"] <= one_r
            )

            hit_two = (
                c["low"] <= two_r
            )


        max_favourable = max(
            max_favourable,
            favourable,
        )

        max_adverse = max(
            max_adverse,
            adverse,
        )


        # Conservative same-candle handling:
        # if stop and target both appear
        # inside one 3m candle, count STOP first.
        if hit_stop:

            stop_hit = True
            stop_time = c["dt"]
            break


        if (
            hit_one
            and not one_hit
        ):

            one_hit = True
            one_time = c["dt"]


        if hit_two:

            two_hit = True
            two_time = c["dt"]
            break


    mfe_r = (
        max_favourable
        / risk_per_share
        if risk_per_share > 0
        else 0
    )

    mae_r = (
        max_adverse
        / risk_per_share
        if risk_per_share > 0
        else 0
    )


    if two_hit:

        outcome = "2R_WIN"

    elif one_hit:

        outcome = "1R_REACHED"

    elif stop_hit:

        outcome = "STOP_LOSS"

    elif mfe_r > 0:

        outcome = "MOVED_FAVOURABLY"

    else:

        outcome = "NO_FAVOURABLE_MOVE"


    return {
        "stop": stop,
        "one_r": one_r,
        "two_r": two_r,
        "outcome": outcome,
        "one_hit": one_hit,
        "two_hit": two_hit,
        "stop_hit": stop_hit,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "one_time": one_time,
        "two_time": two_time,
        "stop_time": stop_time,
    }


# Deduplicate repeated audit records for
# the same symbol/direction/candle timestamp.
dedup = {}

for e in evaluations:

    if (
        e["accepted"]
        or e["entry"] is None
        or e["direction"]
        not in {"BUY", "SELL"}
    ):
        continue

    key = (
        e["symbol"],
        e["direction"],
        e["dt"].replace(
            second=0,
            microsecond=0,
        ),
    )

    if key not in dedup:
        dedup[key] = e


counterfactuals = []

for e in dedup.values():

    result = simulate(e)

    if result:
        counterfactuals.append({
            **e,
            **result,
        })


# ------------------------------------------------------------
# PROFITABLE BLOCKS
# ------------------------------------------------------------

outcomes = Counter(
    x["outcome"]
    for x in counterfactuals
)

print()
print("=" * 100)
print("COUNTERFACTUAL RESULTS")
print("=" * 100)

print(
    "Unique rejected setups replayed:",
    len(counterfactuals),
)

for outcome, count in outcomes.most_common():
    print(f"{outcome:25s}{count:7d}")


profitable = [
    x for x in counterfactuals
    if x["two_hit"]
]

one_r_only = [
    x for x in counterfactuals
    if (
        x["one_hit"]
        and not x["two_hit"]
    )
]


print()
print("=" * 100)
print(
    "BLOCKED SETUPS THAT REACHED 2R "
    "BEFORE STOP"
)
print("=" * 100)

print("Count:", len(profitable))

for x in sorted(
    profitable,
    key=lambda z: z["mfe_r"],
    reverse=True,
):

    print(
        x["dt"],
        f'{x["symbol"]:15s}',
        x["direction"],
        f'entry={x["entry"]:.2f}',
        f'MFE={x["mfe_r"]:.2f}R',
        "reasons=",
        x["reasons"],
    )


print()
print("=" * 100)
print(
    "BLOCKED SETUPS THAT REACHED 1R "
    "BUT NOT 2R"
)
print("=" * 100)

print("Count:", len(one_r_only))

for x in sorted(
    one_r_only,
    key=lambda z: z["mfe_r"],
    reverse=True,
)[:100]:

    print(
        x["dt"],
        f'{x["symbol"]:15s}',
        x["direction"],
        f'entry={x["entry"]:.2f}',
        f'MFE={x["mfe_r"]:.2f}R',
        f'MAE={x["mae_r"]:.2f}R',
        "reasons=",
        x["reasons"],
    )


# ------------------------------------------------------------
# WHICH FILTER BLOCKED WINNERS?
# ------------------------------------------------------------

winner_filter_counts = Counter()

winner_sole_counts = Counter()

for x in profitable:

    for reason in x["reasons"]:
        winner_filter_counts[
            reason
        ] += 1

    if len(x["reasons"]) == 1:
        winner_sole_counts[
            x["reasons"][0]
        ] += 1


print()
print("=" * 100)
print(
    "FILTERS PRESENT ON MISSED 2R WINNERS"
)
print("=" * 100)

for reason, count in (
    winner_filter_counts.most_common()
):
    print(
        f"{reason:50s}{count:7d}"
    )


print()
print("=" * 100)
print(
    "TRUE SOLE FILTERS THAT BLOCKED "
    "A 2R WINNER"
)
print("=" * 100)

for reason, count in (
    winner_sole_counts.most_common()
):
    print(
        f"{reason:50s}{count:7d}"
    )


# ------------------------------------------------------------
# BREAKOUT SUBRULES ON MISSED WINNERS
# ------------------------------------------------------------

winner_breakout_sub = Counter()

for x in profitable:

    b = x["breakout"] or {}

    for reason in (
        b.get("reasons") or []
    ):
        winner_breakout_sub[
            reason
        ] += 1


print()
print("=" * 100)
print(
    "BREAKOUT SUB-RULES PRESENT "
    "ON MISSED 2R WINNERS"
)
print("=" * 100)

for reason, count in (
    winner_breakout_sub.most_common()
):
    print(
        f"{reason:50s}{count:7d}"
    )


# ------------------------------------------------------------
# SAVE CSV
# ------------------------------------------------------------

csv_rows = []

for x in counterfactuals:

    b = x["breakout"] or {}
    m = b.get("metrics") or {}

    csv_rows.append({
        "time": x["dt"],
        "symbol": x["symbol"],
        "direction": x["direction"],
        "entry": x["entry"],
        "outcome": x["outcome"],
        "mfe_r": round(
            x["mfe_r"], 4
        ),
        "mae_r": round(
            x["mae_r"], 4
        ),
        "top_level_reasons":
            "|".join(x["reasons"]),
        "breakout_reasons":
            "|".join(
                b.get("reasons")
                or []
            ),
        "volume_ratio":
            m.get("volume_ratio"),
        "atr_multiplier":
            m.get("atr_multiplier"),
        "clv":
            m.get("clv"),
        "confirmations":
            json.dumps(
                x["confirmations"],
                default=str,
            ),
    })


out = Path(
    "runtime/"
    "aug20_blocked_trade_replay.csv"
)

pd.DataFrame(
    csv_rows
).to_csv(
    out,
    index=False,
)

print()
print(
    "Detailed CSV written to:",
    out,
)
