import bisect
import json
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

HISTORY_PATH = Path("trade_history.jsonl")

OUTPUT_JSON = Path(
    "runtime/pnl_by_rvol_all_days_20260815.json"
)
OUTPUT_CSV = Path(
    "runtime/pnl_by_rvol_all_trades_20260815.csv"
)

MAX_MATCH_SECONDS = 600

BAND_ORDER = [
    "BELOW_0.40",
    "0.40_TO_0.70",
    "0.70_TO_1.00",
    "1.00_TO_1.50",
    "1.50_TO_2.00",
    "AT_OR_ABOVE_2.00",
    "DATA_UNAVAILABLE",
]


def number(value):
    try:
        result = float(value)
        return result if pd.notna(result) else None
    except Exception:
        return None


def first_value(record, keys):
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def recursive_first_number(node, key):
    if isinstance(node, dict):
        if key in node:
            value = number(node.get(key))
            if value is not None:
                return value

        for value in node.values():
            found = recursive_first_number(
                value,
                key,
            )
            if found is not None:
                return found

    elif isinstance(node, list):
        for value in node:
            found = recursive_first_number(
                value,
                key,
            )
            if found is not None:
                return found

    return None


def normalise_symbol(value):
    symbol = str(value or "").strip().upper()

    if ":" in symbol:
        symbol = symbol.split(":", 1)[1]

    return symbol


def normalise_direction(value):
    value = str(value or "").strip().upper()

    if value in {"BUY", "LONG"}:
        return "BUY"

    if value in {"SELL", "SHORT"}:
        return "SELL"

    return ""


def parse_timestamp(value, date_hint=None):
    if value in (None, ""):
        return None

    text = str(value).strip()

    if (
        date_hint
        and len(text) <= 12
        and ":" in text
        and "T" not in text
        and "-" not in text
    ):
        text = f"{date_hint} {text}"

    try:
        timestamp = pd.Timestamp(text)
    except Exception:
        return None

    if pd.isna(timestamp):
        return None

    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(IST)
    else:
        timestamp = timestamp.tz_convert(IST)

    return timestamp


def trade_timestamp(record):
    date_hint = str(
        record.get("date") or ""
    )[:10]

    value = first_value(
        record,
        [
            "entry_time",
            "timestamp",
            "signal_time",
            "fill_time",
            "time",
            "order_timestamp",
        ],
    )

    return parse_timestamp(
        value,
        date_hint=date_hint or None,
    )


def rvol_band(value):
    if value is None or pd.isna(value):
        return "DATA_UNAVAILABLE"

    if value < 0.40:
        return "BELOW_0.40"

    if value < 0.70:
        return "0.40_TO_0.70"

    if value < 1.00:
        return "0.70_TO_1.00"

    if value < 1.50:
        return "1.00_TO_1.50"

    if value < 2.00:
        return "1.50_TO_2.00"

    return "AT_OR_ABOVE_2.00"


def load_actual_trades():
    if not HISTORY_PATH.exists():
        raise SystemExit(
            "trade_history.jsonl is missing"
        )

    trades = []

    with HISTORY_PATH.open(
        encoding="utf-8",
        errors="replace",
    ) as handle:
        for line_number, line in enumerate(
            handle,
            start=1,
        ):
            try:
                record = json.loads(line)
            except Exception:
                continue

            symbol = normalise_symbol(
                first_value(
                    record,
                    [
                        "symbol",
                        "tradingsymbol",
                        "instrument",
                    ],
                )
            )

            if not symbol:
                continue

            net_pnl = number(
                first_value(
                    record,
                    [
                        "net_pnl",
                        "pnl_after_costs",
                        "pnl",
                    ],
                )
            )

            # Only completed records with P&L.
            if net_pnl is None:
                continue

            timestamp = trade_timestamp(record)

            date_value = str(
                record.get("date") or ""
            )[:10]

            if timestamp is not None:
                trade_date = (
                    timestamp.date().isoformat()
                )
            elif date_value:
                trade_date = date_value
            else:
                continue

            gross_pnl = number(
                first_value(
                    record,
                    [
                        "gross_pnl",
                        "gross_profit",
                    ],
                )
            )

            costs = number(
                first_value(
                    record,
                    [
                        "costs",
                        "charges",
                        "estimated_costs",
                        "total_charges",
                    ],
                )
            )

            direct_rvol = (
                recursive_first_number(
                    record,
                    "volume_ratio",
                )
            )

            trades.append(
                {
                    "trade_number":
                        len(trades) + 1,
                    "history_line": line_number,
                    "date": trade_date,
                    "timestamp": (
                        timestamp.isoformat()
                        if timestamp is not None
                        else ""
                    ),
                    "_timestamp": timestamp,
                    "symbol": symbol,
                    "direction":
                        normalise_direction(
                            first_value(
                                record,
                                [
                                    "direction",
                                    "side",
                                    "transaction_type",
                                ],
                            )
                        ),
                    "gross_pnl": gross_pnl,
                    "costs": costs,
                    "net_pnl": net_pnl,
                    "exit_reason": str(
                        first_value(
                            record,
                            [
                                "exit_reason",
                                "result",
                                "reason",
                            ],
                        )
                        or ""
                    ),
                    "direct_rvol": direct_rvol,
                }
            )

    return trades


