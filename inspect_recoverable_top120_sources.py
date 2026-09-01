import json
from pathlib import Path
from collections import defaultdict

import pandas as pd


ROOT = Path("runtime")

DATES = [
    "2026-08-12",
    "2026-08-13",
    "2026-08-14",
    "2026-08-20",
    "2026-08-21",
]

IMPORTANT_SYMBOLS = {
    "2026-08-12": [
        "CUPID",
        "DIVGIITTS",
        "GIPCL",
        "FINCABLES",
        "DYCL",
        "CELLO",
        "ZYDUSLIFE",
        "AGARWALEYE",
    ],
    "2026-08-13": [
        "AEQUS",
        "DICIND",
        "NATIONALUM",
        "VOGL",
        "INDSWFTLAB",
        "IRCON",
    ],
    "2026-08-14": [
        "TARSONS",
    ],
    "2026-08-20": [
        "ASTERDM",
    ],
    "2026-08-21": [
        "KRONOX",
    ],
}


# ============================================================
# Candidate files
# ============================================================

CANDIDATES = [
    # Exact historical full-universe source
    ROOT / "historical_all_nse_watchlist_20260814_0927.json",
    ROOT / "historical_all_nse_watchlist_20260814_0927_checkpoint.jsonl",

    # Aug 20 sources
    ROOT / "replay/aug20_saved_top60_report.json",
    ROOT / "replay/aug20_current_logic_top60.json",
    ROOT / "replay/aug20_scored_breakout_top60.json",
    ROOT / "replay/aug20_two_candle_top60.json",

    # Aug 21 current/live sources
    ROOT / "live_watchlist/latest_report.json",
    ROOT / "live_watchlist/latest_watchlist.json",

    # Historical multi-day files that may contain features
    ROOT / "watchlist_extremes_20260810_20260814.csv",
    ROOT / "watchlist_extremes_20260810_20260814.json",

    ROOT / (
        "watchlist_missed_opportunity/"
        "market_sector_timeframe_comparison/"
        "feature_comparison.csv"
    ),

    ROOT / (
        "watchlist_missed_opportunity/"
        "market_sector_stock_direction_test/"
        "feature_level.csv"
    ),

    ROOT / (
        "watchlist_missed_opportunity/"
        "direction_regime_test/"
        "trade_level_results.csv"
    ),
]


# Add likely files automatically
for pattern in [
    "**/*watchlist*.json",
    "**/*watchlist*.csv",
    "**/*feature*.csv",
    "**/*top60*.json",
    "**/*top120*.json",
    "**/*selector*.json",
]:
    for p in ROOT.glob(pattern):
        try:
            if p.is_file() and p.stat().st_size <= 50_000_000:
                CANDIDATES.append(p)
        except Exception:
            pass


CANDIDATES = sorted(set(CANDIDATES))


# ============================================================
# Helpers
# ============================================================

MOMENTUM_KEYS = {
    "momentum_pct",
    "momentum",
    "day_range_pct",
}

RVOL_KEYS = {
    "relative_volume",
    "rvol",
}

SYMBOL_KEYS = {
    "symbol",
    "tradingsymbol",
}

DATE_KEYS = {
    "date",
    "generated_at",
    "timestamp",
    "signal_ts",
}


def normalize_date(value):
    if value is None:
        return None

    try:
        return pd.to_datetime(value).date().isoformat()
    except Exception:
        return None


