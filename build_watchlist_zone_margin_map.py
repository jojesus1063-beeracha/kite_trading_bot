import time
from pathlib import Path

import pandas as pd

from auth import get_kite_client

TRADE_FILE = Path(
    "runtime/watchlist_missed_opportunity/"
    "momentum_rvol_matrix/trade_level.csv"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "real_money_zone_comparison/"
    "margin_per_share.csv"
)

OUT.parent.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(TRADE_FILE)

# We only need stocks belonging to the broad candidate zone:
# Momentum 1.00-1.50%
# RVOL 1.50-3.00
zone = df[
    (pd.to_numeric(df["momentum_pct"], errors="coerce") >= 1.00) &
    (pd.to_numeric(df["momentum_pct"], errors="coerce") < 1.50) &
    (pd.to_numeric(df["relative_volume"], errors="coerce") >= 1.50) &
    (pd.to_numeric(df["relative_volume"], errors="coerce") < 3.00)
].copy()

symbols = sorted(
    zone["symbol"].astype(str).unique()
)

print("Zone trade rows :", len(zone))
print("Unique symbols  :", len(symbols))

kite = get_kite_client()

results = []

for i, symbol in enumerate(symbols, 1):

    order = {
        "exchange": "NSE",
        "tradingsymbol": symbol,
        "transaction_type": "BUY",
        "variety": "regular",
        "product": "MIS",
        "order_type": "MARKET",
        "quantity": 1,
    }

    try:
        x = kite.order_margins([order])[0]

        # Kite normally returns total margin under "total".
        total = x.get("total")

        if total is None:
            # Defensive fallback.
            total = (
                x.get("span", 0)
                + x.get("exposure", 0)
                + x.get("option_premium", 0)
                + x.get("additional", 0)
            )

        margin_per_share = float(total)

        results.append({
            "symbol": symbol,
            "margin_per_share": margin_per_share,
            "status": "OK",
        })

        print(
            f"[{i}/{len(symbols)}] "
            f"{symbol}: "
            f"margin/share=Rs {margin_per_share:.2f}"
        )

    except Exception as e:

        results.append({
            "symbol": symbol,
            "margin_per_share": None,
            "status": "ERROR",
            "error": str(e),
        })

        print(
            f"[{i}/{len(symbols)}] "
            f"{symbol}: ERROR {e}"
        )

    time.sleep(0.25)

r = pd.DataFrame(results)

r.to_csv(OUT, index=False)

print("\n===== MARGIN COVERAGE =====")

print(r["status"].value_counts().to_string())

ok = r[r["status"] == "OK"]

print("\nSuccessful :", len(ok))
print("Failed     :", len(r) - len(ok))

if not ok.empty:
    print(
        "Median MIS margin/share : "
        f"Rs {ok['margin_per_share'].median():,.2f}"
    )

print("\nWrote:", OUT)
