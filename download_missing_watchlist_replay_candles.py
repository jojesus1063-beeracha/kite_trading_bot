import time
from pathlib import Path

import pandas as pd

from auth import get_kite_client

REPLAY = Path(
    "runtime/watchlist_missed_opportunity/replay/"
    "all_rejected_replay.csv"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "rejected_candles_3minute"
)
OUT.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(REPLAY)

missing = df[
    df["status"].isin(
        ["NO_CANDLES", "NO_DAY_CANDLES"]
    )
][["date", "symbol"]].drop_duplicates()

print("Missing symbol/date pairs:", len(missing))
print("Unique missing symbols:", missing["symbol"].nunique())

kite = get_kite_client()

print("Loading NSE instrument master...")
inst = pd.DataFrame(kite.instruments("NSE"))

token_map = {}

for _, r in inst.iterrows():
    symbol = str(r.get("tradingsymbol", "")).strip()
    token = r.get("instrument_token")

    if symbol and pd.notna(token):
        token_map[symbol] = int(token)

print("Instrument symbols:", len(token_map))

report = []

for n, (_, row) in enumerate(missing.iterrows(), 1):
    symbol = str(row["symbol"])
    date = str(row["date"])

    token = token_map.get(symbol)

    if token is None:
        report.append({
            "date": date,
            "symbol": symbol,
            "status": "TOKEN_NOT_FOUND",
            "rows": 0,
        })
        print(
            f"[{n}/{len(missing)}] "
            f"{date} {symbol}: TOKEN_NOT_FOUND"
        )
        continue

    day = pd.Timestamp(date)

    start = day.strftime("%Y-%m-%d 09:15:00")
    end = day.strftime("%Y-%m-%d 15:30:00")

    try:
        candles = kite.historical_data(
            token,
            start,
            end,
            "3minute",
            continuous=False,
            oi=False,
        )
    except Exception as e:
        report.append({
            "date": date,
            "symbol": symbol,
            "status": "ERROR",
            "rows": 0,
            "error": str(e),
        })

        print(
            f"[{n}/{len(missing)}] "
            f"{date} {symbol}: ERROR {e}"
        )

        time.sleep(0.40)
        continue

    c = pd.DataFrame(candles)

    if c.empty:
        report.append({
            "date": date,
            "symbol": symbol,
            "status": "EMPTY",
            "rows": 0,
        })

        print(
            f"[{n}/{len(missing)}] "
            f"{date} {symbol}: EMPTY"
        )

        time.sleep(0.40)
        continue

    c = c.rename(columns={"date": "timestamp"})

    p = OUT / f"{date}_{symbol}.parquet"

    c.to_parquet(
        p,
        index=False
    )

    report.append({
        "date": date,
        "symbol": symbol,
        "status": "OK",
        "rows": len(c),
        "file": str(p),
    })

    print(
        f"[{n}/{len(missing)}] "
        f"{date} {symbol}: OK rows={len(c)}"
    )

    time.sleep(0.40)

pd.DataFrame(report).to_csv(
    "runtime/watchlist_missed_opportunity/"
    "missing_candle_download_report.csv",
    index=False
)

print("\n===== SUMMARY =====")

r = pd.DataFrame(report)

print(
    r["status"]
    .value_counts(dropna=False)
    .to_string()
)

print(
    "\nDownloaded rows:",
    int(r.loc[r["status"] == "OK", "rows"].sum())
)

print("Wrote:", OUT)