def observation_nodes(node, context=None):
    context = dict(context or {})

    if isinstance(node, dict):
        mappings = {
            "symbol": [
                "symbol",
                "tradingsymbol",
                "instrument",
            ],
            "direction": [
                "direction",
                "side",
                "transaction_type",
            ],
            "timestamp": [
                "timestamp",
                "time",
                "signal_time",
                "entry_time",
                "candle_time",
            ],
            "event": [
                "event",
                "type",
                "record_type",
                "event_type",
            ],
        }

        for target, keys in mappings.items():
            value = first_value(node, keys)

            if value not in (None, ""):
                context[target] = value

        if "volume_ratio" in node:
            value = number(
                node.get("volume_ratio")
            )

            if value is not None:
                yield {
                    **context,
                    "volume_ratio": value,
                }

        for value in node.values():
            yield from observation_nodes(
                value,
                context,
            )

    elif isinstance(node, list):
        for value in node:
            yield from observation_nodes(
                value,
                context,
            )


def excluded_observation_path(path):
    lowered = str(path).lower()

    exclusions = (
        "/venv/",
        "/.git/",
        "replay",
        "checkpoint",
        "test_",
        ".backup",
        ".before_",
    )

    return any(
        item in lowered
        for item in exclusions
    )


def load_observations():
    observations = []
    source_counts = Counter()

    for path in sorted(
        Path(".").rglob("*.jsonl")
    ):
        if path == HISTORY_PATH:
            continue

        if excluded_observation_path(path):
            continue

        try:
            with path.open(
                encoding="utf-8",
                errors="replace",
            ) as handle:
                for line in handle:
                    if "volume_ratio" not in line:
                        continue

                    try:
                        record = json.loads(line)
                    except Exception:
                        continue

                    for node in observation_nodes(
                        record
                    ):
                        symbol = normalise_symbol(
                            node.get("symbol")
                        )

                        timestamp = parse_timestamp(
                            node.get("timestamp")
                        )

                        if (
                            not symbol
                            or timestamp is None
                        ):
                            continue

                        event = str(
                            node.get("event") or ""
                        ).upper()

                        observations.append(
                            {
                                "symbol": symbol,
                                "direction":
                                    normalise_direction(
                                        node.get(
                                            "direction"
                                        )
                                    ),
                                "timestamp": timestamp,
                                "epoch":
                                    timestamp.timestamp(),
                                "volume_ratio": float(
                                    node["volume_ratio"]
                                ),
                                "event": event,
                                "source": str(path),
                            }
                        )

                        source_counts[str(path)] += 1

        except Exception:
            continue

    # Remove duplicate observations copied between logs.
    unique = {}

    for row in observations:
        key = (
            row["symbol"],
            row["direction"],
            row["timestamp"].isoformat(),
            round(row["volume_ratio"], 8),
        )

        existing = unique.get(key)

        # Prefer explicit EXPERIMENT_OBSERVATION.
        if existing is None:
            unique[key] = row
        elif (
            "EXPERIMENT_OBSERVATION"
            in row["event"]
            and "EXPERIMENT_OBSERVATION"
            not in existing["event"]
        ):
            unique[key] = row

    return list(unique.values()), source_counts


def build_observation_index(observations):
    index = defaultdict(list)

    for row in observations:
        index[
            (
                row["symbol"],
                row["direction"],
            )
        ].append(row)

        if row["direction"]:
            index[
                (
                    row["symbol"],
                    "",
                )
            ].append(row)

    result = {}

    for key, rows in index.items():
        rows.sort(
            key=lambda row: row["epoch"]
        )

        result[key] = {
            "times": [
                row["epoch"]
                for row in rows
            ],
            "rows": rows,
        }

    return result


