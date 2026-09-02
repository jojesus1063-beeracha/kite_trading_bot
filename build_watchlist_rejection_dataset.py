import json
from pathlib import Path
import pandas as pd

ROOT = Path("runtime")

OUTDIR = ROOT / "watchlist_missed_opportunity"
OUTDIR.mkdir(parents=True, exist_ok=True)

# Historical selector snapshots already present on your VM
PATTERNS = [
    "historical_all_nse_watchlist_*.json",
    "historical_all_nse_watchlist_*_checkpoint.jsonl",
    "paper_watchlist/selection_audit.jsonl",
]

rows = []

def add_decision(x, source, run_date=None):
    if not isinstance(x, dict):
        return

    symbol = x.get("symbol")
    if not symbol:
        return

    thresholds = x.get("thresholds") or {}

    momentum = (
        x.get("momentum_pct")
        if x.get("momentum_pct") is not None
        else x.get("day_range_pct")
    )

    rvol = (
        x.get("relative_volume")
        if x.get("relative_volume") is not None
        else x.get("rvol")
    )

    momentum_pass = x.get("momentum_pass")
    famine_pass = x.get("famine_pass")
    selected = x.get("selected")

    decision = x.get("decision")

    if selected is None and decision is not None:
        selected = str(decision).upper() == "SELECT"

    if selected is None:
        return

    if run_date is None:
        generated = (
            x.get("generated_at")
            or x.get("date")
            or x.get("timestamp")
        )

        if generated:
            try:
                run_date = pd.to_datetime(generated).date().isoformat()
            except Exception:
                run_date = None

    reasons = x.get("selection_reasons") or x.get("reasons") or []

    if isinstance(reasons, list):
        reasons = "|".join(str(v) for v in reasons)
    else:
        reasons = str(reasons)

    reject_reason = []

    if selected is False:
        if momentum_pass is False:
            reject_reason.append("MOMENTUM_FAIL")

        if famine_pass is False:
            if rvol is not None:
                try:
                    rv = float(rvol)

                    rv_min = float(
                        thresholds.get(
                            "famine_rvol_min",
                            0.40
                        )
                    )

                    rv_max = float(
                        thresholds.get(
                            "famine_rvol_max",
                            0.70
                        )
                    )

                    if rv < rv_min:
                        reject_reason.append("RVOL_BELOW_MIN")
                    elif rv > rv_max:
                        reject_reason.append("RVOL_ABOVE_MAX")
                    else:
                        reject_reason.append("RVOL_FAIL")
                except Exception:
                    reject_reason.append("RVOL_FAIL")
            else:
                reject_reason.append("RVOL_MISSING")

    rows.append({
        "date": run_date,
        "symbol": str(symbol),
        "exchange": x.get("exchange", "NSE"),
        "instrument_token": x.get("instrument_token"),
        "selected": bool(selected),
        "decision": decision,
        "momentum_pct": momentum,
        "relative_volume": rvol,
        "momentum_pass": momentum_pass,
        "famine_pass": famine_pass,

        "momentum_min_pct":
            thresholds.get("momentum_min_pct", 0.75),

        "famine_rvol_min":
            thresholds.get("famine_rvol_min", 0.40),

        "famine_rvol_max":
            thresholds.get("famine_rvol_max", 0.70),

        "change_pct": x.get("change_pct"),
        "day_range_pct": x.get("day_range_pct"),

        "last_price": x.get("last_price"),
        "open": x.get("open"),
        "high": x.get("high"),
        "low": x.get("low"),
        "previous_close": x.get("previous_close"),

        "current_volume": x.get("current_volume"),
        "average_daily_volume": x.get("average_daily_volume"),
        "expected_volume_now": x.get("expected_volume_now"),

        "score": x.get("score"),
        "selection_reasons": reasons,
        "reject_reason": "|".join(reject_reason),
        "source_file": str(source),
    })


def parse_json_file(p):
    try:
        obj = json.loads(p.read_text(errors="ignore"))
    except Exception:
        return

    run_date = None

    generated = obj.get("generated_at") if isinstance(obj, dict) else None

    if generated:
        try:
            run_date = pd.to_datetime(generated).date().isoformat()
        except Exception:
            pass

    if isinstance(obj, dict):

        # Full decisions array
        for key in [
            "decisions",
            "selected",
            "rejected",
            "candidates",
            "rows",
        ]:
            vals = obj.get(key)

            if isinstance(vals, list):
                for x in vals:
                    add_decision(
                        x,
                        p,
                        run_date=run_date
                    )

        # Some historical files may store one symbol per key
        if not any(
            isinstance(obj.get(k), list)
            for k in [
                "decisions",
                "selected",
                "rejected",
                "candidates",
                "rows",
            ]
        ):
            for _, value in obj.items():
                if isinstance(value, dict):
                    add_decision(
                        value,
                        p,
                        run_date=run_date
                    )

    elif isinstance(obj, list):
        for x in obj:
            add_decision(
                x,
                p,
                run_date=run_date
            )


