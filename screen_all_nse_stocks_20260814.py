import json
import os
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from auth import get_kite_client
from paper_watchlist_selector import (
    PaperSelectorSettings,
    evaluate_candidate,
)

IST = ZoneInfo("Asia/Kolkata")

SESSION_DATE = date(2026, 8, 14)
SESSION_START = datetime(
    2026, 8, 14, 9, 15, tzinfo=IST
)
CUTOFF = datetime(
    2026, 8, 14, 9, 27, tzinfo=IST
)
SESSION_END = datetime(
    2026, 8, 14, 15, 30, tzinfo=IST
)

UNIVERSE_PATH = Path(
    "runtime/"
    "kite_all_nse_stocks_eq_series_20260815.json"
)

CHECKPOINT_PATH = Path(
    "runtime/"
    "historical_all_nse_watchlist_"
    "20260814_0927_checkpoint.jsonl"
)

OUTPUT_PATH = Path(
    "runtime/"
    "historical_all_nse_watchlist_"
    "20260814_0927.json"
)

settings = PaperSelectorSettings(
    momentum_min_pct=0.75,
    famine_rvol_min=0.40,
    famine_rvol_max=0.70,
    baseline_days=20,
    historical_lookback_days=45,
    historical_delay_seconds=0.36,
    earliest_famine_time="09:27",
    top_n=60,
)

SESSION_FRACTION = (
    (CUTOFF - SESSION_START).total_seconds()
    / (SESSION_END - SESSION_START).total_seconds()
)


class AuthExpired(RuntimeError):
    pass


def is_auth_error(exc):
    message = str(exc).lower()

    patterns = (
        "access_token",
        "api_key",
        "tokenexception",
        "session expired",
        "incorrect `api_key`",
        "incorrect api_key",
    )

    return any(
        pattern in message
        for pattern in patterns
    )


def historical_with_retry(
    kite,
    instrument_token,
    start,
    end,
    interval,
):
    last_error = None

    for attempt in range(1, 4):
        try:
            rows = kite.historical_data(
                instrument_token,
                start,
                end,
                interval,
            )

            time.sleep(
                settings.historical_delay_seconds
            )

            return rows

        except Exception as exc:
            if is_auth_error(exc):
                raise AuthExpired(str(exc)) from exc

            last_error = exc

            print(
                f"RETRY interval={interval} "
                f"attempt={attempt}/3 "
                f"error={exc}",
                flush=True,
            )

            time.sleep(
                max(
                    settings.historical_delay_seconds,
                    attempt * 1.5,
                )
            )

    raise RuntimeError(
        f"historical_data failed after "
        f"3 attempts: {last_error}"
    )


def candle_timestamp(candle):
    return pd.Timestamp(candle["date"])


def load_latest_checkpoint():
    latest = {}

    if not CHECKPOINT_PATH.exists():
        return latest

    with CHECKPOINT_PATH.open(
        encoding="utf-8"
    ) as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except Exception:
                continue

            symbol = str(
                record.get("symbol") or ""
            ).strip()

            if symbol:
                latest[symbol] = record

    return latest


def append_checkpoint(record):
    CHECKPOINT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with CHECKPOINT_PATH.open(
        "a",
        encoding="utf-8",
    ) as handle:
        handle.write(
            json.dumps(
                record,
                default=str,
                separators=(",", ":"),
            )
            + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())


def build_historical_quote(
    daily_candles,
    intraday_candles,
):
    prior_daily = []

    for candle in daily_candles:
        candle_date = candle_timestamp(
            candle
        ).date()

        if candle_date < SESSION_DATE:
            prior_daily.append(candle)

    prior_daily.sort(
        key=candle_timestamp
    )

    baseline_rows = prior_daily[
        -settings.baseline_days:
    ]

    baseline_volumes = [
        float(row.get("volume") or 0)
        for row in baseline_rows
        if float(row.get("volume") or 0) > 0
    ]

    if (
        len(baseline_volumes)
        < settings.baseline_days
    ):
        average_volume = 0.0
    else:
        average_volume = (
            sum(baseline_volumes)
            / len(baseline_volumes)
        )

    previous_close = (
        float(
            prior_daily[-1].get("close") or 0
        )
        if prior_daily
        else 0.0
    )

    completed = []

    for candle in intraday_candles:
        timestamp = candle_timestamp(candle)

        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize(
                IST
            )
        else:
            timestamp = timestamp.tz_convert(
                IST
            )

        if SESSION_START <= timestamp < CUTOFF:
            completed.append(candle)

    completed.sort(
        key=candle_timestamp
    )

    if not completed:
        quote = {
            "last_price": 0,
            "volume": 0,
            "ohlc": {
                "open": 0,
                "high": 0,
                "low": 0,
                "close": previous_close,
            },
        }
    else:
        quote = {
            "last_price": float(
                completed[-1].get("close") or 0
            ),
            "volume": int(
                sum(
                    float(
                        row.get("volume") or 0
                    )
                    for row in completed
                )
            ),
            "ohlc": {
                "open": float(
                    completed[0].get("open") or 0
                ),
                "high": max(
                    float(
                        row.get("high") or 0
                    )
                    for row in completed
                ),
                "low": min(
                    float(
                        row.get("low") or 0
                    )
                    for row in completed
                ),
                "close": previous_close,
            },
        }

    return (
        quote,
        average_volume,
        len(baseline_volumes),
        len(completed),
    )