def inspect_dict_tree(obj, path="", inherited_date=None, rows=None):
    if rows is None:
        rows = []

    if isinstance(obj, dict):

        current_date = inherited_date

        for k in DATE_KEYS:
            if k in obj:
                d = normalize_date(obj.get(k))
                if d:
                    current_date = d
                    break

        symbol = None

        for k in SYMBOL_KEYS:
            if obj.get(k):
                symbol = str(obj.get(k)).upper()
                break

        momentum = None

        for k in MOMENTUM_KEYS:
            if obj.get(k) is not None:
                momentum = obj.get(k)
                break

        rvol = None

        for k in RVOL_KEYS:
            if obj.get(k) is not None:
                rvol = obj.get(k)
                break

        if symbol is not None and (
            momentum is not None or rvol is not None
        ):
            rows.append({
                "date": current_date,
                "symbol": symbol,
                "momentum": momentum,
                "rvol": rvol,
                "path": path,
            })

        for key, value in obj.items():
            inspect_dict_tree(
                value,
                path=f"{path}/{key}",
                inherited_date=current_date,
                rows=rows,
            )

    elif isinstance(obj, list):

        for i, value in enumerate(obj):
            inspect_dict_tree(
                value,
                path=f"{path}[{i}]",
                inherited_date=inherited_date,
                rows=rows,
            )

    return rows


def inspect_json(path):
    try:
        obj = json.loads(
            path.read_text(errors="ignore")
        )
    except Exception:
        return []

    return inspect_dict_tree(obj)


def inspect_jsonl(path):
    rows = []

    try:
        with path.open(errors="ignore") as f:
            for i, line in enumerate(f, 1):
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                inspect_dict_tree(
                    obj,
                    path=f"line:{i}",
                    rows=rows,
                )

    except Exception:
        return []

    return rows


def find_col(cols, possibilities):
    lookup = {
        str(c).lower(): c
        for c in cols
    }

    for name in possibilities:
        if name in lookup:
            return lookup[name]

    return None


def inspect_csv(path):
    try:
        df = pd.read_csv(
            path,
            low_memory=False,
        )
    except Exception:
        return []

    symbol_col = find_col(
        df.columns,
        [
            "symbol",
            "tradingsymbol",
        ],
    )

    momentum_col = find_col(
        df.columns,
        [
            "momentum_pct",
            "momentum",
            "day_range_pct",
        ],
    )

    rvol_col = find_col(
        df.columns,
        [
            "relative_volume",
            "rvol",
        ],
    )

    date_col = find_col(
        df.columns,
        [
            "date",
            "generated_at",
            "timestamp",
            "signal_ts",
        ],
    )

    if symbol_col is None:
        return []

    if momentum_col is None and rvol_col is None:
        return []

    rows = []

    for _, r in df.iterrows():

        symbol = str(
            r.get(symbol_col, "")
        ).upper()

        if not symbol or symbol == "NAN":
            continue

        d = None

        if date_col is not None:
            d = normalize_date(
                r.get(date_col)
            )

        rows.append({
            "date": d,
            "symbol": symbol,
            "momentum":
                r.get(momentum_col)
                if momentum_col is not None
                else None,
            "rvol":
                r.get(rvol_col)
                if rvol_col is not None
                else None,
            "path": "CSV",
        })

    return rows


def inspect_file(path):

    suffix = path.suffix.lower()

    if suffix == ".json":
        return inspect_json(path)

    if suffix == ".jsonl":
        return inspect_jsonl(path)

    if suffix == ".csv":
        return inspect_csv(path)

    return []


# ============================================================
# Main
# ============================================================

print("=" * 110)
print("TOP120 HISTORICAL SOURCE FORENSICS")
print("=" * 110)

results = []

for path in CANDIDATES:

    if not path.exists():
        continue

    try:
        rows = inspect_file(path)
    except Exception as exc:
        print(
            "ERROR:",
            path,
            repr(exc),
        )
        continue

    if not rows:
        continue

    frame = pd.DataFrame(rows)

    if frame.empty:
        continue

    frame["momentum_num"] = pd.to_numeric(
        frame["momentum"],
        errors="coerce",
    )

    frame["rvol_num"] = pd.to_numeric(
        frame["rvol"],
        errors="coerce",
    )

    both = frame[
        frame["momentum_num"].notna()
        &
        frame["rvol_num"].notna()
    ].copy()

    if both.empty:
        continue

    dates = sorted(
        d for d in both["date"]
        .dropna()
        .unique()
        if d in DATES
    )

    important_hits = []

    for date, symbols in IMPORTANT_SYMBOLS.items():

        temp = both[
            (
                (both["date"] == date)
                |
                (both["date"].isna())
            )
            &
            both["symbol"].isin(symbols)
        ]

        for _, row in temp.iterrows():

            important_hits.append({
                "date": date,
                "symbol": row["symbol"],
                "momentum": row["momentum_num"],
                "rvol": row["rvol_num"],
            })

    results.append({
        "file": str(path),
        "size": path.stat().st_size,
        "feature_rows": len(both),
        "unique_symbols": both["symbol"].nunique(),
        "dates": dates,
        "important_hits": important_hits,
    })


