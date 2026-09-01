from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time

import pandas as pd

from auth import get_kite_client

IST = ZoneInfo("Asia/Kolkata")

# --------------------------------------------------
# SETTINGS
# --------------------------------------------------

START_DATE = "2026-07-29"
END_DATE   = "2026-08-21"

OUT = Path(
    "runtime/trade_replay_history/"
    "candles_3minute"
)
OUT.mkdir(parents=True, exist_ok=True)

# Canonical labels we want to save under.
# Candidate instrument names allow for Zerodha naming variations.
INDEXES = {
    "NIFTY50": [
        "NIFTY 50",
    ],
    "NIFTYBANK": [
        "NIFTY BANK",
    ],
    "NIFTYFINSERVICE": [
        "NIFTY FIN SERVICE",
        "NIFTY FINANCIAL SERVICES",
    ],
    "NIFTYIT": [
        "NIFTY IT",
    ],
    "NIFTYAUTO": [
        "NIFTY AUTO",
    ],
    "NIFTYMETAL": [
        "NIFTY METAL",
    ],
    "NIFTYFMCG": [
        "NIFTY FMCG",
    ],
    "NIFTYPHARMA": [
        "NIFTY PHARMA",
    ],
    "NIFTYHEALTHCARE": [
        "NIFTY HEALTHCARE INDEX",
        "NIFTY HEALTHCARE",
    ],
    "NIFTYENERGY": [
        "NIFTY ENERGY",
    ],
    "NIFTYREALTY": [
        "NIFTY REALTY",
    ],
    "NIFTYPSUBANK": [
        "NIFTY PSU BANK",
    ],
    "NIFTYMEDIA": [
        "NIFTY MEDIA",
    ],
    "NIFTYCONSUMER": [
        "NIFTY CONSUMER DURABLES",
    ],
    "NIFTYINFRA": [
        "NIFTY INFRASTRUCTURE",
    ],
}

# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def norm(s):
    return (
        str(s)
        .strip()
        .upper()
        .replace("-", "")
        .replace("_", "")
        .replace(" ", "")
    )


def resolve_indexes(kite):
    print("Loading NFO/NSE instruments...")

    frames = []

    for exchange in ["NSE"]:
        try:
            data = kite.instruments(exchange)
            if data:
                f = pd.DataFrame(data)
                f["exchange_source"] = exchange
                frames.append(f)
        except Exception as e:
            print(f"{exchange} instrument load failed: {e}")

    if not frames:
        raise SystemExit("No instruments loaded.")

    inst = pd.concat(
        frames,
        ignore_index=True
    )

    print("Instrument rows:", len(inst))
    print("Columns:", list(inst.columns))

    # Index rows commonly show segment INDICES.
    if "segment" in inst.columns:
        idx = inst[
            inst["segment"]
            .astype(str)
            .str.upper()
            .str.contains("INDICES", na=False)
        ].copy()
    else:
        idx = inst.copy()

    if idx.empty:
        print(
            "WARNING: no segment=INDICES rows found; "
            "searching complete NSE instrument list."
        )
        idx = inst.copy()

    idx["name_norm"] = (
        idx["name"]
        .astype(str)
        .map(norm)
    )

    if "tradingsymbol" in idx.columns:
        idx["symbol_norm"] = (
            idx["tradingsymbol"]
            .astype(str)
            .map(norm)
        )
    else:
        idx["symbol_norm"] = ""

    resolved = {}
    unresolved = []

    for save_name, candidates in INDEXES.items():

        match = None

        for candidate in candidates:
            n = norm(candidate)

            exact = idx[
                (idx["name_norm"] == n)
                |
                (idx["symbol_norm"] == n)
            ]

            if not exact.empty:
                match = exact.iloc[0]
                break

        if match is None:
            # Loose fallback.
            for candidate in candidates:
                n = norm(candidate)

                loose = idx[
                    idx["name_norm"].str.contains(
                        n,
                        regex=False,
                        na=False
                    )
                    |
                    idx["symbol_norm"].str.contains(
                        n,
                        regex=False,
                        na=False
                    )
                ]

                if not loose.empty:
                    match = loose.iloc[0]
                    break

        if match is None:
            unresolved.append(save_name)
            continue

        resolved[save_name] = {
            "instrument_token":
                int(match["instrument_token"]),
            "name":
                str(match.get("name", "")),
            "tradingsymbol":
                str(match.get("tradingsymbol", "")),
            "segment":
                str(match.get("segment", "")),
            "exchange":
                str(match.get("exchange", "NSE")),
        }

    print("\n===== INDEX RESOLUTION =====")

    for k, v in resolved.items():
        print(
            f"{k:20s} "
            f"token={v['instrument_token']} "
            f"name={v['name']} "
            f"symbol={v['tradingsymbol']} "
            f"segment={v['segment']}"
        )

    if unresolved:
        print(
            "\nUNRESOLVED:",
            ", ".join(unresolved)
        )

    return resolved