def nearest_observation(
    index,
    symbol,
    direction,
    timestamp,
):
    if timestamp is None:
        return None, None

    pools = []

    exact = index.get(
        (symbol, direction)
    )

    if exact:
        pools.append(exact)

    directionless = index.get(
        (symbol, "")
    )

    if (
        directionless
        and directionless is not exact
    ):
        pools.append(directionless)

    candidates = []

    target = timestamp.timestamp()

    for pool in pools:
        position = bisect.bisect_left(
            pool["times"],
            target,
        )

        for offset in range(-4, 5):
            candidate_position = (
                position + offset
            )

            if (
                0
                <= candidate_position
                < len(pool["rows"])
            ):
                candidates.append(
                    pool["rows"][
                        candidate_position
                    ]
                )

    if not candidates:
        return None, None

    candidate = min(
        candidates,
        key=lambda row: abs(
            row["epoch"] - target
        ),
    )

    difference = abs(
        candidate["epoch"] - target
    )

    if difference > MAX_MATCH_SECONDS:
        return None, None

    # Never match observations from another date.
    if (
        candidate["timestamp"].date()
        != timestamp.date()
    ):
        return None, None

    return candidate, difference


def summarise(rows):
    trades = len(rows)
    wins = sum(
        1
        for row in rows
        if row["net_pnl"] > 0
    )
    losses = sum(
        1
        for row in rows
        if row["net_pnl"] < 0
    )
    flats = trades - wins - losses

    available = [
        row["volume_ratio"]
        for row in rows
        if row["volume_ratio"] is not None
    ]

    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate": (
            wins / trades * 100
            if trades
            else 0
        ),
        "gross_pnl": sum(
            row["gross_pnl"] or 0
            for row in rows
        ),
        "costs": sum(
            row["costs"] or 0
            for row in rows
        ),
        "net_pnl": sum(
            row["net_pnl"]
            for row in rows
        ),
        "average_rvol": (
            sum(available) / len(available)
            if available
            else None
        ),
        "median_rvol": (
            float(pd.Series(available).median())
            if available
            else None
        ),
        "rvol_available": len(available),
    }