# ============================================================
# Summary
# ============================================================

print()
print("FILES WITH BOTH MOMENTUM + RVOL")
print("-" * 110)

results = sorted(
    results,
    key=lambda x: (
        len(x["dates"]),
        x["unique_symbols"],
        x["feature_rows"],
    ),
    reverse=True,
)

for x in results:

    print()
    print(x["file"])

    print(
        f"  size           : {x['size']:,}"
    )

    print(
        f"  feature rows   : {x['feature_rows']:,}"
    )

    print(
        f"  unique symbols : {x['unique_symbols']:,}"
    )

    print(
        "  target dates   :",
        x["dates"] or "DATE NOT EMBEDDED",
    )

    if x["important_hits"]:

        print("  IMPORTANT STOCK HITS:")

        seen = set()

        for hit in x["important_hits"]:

            key = (
                hit["date"],
                hit["symbol"],
                hit["momentum"],
                hit["rvol"],
            )

            if key in seen:
                continue

            seen.add(key)

            print(
                f"    {hit['date']} "
                f"{hit['symbol']:<12} "
                f"momentum={hit['momentum']:.6f} "
                f"rvol={hit['rvol']:.6f}"
            )


# ============================================================
# Coverage by date
# ============================================================

print()
print("=" * 110)
print("BEST RECOVERY SOURCES BY DATE")
print("=" * 110)

for date in DATES:

    print()
    print(date)
    print("-" * 60)

    candidates = []

    for x in results:

        explicit_date = date in x["dates"]

        hits = [
            h
            for h in x["important_hits"]
            if h["date"] == date
        ]

        if explicit_date or hits:

            candidates.append(
                (
                    explicit_date,
                    len(hits),
                    x["unique_symbols"],
                    x["feature_rows"],
                    x,
                )
            )

    candidates.sort(
        key=lambda z: (
            z[0],
            z[1],
            z[2],
            z[3],
        ),
        reverse=True,
    )

    if not candidates:
        print(
            "❌ No source found with both "
            "momentum + RVOL."
        )
        continue

    for _, hit_count, _, _, x in candidates[:10]:

        print(
            f"{x['file']}\n"
            f"    symbols={x['unique_symbols']} "
            f"rows={x['feature_rows']} "
            f"important_hits={hit_count}"
        )


# ============================================================
# Important symbols exact recovery
# ============================================================

print()
print("=" * 110)
print("IMPORTANT TRADE STOCK RECOVERY")
print("=" * 110)

for date, symbols in IMPORTANT_SYMBOLS.items():

    print()
    print(date)

    for symbol in symbols:

        found = []

        for x in results:

            for hit in x["important_hits"]:

                if (
                    hit["date"] == date
                    and
                    hit["symbol"] == symbol
                ):
                    found.append(
                        (
                            x["file"],
                            hit["momentum"],
                            hit["rvol"],
                        )
                    )

        if not found:

            print(
                f"  {symbol:<12} ❌ NOT RECOVERED"
            )

        else:

            print(
                f"  {symbol:<12} ✅ RECOVERED "
                f"from {len(found)} source(s)"
            )

            shown = set()

            for file, momentum, rvol in found:

                values = (
                    round(float(momentum), 8),
                    round(float(rvol), 8),
                )

                if values in shown:
                    continue

                shown.add(values)

                print(
                    f"      momentum={momentum:.6f} "
                    f"rvol={rvol:.6f}"
                )

                print(
                    f"      source={file}"
                )


print()
print("=" * 110)
print("DONE")
print("=" * 110)