def fetch_history(
    kite,
    token,
    start_date,
    end_date
):
    start = datetime.strptime(
        start_date,
        "%Y-%m-%d"
    ).replace(
        hour=9,
        minute=15,
        tzinfo=IST
    )

    end = datetime.strptime(
        end_date,
        "%Y-%m-%d"
    ).replace(
        hour=15,
        minute=30,
        tzinfo=IST
    )

    # Keep chunks small to avoid broker historical limits.
    chunk_days = 20

    all_rows = []

    cur = start

    while cur <= end:

        chunk_end = min(
            cur + timedelta(days=chunk_days),
            end
        )

        print(
            f"  fetch "
            f"{cur:%Y-%m-%d} -> "
            f"{chunk_end:%Y-%m-%d}"
        )

        try:
            rows = kite.historical_data(
                token,
                cur,
                chunk_end,
                "3minute",
                continuous=False,
                oi=False,
            )
        except Exception as e:
            print("  ERROR:", e)
            rows = []

        if rows:
            all_rows.extend(rows)

        cur = (
            chunk_end
            + timedelta(seconds=1)
        )

        time.sleep(0.4)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)

    if "date" in df.columns:
        df = df.rename(
            columns={"date": "timestamp"}
        )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"]
    )

    df = (
        df.drop_duplicates(
            subset=["timestamp"]
        )
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    return df


# --------------------------------------------------
# MAIN
# --------------------------------------------------

kite = get_kite_client()

resolved = resolve_indexes(kite)

summary = []

for i, (save_name, meta) in enumerate(
    resolved.items(),
    1
):
    print(
        f"\n[{i}/{len(resolved)}] "
        f"{save_name}"
    )

    df = fetch_history(
        kite,
        meta["instrument_token"],
        START_DATE,
        END_DATE,
    )

    if df.empty:
        print("  NO CANDLES")

        summary.append({
            "index": save_name,
            "status": "NO_CANDLES",
            "rows": 0,
            **meta,
        })
        continue

    df["symbol"] = save_name
    df["instrument_token"] = (
        meta["instrument_token"]
    )

    cols = [
        "timestamp",
        "symbol",
        "instrument_token",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]

    for c in cols:
        if c not in df.columns:
            df[c] = 0

    df = df[cols]

    p = OUT / f"{save_name}.parquet"

    df.to_parquet(
        p,
        index=False
    )

    print(
        "  rows:",
        len(df),
        "|",
        df["timestamp"].min(),
        "->",
        df["timestamp"].max()
    )

    summary.append({
        "index": save_name,
        "status": "OK",
        "rows": len(df),
        "start":
            df["timestamp"].min(),
        "end":
            df["timestamp"].max(),
        **meta,
    })

pd.DataFrame(summary).to_csv(
    OUT / "index_download_summary.csv",
    index=False
)

print(
    "\n===== DOWNLOAD SUMMARY ====="
)

print(
    pd.DataFrame(summary).to_string(
        index=False
    )
)

print(
    "\nWrote index candles to:",
    OUT
)