def main():
    trades = load_actual_trades()

    observations, source_counts = (
        load_observations()
    )

    observation_index = (
        build_observation_index(
            observations
        )
    )

    matched_sources = Counter()

    final_rows = []

    for trade in trades:
        volume_ratio = trade["direct_rvol"]
        source = (
            "trade_history.jsonl"
            if volume_ratio is not None
            else ""
        )
        match_seconds = 0

        if volume_ratio is None:
            observation, difference = (
                nearest_observation(
                    observation_index,
                    trade["symbol"],
                    trade["direction"],
                    trade["_timestamp"],
                )
            )

            if observation is not None:
                volume_ratio = observation[
                    "volume_ratio"
                ]
                source = observation["source"]
                match_seconds = difference
                matched_sources[source] += 1

        final_rows.append(
            {
                "trade_number":
                    trade["trade_number"],
                "date": trade["date"],
                "timestamp": trade["timestamp"],
                "symbol": trade["symbol"],
                "direction":
                    trade["direction"],
                "gross_pnl":
                    trade["gross_pnl"],
                "costs": trade["costs"],
                "net_pnl":
                    trade["net_pnl"],
                "volume_ratio":
                    volume_ratio,
                "volume_band":
                    rvol_band(volume_ratio),
                "rvol_source": source,
                "rvol_match_seconds":
                    match_seconds
                    if source
                    else None,
                "exit_reason":
                    trade["exit_reason"],
            }
        )

    by_date = {}

    for trade_date in sorted(
        {
            row["date"]
            for row in final_rows
        }
    ):
        date_rows = [
            row
            for row in final_rows
            if row["date"] == trade_date
        ]

        band_summaries = {}

        for band in BAND_ORDER:
            band_rows = [
                row
                for row in date_rows
                if row["volume_band"] == band
            ]

            if band_rows:
                band_summaries[band] = (
                    summarise(band_rows)
                )

        by_date[trade_date] = {
            "overall": summarise(
                date_rows
            ),
            "rvol_bands": band_summaries,
        }

    aggregate_bands = {}

    for band in BAND_ORDER:
        band_rows = [
            row
            for row in final_rows
            if row["volume_band"] == band
        ]

        aggregate_bands[band] = summarise(
            band_rows
        )

    broader_groups = {
        "BELOW_1.50": [
            row
            for row in final_rows
            if (
                row["volume_ratio"] is not None
                and row["volume_ratio"] < 1.50
            )
        ],
        "1.50_TO_2.00": [
            row
            for row in final_rows
            if (
                row["volume_ratio"] is not None
                and 1.50
                <= row["volume_ratio"]
                < 2.00
            )
        ],
        "AT_OR_ABOVE_2.00": [
            row
            for row in final_rows
            if (
                row["volume_ratio"] is not None
                and row["volume_ratio"] >= 2.00
            )
        ],
        "DATA_UNAVAILABLE": [
            row
            for row in final_rows
            if row["volume_ratio"] is None
        ],
    }

    report = {
        "cohort":
            "ACTUAL_RECORDED_CLOSED_TRADES",
        "history_file": str(
            HISTORY_PATH
        ),
        "rvol_definition":
            "logged trade-entry volume_ratio",
        "maximum_observation_match_seconds":
            MAX_MATCH_SECONDS,
        "overall": summarise(final_rows),
        "by_date": by_date,
        "aggregate_rvol_bands":
            aggregate_bands,
        "broader_comparison": {
            name: summarise(rows)
            for name, rows
            in broader_groups.items()
        },
        "matched_sources": dict(
            matched_sources
        ),
        "available_observation_sources":
            dict(source_counts),
        "trades": final_rows,
    }

    OUTPUT_JSON.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT_JSON.write_text(
        json.dumps(
            report,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    pd.DataFrame(final_rows).to_csv(
        OUTPUT_CSV,
        index=False,
    )

    print("READ_ONLY_ANALYSIS=True")
    print(
        "COHORT=ACTUAL_RECORDED_CLOSED_TRADES"
    )
    print(
        f"TRADES={len(final_rows)}"
    )
    print(
        f"RVOL_OBSERVATIONS_FOUND="
        f"{len(observations)}"
    )
    print(
        f"RVOL_MATCHED="
        f"{report['overall']['rvol_available']}"
    )
    print(
        "RVOL_UNAVAILABLE="
        f"{len(final_rows) - report['overall']['rvol_available']}"
    )

    print("\nALL DAYS")

    for trade_date, result in by_date.items():
        overall = result["overall"]

        print(
            f"\n{trade_date} "
            f"trades={overall['trades']:3d} "
            f"wins={overall['wins']:3d} "
            f"losses={overall['losses']:3d} "
            f"win_rate="
            f"{overall['win_rate']:6.2f}% "
            f"net=Rs "
            f"{overall['net_pnl']:+.2f}"
        )

        for band in BAND_ORDER:
            values = result[
                "rvol_bands"
            ].get(band)

            if not values:
                continue

            print(
                f"  {band:<20} "
                f"trades={values['trades']:3d} "
                f"wins={values['wins']:3d} "
                f"losses={values['losses']:3d} "
                f"win_rate="
                f"{values['win_rate']:6.2f}% "
                f"net=Rs "
                f"{values['net_pnl']:+.2f}"
            )

    print("\nAGGREGATE RVOL BANDS")

    for band in BAND_ORDER:
        values = aggregate_bands[band]

        print(
            f"{band:<20} "
            f"trades={values['trades']:3d} "
            f"wins={values['wins']:3d} "
            f"losses={values['losses']:3d} "
            f"win_rate="
            f"{values['win_rate']:6.2f}% "
            f"net=Rs "
            f"{values['net_pnl']:+.2f}"
        )

    print("\nBROADER COMPARISON")

    for name, rows in broader_groups.items():
        values = summarise(rows)

        print(
            f"{name:<20} "
            f"trades={values['trades']:3d} "
            f"wins={values['wins']:3d} "
            f"losses={values['losses']:3d} "
            f"win_rate="
            f"{values['win_rate']:6.2f}% "
            f"net=Rs "
            f"{values['net_pnl']:+.2f}"
        )

    print("\nMATCHED RVOL SOURCES")

    if matched_sources:
        for source, count in (
            matched_sources.most_common()
        ):
            print(
                f"{count:4d}  {source}"
            )
    else:
        print("NONE")

    print(f"\nDETAIL={OUTPUT_JSON}")
    print(f"CSV={OUTPUT_CSV}")


if __name__ == "__main__":
    main()