def evaluate_stock(kite, stock):
    token = int(stock["instrument_token"])

    daily_start = (
        SESSION_DATE
        - timedelta(
            days=settings.historical_lookback_days
        )
    )

    daily_end = (
        SESSION_DATE
        - timedelta(days=1)
    )

    daily_candles = historical_with_retry(
        kite,
        token,
        daily_start,
        daily_end,
        "day",
    )

    intraday_candles = historical_with_retry(
        kite,
        token,
        SESSION_START,
        CUTOFF,
        "3minute",
    )

    (
        quote,
        average_volume,
        baseline_observations,
        completed_candles,
    ) = build_historical_quote(
        daily_candles,
        intraday_candles,
    )

    evaluation = evaluate_candidate(
        stock,
        quote,
        average_volume,
        SESSION_FRACTION,
        settings,
    )

    evaluation.update(
        {
            "fo_eligible": bool(
                stock.get("fo_eligible")
            ),
            "baseline_observations":
                baseline_observations,
            "completed_3minute_candles":
                completed_candles,
            "point_in_time_cutoff":
                CUTOFF.isoformat(),
        }
    )

    return evaluation


def write_final_report(stocks):
    latest = load_latest_checkpoint()

    evaluations = []
    failures = []

    for stock in stocks:
        symbol = stock["symbol"]
        record = latest.get(symbol)

        if not record:
            continue

        if record.get("status") == "OK":
            evaluations.append(
                record["evaluation"]
            )
        else:
            failures.append(record)

    qualified = sorted(
        [
            row
            for row in evaluations
            if row.get("decision") == "SELECT"
        ],
        key=lambda row: (
            float(row.get("score") or 0),
            float(
                row.get("momentum_pct") or 0
            ),
        ),
        reverse=True,
    )

    selected = []

    for rank, row in enumerate(
        qualified[:settings.top_n],
        start=1,
    ):
        selected_row = dict(row)
        selected_row["rank"] = rank
        selected_row["watchlist_selected"] = True
        selected.append(selected_row)

    rejection_counts = Counter()

    for row in evaluations:
        for reason in (
            row.get("rejection_reasons") or []
        ):
            rejection_counts[str(reason)] += 1

    report = {
        "generated_at": datetime.now(
            IST
        ).isoformat(),
        "date": SESSION_DATE.isoformat(),
        "session_date":
            SESSION_DATE.isoformat(),
        "cutoff": CUTOFF.isoformat(),
        "universe_label":
            "all_nse_eq_series_point_in_time",
        "universe_source": str(
            UNIVERSE_PATH
        ),
        "universe_size": len(stocks),
        "evaluated_count": len(
            evaluations
        ),
        "fetch_failure_count": len(
            failures
        ),
        "qualified_count": len(
            qualified
        ),
        "selected_count": len(
            selected
        ),
        "fo_eligible_universe_count": sum(
            1
            for stock in stocks
            if stock.get("fo_eligible")
        ),
        "fo_eligible_qualified_count": sum(
            1
            for row in qualified
            if row.get("fo_eligible")
        ),
        "fo_eligible_selected_count": sum(
            1
            for row in selected
            if row.get("fo_eligible")
        ),
        "settings": {
            "momentum_min_pct":
                settings.momentum_min_pct,
            "famine_rvol_min":
                settings.famine_rvol_min,
            "famine_rvol_max":
                settings.famine_rvol_max,
            "baseline_days":
                settings.baseline_days,
            "top_n": settings.top_n,
        },
        "methodology": {
            "completed_candles_only": True,
            "last_included_candle_start":
                "09:24",
            "future_price_data_used": False,
            "actual_derivatives_included":
                False,
            "fo_underlying_equities_included":
                True,
            "universe_reconstruction_note":
                "Uses the 2026-08-15 NSE/Kite "
                "instrument master to reconstruct "
                "the 2026-08-14 universe.",
        },
        "rejection_counts": dict(
            rejection_counts.most_common()
        ),
        "qualified": qualified,
        "selected": selected,
        "failures": failures,
        "evaluations": evaluations,
    }

    OUTPUT_PATH.write_text(
        json.dumps(
            report,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    print("\n" + "=" * 94)
    print("ALL-NSE HISTORICAL WATCHLIST SCREEN")
    print("=" * 94)
    print(
        f"Universe                 : "
        f"{len(stocks)}"
    )
    print(
        f"Successfully evaluated   : "
        f"{len(evaluations)}"
    )
    print(
        f"Fetch failures           : "
        f"{len(failures)}"
    )
    print(
        f"Qualified                : "
        f"{len(qualified)}"
    )
    print(
        f"Selected top {settings.top_n:<3}        : "
        f"{len(selected)}"
    )
    print(
        f"F&O qualified            : "
        f"{report['fo_eligible_qualified_count']}"
    )
    print(
        f"F&O selected             : "
        f"{report['fo_eligible_selected_count']}"
    )

    print("\nSELECTED WATCHLIST")

    for row in selected:
        print(
            f"{row['rank']:>2}. "
            f"{row['symbol']:<15} "
            f"momentum="
            f"{row.get('momentum_pct', 0):>7.3f}% "
            f"rvol="
            f"{row.get('relative_volume', 0):>6.3f} "
            f"score="
            f"{row.get('score', 0):>8.3f} "
            f"FO={'Y' if row.get('fo_eligible') else 'N'}"
        )

    print(f"\nDETAIL={OUTPUT_PATH}")


def main():
    universe = json.loads(
        UNIVERSE_PATH.read_text(
            encoding="utf-8"
        )
    )

    stocks = universe.get("stocks") or []

    if not stocks:
        raise SystemExit(
            "EMPTY_UNIVERSE"
        )

    latest = load_latest_checkpoint()

    completed = {
        symbol
        for symbol, record in latest.items()
        if record.get("status") == "OK"
    }

    print("READ_ONLY_SCREENING=True")
    print(
        "UNIVERSE=ALL_NSE_ORDINARY_EQ_SERIES"
    )
    print(
        f"UNIVERSE_SIZE={len(stocks)}"
    )
    print(
        f"ALREADY_COMPLETED={len(completed)}"
    )
    print(
        f"SESSION_DATE={SESSION_DATE}"
    )
    print(
        f"CUTOFF={CUTOFF.isoformat()}"
    )
    print(
        f"SESSION_FRACTION={SESSION_FRACTION:.6f}"
    )
    print(
        "ACTUAL_DERIVATIVES_INCLUDED=False"
    )
    print(
        "FO_UNDERLYING_EQUITIES_INCLUDED=True"
    )
    print(
        "LIVE_WATCHLIST_CHANGED=False"
    )

    kite = get_kite_client()

    processed_this_run = 0

    try:
        for index, stock in enumerate(
            stocks,
            start=1,
        ):
            symbol = stock["symbol"]

            if symbol in completed:
                continue

            try:
                evaluation = evaluate_stock(
                    kite,
                    stock,
                )

                record = {
                    "symbol": symbol,
                    "fo_eligible": bool(
                        stock.get("fo_eligible")
                    ),
                    "status": "OK",
                    "evaluation": evaluation,
                }

            except AuthExpired:
                raise

            except Exception as exc:
                record = {
                    "symbol": symbol,
                    "fo_eligible": bool(
                        stock.get("fo_eligible")
                    ),
                    "status": "ERROR",
                    "error": str(exc),
                }

            append_checkpoint(record)
            processed_this_run += 1

            if (
                processed_this_run % 25 == 0
                or index == len(stocks)
            ):
                print(
                    f"PROGRESS={index}/{len(stocks)} "
                    f"processed_this_run="
                    f"{processed_this_run} "
                    f"symbol={symbol} "
                    f"status={record['status']}",
                    flush=True,
                )

    except AuthExpired as exc:
        print(
            f"\nAUTH_EXPIRED={exc}",
            flush=True,
        )
        print(
            "CHECKPOINT_SAVED=True",
            flush=True,
        )
        print(
            "REAUTHENTICATE_AND_RERUN_SAME_SCRIPT=True",
            flush=True,
        )
        raise SystemExit(2)

    write_final_report(stocks)


if __name__ == "__main__":
    main()