def parse_jsonl_file(p):
    for line in p.open(errors="ignore"):
        line = line.strip()

        if not line:
            continue

        try:
            obj = json.loads(line)
        except Exception:
            continue

        run_date = None

        generated = (
            obj.get("generated_at")
            or obj.get("date")
            or obj.get("timestamp")
        )

        if generated:
            try:
                run_date = (
                    pd.to_datetime(generated)
                    .date()
                    .isoformat()
                )
            except Exception:
                pass

        # Selection audit run
        if isinstance(obj.get("decisions"), list):
            for x in obj["decisions"]:
                add_decision(
                    x,
                    p,
                    run_date=run_date
                )

        else:
            add_decision(
                obj,
                p,
                run_date=run_date
            )


files = []

for pattern in PATTERNS:
    files.extend(ROOT.glob(pattern))

files = sorted(set(files))

print("===== WATCHLIST SOURCE FILES =====")
print("Files found:", len(files))

for p in files:
    print(p)

for p in files:
    if p.suffix == ".jsonl":
        parse_jsonl_file(p)
    elif p.suffix == ".json":
        parse_json_file(p)


df = pd.DataFrame(rows)

if df.empty:
    raise SystemExit(
        "No selector decisions found."
    )

# Numeric conversion
for col in [
    "momentum_pct",
    "relative_volume",
    "change_pct",
    "day_range_pct",
    "last_price",
    "current_volume",
    "average_daily_volume",
    "expected_volume_now",
    "score",
]:
    if col in df:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )

# Deduplicate same stock/date/decision
df = (
    df.sort_values(
        ["date", "symbol", "source_file"]
    )
    .drop_duplicates(
        ["date", "symbol", "selected"],
        keep="last"
    )
    .reset_index(drop=True)
)

selected = df[df["selected"] == True].copy()
rejected = df[df["selected"] == False].copy()

# Explicit rejection classification
def classify(row):
    if bool(row["selected"]):
        return "SELECTED"

    m = row.get("momentum_pct")
    rv = row.get("relative_volume")

    reasons = []

    if pd.notna(m) and m < 0.75:
        reasons.append("LOW_MOMENTUM")

    if pd.notna(rv):
        if rv < 0.40:
            reasons.append("RVOL_LT_0.40")
        elif rv > 0.70:
            reasons.append("RVOL_GT_0.70")

    if not reasons:
        reasons.append("OTHER")

    return "+".join(reasons)

df["watchlist_group"] = df.apply(
    classify,
    axis=1
)

selected = df[df["selected"] == True].copy()
rejected = df[df["selected"] == False].copy()

# Save
df.to_csv(
    OUTDIR / "all_watchlist_decisions.csv",
    index=False
)

selected.to_csv(
    OUTDIR / "selected_stocks.csv",
    index=False
)

rejected.to_csv(
    OUTDIR / "rejected_stocks.csv",
    index=False
)

print("\n===== DATASET SUMMARY =====")

print("Rows                 :", len(df))
print("Dates                :", df["date"].nunique())
print("Unique symbols       :", df["symbol"].nunique())
print("Selected rows        :", len(selected))
print("Rejected rows        :", len(rejected))

print("\n===== REJECTION GROUPS =====")

print(
    df[df["selected"] == False]
    ["watchlist_group"]
    .value_counts(dropna=False)
    .to_string()
)

print("\n===== RVOL BANDS AMONG REJECTED =====")

rv = rejected.copy()

rv["rvol_band"] = pd.cut(
    rv["relative_volume"],
    bins=[
        -float("inf"),
        0.40,
        0.70,
        1.00,
        1.50,
        2.00,
        float("inf"),
    ],
    labels=[
        "<0.40",
        "0.40-0.70",
        "0.70-1.00",
        "1.00-1.50",
        "1.50-2.00",
        ">2.00",
    ],
    right=False,
)

print(
    rv["rvol_band"]
    .value_counts(sort=False)
    .to_string()
)

print("\n===== HIGH MOMENTUM BUT REJECTED =====")

high_momentum_rejected = rejected[
    rejected["momentum_pct"] >= 0.75
].copy()

print(
    high_momentum_rejected[
        [
            "date",
            "symbol",
            "momentum_pct",
            "relative_volume",
            "watchlist_group",
        ]
    ]
    .sort_values(
        ["date", "momentum_pct"],
        ascending=[True, False]
    )
    .to_string(index=False)
)

high_momentum_rejected.to_csv(
    OUTDIR /
    "high_momentum_rejected_stocks.csv",
    index=False
)

print("\nWROTE:")
for p in sorted(OUTDIR.glob("*.csv")):
    print(" ", p)
