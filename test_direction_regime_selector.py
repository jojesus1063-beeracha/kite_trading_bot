import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

CAPITAL = 5000.0
AVAILABLE_MARGIN = 5000.0

RPT_PCT = 0.20
SL_PCT = 0.004
T1_PCT = 0.005
T2_PCT = 0.010

MAX_POSITION_PCT = 25.0

SRC = Path(
    "runtime/proposed_logic_broker_sl_replay/"
    "trade_level.csv"
)

CANDLE_DIR = Path(
    "runtime/trade_replay_history/"
    "candles_3minute"
)

MARGIN_FILES = [
    Path(
        "runtime/watchlist_missed_opportunity/"
        "top120_ranked_watchlist/"
        "margin_per_share.csv"
    ),
    Path(
        "runtime/watchlist_missed_opportunity/"
        "real_money_zone_comparison/"
        "margin_per_share.csv"
    ),
    Path(
        "runtime/sl_rpt_max_position_sweep/"
        "current_margin_per_share.csv"
    ),
]

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "direction_regime_test"
)
OUT.mkdir(parents=True, exist_ok=True)

ADX_LEVELS = [15, 20, 25, 30, 35, 40]

BUY_CLV_LEVELS = [
    0.55, 0.60, 0.65, 0.70, 0.75
]

VOLUME_LEVELS = [
    0.8, 1.0, 1.2, 1.5, 2.0
]

MODES = [
    "PASS_NORMAL_FAIL_SKIP",
    "PASS_NORMAL_FAIL_REVERSE",
    "FAIL_REVERSE_ONLY",
]

# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def ema(s, n):
    return s.ewm(
        span=n,
        adjust=False
    ).mean()


def true_range(df):
    prev = df["close"].shift(1)

    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev).abs(),
            (df["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)


def calc_atr(df, n=14):
    return true_range(df).ewm(
        alpha=1/n,
        adjust=False
    ).mean()


def calc_adx_di(df, n=14):
    high = df["high"]
    low = df["low"]

    up = high.diff()
    down = -low.diff()

    plus_dm = pd.Series(
        np.where(
            (up > down) & (up > 0),
            up,
            0.0,
        ),
        index=df.index,
    )

    minus_dm = pd.Series(
        np.where(
            (down > up) & (down > 0),
            down,
            0.0,
        ),
        index=df.index,
    )

    tr = true_range(df)

    atr = tr.ewm(
        alpha=1/n,
        adjust=False
    ).mean()

    plus_di = (
        100
        * plus_dm.ewm(
            alpha=1/n,
            adjust=False
        ).mean()
        / atr.replace(0, np.nan)
    )

    minus_di = (
        100
        * minus_dm.ewm(
            alpha=1/n,
            adjust=False
        ).mean()
        / atr.replace(0, np.nan)
    )

    dx = (
        100
        * (plus_di - minus_di).abs()
        / (plus_di + minus_di).replace(
            0,
            np.nan
        )
    )

    adx = dx.ewm(
        alpha=1/n,
        adjust=False
    ).mean()

    return adx, plus_di, minus_di


def prepare_day(df):
    df = df.copy()

    df["timestamp"] = pd.to_datetime(
        df["timestamp"]
    )

    df = (
        df.sort_values("timestamp")
        .reset_index(drop=True)
    )

    df["ema9"] = ema(
        df["close"],
        9
    )

    df["ema21"] = ema(
        df["close"],
        21
    )

    df["atr14"] = calc_atr(
        df,
        14
    )

    adx, plus_di, minus_di = (
        calc_adx_di(df, 14)
    )

    df["adx14_rebuilt"] = adx
    df["plus_di14"] = plus_di
    df["minus_di14"] = minus_di

    rng = (
        df["high"]
        -
        df["low"]
    )

    # 0..1 CLV
    df["clv01"] = (
        (
            df["close"]
            -
            df["low"]
        )
        /
        rng.replace(
            0,
            np.nan
        )
    )

    # Volume ratio uses PRIOR candles only
    prior_avg_vol = (
        df["volume"]
        .shift(1)
        .rolling(20)
        .mean()
    )

    df["volume_ratio20_rebuilt"] = (
        df["volume"]
        /
        prior_avg_vol.replace(
            0,
            np.nan
        )
    )

    return df


def load_day(symbol, date):
    p = (
        CANDLE_DIR
        /
        f"{symbol}.parquet"
    )

    if not p.exists():
        return None

    df = pd.read_parquet(p)

    df["timestamp"] = pd.to_datetime(
        df["timestamp"]
    )

    target_date = (
        pd.Timestamp(date).date()
    )

    day = df[
        df["timestamp"].dt.date
        ==
        target_date
    ].copy()

    if day.empty:
        return None

    return prepare_day(day)


def find_signal_row(
    day,
    signal_ts,
):
    ts = pd.Timestamp(signal_ts)

    candle_tz = (
        day["timestamp"].dt.tz
    )

    if candle_tz is not None:
        if ts.tzinfo is None:
            ts = ts.tz_localize(
                "Asia/Kolkata"
            )
        else:
            ts = ts.tz_convert(
                candle_tz
            )

    exact = day[
        day["timestamp"] == ts
    ]

    if not exact.empty:
        return exact.index[0]

    # nearest prior completed candle
    prior = day[
        day["timestamp"] <= ts
    ]

    if prior.empty:
        return None

    return prior.index[-1]


def estimate_cost(
    entry,
    exit_price,
    qty,
):
    turnover = (
        entry + exit_price
    ) * qty

    brokerage = (
        min(
            20.0,
            entry
            * qty
            * 0.0003
        )
        +
        min(
            20.0,
            exit_price
            * qty
            * 0.0003
        )
    )

    exchange = (
        turnover
        * 0.0000345
    )

    sebi = (
        turnover
        * 0.000001
    )

    stamp = (
        entry
        * qty
        * 0.00003
    )

    stt = (
        exit_price
        * qty
        * 0.00025
    )

    gst = (
        brokerage
        + exchange
        + sebi
    ) * 0.18

    return (
        brokerage
        + exchange
        + sebi
        + stamp
        + stt
        + gst
    )


def simulate(
    day,
    side,
    entry,
    entry_ts,
    qty,
):
    side = side.upper()

    if side == "BUY":
        stop = entry * (
            1 - SL_PCT
        )
        t1 = entry * (
            1 + T1_PCT
        )
        t2 = entry * (
            1 + T2_PCT
        )

    else:
        stop = entry * (
            1 + SL_PCT
        )
        t1 = entry * (
            1 - T1_PCT
        )
        t2 = entry * (
            1 - T2_PCT
        )

    q1 = qty // 2
    q2 = qty - q1

    if q1 == 0:
        q1 = 1
        q2 = 0

    after = day[
        day["timestamp"]
        >=
        entry_ts
    ].copy()

    if after.empty:
        return None

    gross = 0.0
    costs = 0.0
    t1_hit = False

    for _, r in after.iterrows():
        hi = float(r["high"])
        lo = float(r["low"])
        ts = r["timestamp"]

        if not t1_hit:

            if side == "BUY":
                sl_hit = (
                    lo <= stop
                )
                t1_now = (
                    hi >= t1
                )
            else:
                sl_hit = (
                    hi >= stop
                )
                t1_now = (
                    lo <= t1
                )

            # Conservative
            if sl_hit:
                gross = (
                    (stop-entry)
                    * qty
                    if side == "BUY"
                    else
                    (entry-stop)
                    * qty
                )

                costs = estimate_cost(
                    entry,
                    stop,
                    qty
                )

                return {
                    "exit":
                        "SL_0.4",

                    "exit_time":
                        ts,

                    "gross":
                        gross,

                    "costs":
                        costs,

                    "net":
                        gross-costs,
                }

            if t1_now:
                t1_hit = True

                gross += (
                    (t1-entry)
                    * q1
                    if side == "BUY"
                    else
                    (entry-t1)
                    * q1
                )

                costs += estimate_cost(
                    entry,
                    t1,
                    q1
                )

                if q2 == 0:
                    return {
                        "exit":
                            "T1_ONLY",

                        "exit_time":
                            ts,

                        "gross":
                            gross,

                        "costs":
                            costs,

                        "net":
                            gross-costs,
                    }

        else:

            if side == "BUY":
                be_hit = (
                    lo <= entry
                )
                t2_hit = (
                    hi >= t2
                )
            else:
                be_hit = (
                    hi >= entry
                )
                t2_hit = (
                    lo <= t2
                )

            if be_hit:
                if q2 > 0:
                    costs += (
                        estimate_cost(
                            entry,
                            entry,
                            q2
                        )
                    )

                return {
                    "exit":
                        "T1_PLUS_BE",

                    "exit_time":
                        ts,

                    "gross":
                        gross,

                    "costs":
                        costs,

                    "net":
                        gross-costs,
                }

            if t2_hit:
                if q2 > 0:
                    gross += (
                        (t2-entry)
                        * q2
                        if side == "BUY"
                        else
                        (entry-t2)
                        * q2
                    )

                    costs += (
                        estimate_cost(
                            entry,
                            t2,
                            q2
                        )
                    )

                return {
                    "exit":
                        "T1_PLUS_T2",

                    "exit_time":
                        ts,

                    "gross":
                        gross,

                    "costs":
                        costs,

                    "net":
                        gross-costs,
                }

    last = after.iloc[-1]
    eod = float(
        last["close"]
    )

    if not t1_hit:
        gross = (
            (eod-entry)
            * qty
            if side == "BUY"
            else
            (entry-eod)
            * qty
        )

        costs = estimate_cost(
            entry,
            eod,
            qty
        )

        return {
            "exit":
                "EOD_NO_T1",

            "exit_time":
                last["timestamp"],

            "gross":
                gross,

            "costs":
                costs,

            "net":
                gross-costs,
        }

    if q2 > 0:
        gross += (
            (eod-entry)
            * q2
            if side == "BUY"
            else
            (entry-eod)
            * q2
        )

        costs += estimate_cost(
            entry,
            eod,
            q2
        )

    return {
        "exit":
            "T1_PLUS_EOD",

        "exit_time":
            last["timestamp"],

        "gross":
            gross,

        "costs":
            costs,

        "net":
            gross-costs,
    }


def load_margin_map():
    out = {}

    for p in MARGIN_FILES:
        if not p.exists():
            continue

        try:
            m = pd.read_csv(p)
        except Exception:
            continue

        if (
            "symbol"
            not in m.columns
            or
            "margin_per_share"
            not in m.columns
        ):
            continue

        for _, r in m.iterrows():
            try:
                value = float(
                    r["margin_per_share"]
                )
            except Exception:
                continue

            if value > 0:
                out[
                    str(r["symbol"])
                ] = value

    return out


def size_position(
    entry,
    symbol,
    margin_map,
):
    risk_rupees = (
        CAPITAL
        * RPT_PCT
        / 100.0
    )

    per_share_risk = (
        entry
        * SL_PCT
    )

    if per_share_risk <= 0:
        return 0

    qty_risk = int(
        risk_rupees
        /
        per_share_risk
    )

    mps = margin_map.get(
        symbol
    )

    if (
        mps is None
        or not math.isfinite(mps)
        or mps <= 0
    ):
        # Fallback:
        # use historical replay qty.
        return None

    margin_budget = (
        AVAILABLE_MARGIN
        *
        MAX_POSITION_PCT
        /
        100.0
    )

    qty_margin = int(
        margin_budget
        /
        mps
    )

    return min(
        qty_risk,
        qty_margin
    )


def reverse_side(side):
    return (
        "SELL"
        if side == "BUY"
        else "BUY"
    )


def calc_drawdown(pnls):
    eq = 0.0
    peak = 0.0
    dd = 0.0

    for p in pnls:
        eq += p
        peak = max(
            peak,
            eq
        )
        dd = min(
            dd,
            eq-peak
        )

    return dd


def metrics(df):
    if df.empty:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0,
            "gross_profit": 0,
            "gross_loss": 0,
            "gross": 0,
            "costs": 0,
            "net": 0,
            "avg_net": 0,
            "avg_winner": 0,
            "avg_loser": 0,
            "profit_factor": np.nan,
            "max_drawdown": 0,
            "largest_win": 0,
            "largest_loss": 0,
        }

    wins = df[
        df["net"] > 0
    ]

    losses = df[
        df["net"] <= 0
    ]

    gp = float(
        wins["gross"].sum()
    )

    gl = float(
        losses["gross"].sum()
    )

    pf = (
        gp
        /
        abs(gl)
        if gl < 0
        else np.inf
    )

    return {
        "trades":
            len(df),

        "wins":
            len(wins),

        "losses":
            len(losses),

        "win_rate_pct":
            len(wins)
            / len(df)
            * 100,

        "gross_profit":
            gp,

        "gross_loss":
            gl,

        "gross":
            float(
                df["gross"].sum()
            ),

        "costs":
            float(
                df["costs"].sum()
            ),

        "net":
            float(
                df["net"].sum()
            ),

        "avg_net":
            float(
                df["net"].mean()
            ),

        "avg_winner":
            float(
                wins["net"].mean()
            )
            if not wins.empty
            else 0,

        "avg_loser":
            float(
                losses["net"].mean()
            )
            if not losses.empty
            else 0,

        "profit_factor":
            pf,

        "max_drawdown":
            calc_drawdown(
                df["net"].tolist()
            ),

        "largest_win":
            float(
                df["net"].max()
            ),

        "largest_loss":
            float(
                df["net"].min()
            ),
    }


# --------------------------------------------------
# LOAD SOURCE
# --------------------------------------------------

df = pd.read_csv(SRC)

if "status" in df.columns:
    df = df[
        df["status"]
        .astype(str)
        .str.upper()
        == "OK"
    ].copy()

df["signal_ts"] = pd.to_datetime(
    df["signal_ts"]
)

df["direction"] = (
    df["direction"]
    .astype(str)
    .str.upper()
)

df["entry"] = pd.to_numeric(
    df["entry"],
    errors="coerce"
)

df["qty"] = pd.to_numeric(
    df["qty"],
    errors="coerce"
)

df = df.dropna(
    subset=[
        "entry",
        "signal_ts"
    ]
)

print(
    "===== SOURCE ====="
)

print(
    "Replay candidates:",
    len(df)
)

print(
    "Dates:",
    df["date"].nunique()
)

print(
    "Symbols:",
    df["symbol"].nunique()
)

margin_map = load_margin_map()

# --------------------------------------------------
# REBUILD ENTRY FEATURES ONCE
# --------------------------------------------------

feature_rows = []
unreplayable = []

for n, (_, t) in enumerate(
    df.iterrows(),
    1
):
    symbol = str(
        t["symbol"]
    )

    date = str(
        t["date"]
    )

    day = load_day(
        symbol,
        date
    )

    if day is None:
        unreplayable.append({
            "symbol": symbol,
            "date": date,
            "reason": "NO_CANDLES",
        })
        continue

    i = find_signal_row(
        day,
        t["signal_ts"]
    )

    if i is None:
        unreplayable.append({
            "symbol": symbol,
            "date": date,
            "reason":
                "NO_SIGNAL_CANDLE",
        })
        continue

    r = day.iloc[i]

    entry = float(
        t["entry"]
    )

    qty = size_position(
        entry,
        symbol,
        margin_map
    )

    if qty is None:
        qty = int(
            t["qty"]
        )

    if qty <= 0:
        unreplayable.append({
            "symbol": symbol,
            "date": date,
            "reason": "ZERO_QTY",
        })
        continue

    feature_rows.append({
        "trade_index":
            t.get(
                "trade_index"
            ),

        "date":
            date,

        "symbol":
            symbol,

        "signal_ts":
            r["timestamp"],

        "direction":
            t["direction"],

        "entry":
            entry,

        "qty":
            qty,

        "adx":
            float(
                r["adx14_rebuilt"]
            )
            if pd.notna(
                r["adx14_rebuilt"]
            )
            else np.nan,

        "plus_di":
            float(
                r["plus_di14"]
            )
            if pd.notna(
                r["plus_di14"]
            )
            else np.nan,

        "minus_di":
            float(
                r["minus_di14"]
            )
            if pd.notna(
                r["minus_di14"]
            )
            else np.nan,

        "clv01":
            float(
                r["clv01"]
            )
            if pd.notna(
                r["clv01"]
            )
            else np.nan,

        "volume_ratio":
            float(
                r[
                    "volume_ratio20_rebuilt"
                ]
            )
            if pd.notna(
                r[
                    "volume_ratio20_rebuilt"
                ]
            )
            else np.nan,

        "breakout":
            t.get(
                "breakout"
            ),

        "pullback":
            t.get(
                "pullback"
            ),

        "historical_sim_net":
            t.get(
                "sim_net"
            ),
    })

    if n % 25 == 0:
        print(
            f"Features {n}/{len(df)}"
        )

f = pd.DataFrame(
    feature_rows
)

f = f.dropna(
    subset=[
        "adx",
        "clv01",
        "volume_ratio",
    ]
).copy()

print(
    "\nFeature-replayable:",
    len(f)
)

print(
    "Unreplayable:",
    len(unreplayable)
)

# --------------------------------------------------
# PRE-SIM NORMAL + REVERSE ONCE
# --------------------------------------------------

base_results = []

for n, (_, t) in enumerate(
    f.iterrows(),
    1
):
    day = load_day(
        t["symbol"],
        t["date"]
    )

    normal = simulate(
        day,
        t["direction"],
        float(t["entry"]),
        pd.Timestamp(
            t["signal_ts"]
        ),
        int(t["qty"]),
    )

    rev_side = reverse_side(
        t["direction"]
    )

    reversed_sim = simulate(
        day,
        rev_side,
        float(t["entry"]),
        pd.Timestamp(
            t["signal_ts"]
        ),
        int(t["qty"]),
    )

    if (
        normal is None
        or reversed_sim is None
    ):
        continue

    row = t.to_dict()

    for k, v in normal.items():
        row[
            f"normal_{k}"
        ] = v

    for k, v in reversed_sim.items():
        row[
            f"reverse_{k}"
        ] = v

    row[
        "reverse_direction"
    ] = rev_side

    base_results.append(row)

    if n % 25 == 0:
        print(
            f"Simulated {n}/{len(f)}"
        )

base = pd.DataFrame(
    base_results
)

base.to_csv(
    OUT / "base_normal_reverse.csv",
    index=False
)

# --------------------------------------------------
# BASELINE CURRENT
# --------------------------------------------------

baseline = pd.DataFrame({
    "date":
        base["date"],

    "symbol":
        base["symbol"],

    "signal_ts":
        base["signal_ts"],

    "executed_direction":
        base["direction"],

    "normal_or_reverse":
        "NORMAL",

    "gross":
        base["normal_gross"],

    "costs":
        base["normal_costs"],

    "net":
        base["normal_net"],
})

baseline_metrics = metrics(
    baseline
)

# --------------------------------------------------
# SWEEP
# --------------------------------------------------

summary_rows = []
trade_rows = []
day_rows = []

for use_di in [
    False,
    True
]:

    for adx_min in ADX_LEVELS:

        for buy_clv_min in BUY_CLV_LEVELS:

            sell_clv_max = (
                1.0
                -
                buy_clv_min
            )

            for volume_min in VOLUME_LEVELS:

                normal_direction_ok = (
                    (
                        (base["direction"] == "BUY")
                        &
                        (
                            base["clv01"]
                            >=
                            buy_clv_min
                        )
                    )
                    |
                    (
                        (base["direction"] == "SELL")
                        &
                        (
                            base["clv01"]
                            <=
                            sell_clv_max
                        )
                    )
                )

                quality = (
                    (base["adx"] >= adx_min)
                    &
                    (
                        base["volume_ratio"]
                        >=
                        volume_min
                    )
                    &
                    normal_direction_ok
                )

                if use_di:
                    di_ok = (
                        (
                            (base["direction"] == "BUY")
                            &
                            (
                                base["plus_di"]
                                >
                                base["minus_di"]
                            )
                        )
                        |
                        (
                            (base["direction"] == "SELL")
                            &
                            (
                                base["minus_di"]
                                >
                                base["plus_di"]
                            )
                        )
                    )

                    quality = (
                        quality
                        &
                        di_ok
                    )

                for mode in MODES:

                    rows = []

                    for idx, t in base.iterrows():
                        passed = bool(
                            quality.loc[idx]
                        )

                        execute = None

                        if (
                            mode
                            ==
                            "PASS_NORMAL_FAIL_SKIP"
                        ):
                            if passed:
                                execute = "NORMAL"

                        elif (
                            mode
                            ==
                            "PASS_NORMAL_FAIL_REVERSE"
                        ):
                            execute = (
                                "NORMAL"
                                if passed
                                else
                                "REVERSE"
                            )

                        elif (
                            mode
                            ==
                            "FAIL_REVERSE_ONLY"
                        ):
                            if not passed:
                                execute = "REVERSE"

                        if execute is None:
                            continue

                        if execute == "NORMAL":
                            direction = (
                                t["direction"]
                            )

                            gross = float(
                                t[
                                    "normal_gross"
                                ]
                            )

                            costs = float(
                                t[
                                    "normal_costs"
                                ]
                            )

                            net = float(
                                t[
                                    "normal_net"
                                ]
                            )

                            exit_name = t[
                                "normal_exit"
                            ]

                        else:
                            direction = t[
                                "reverse_direction"
                            ]

                            gross = float(
                                t[
                                    "reverse_gross"
                                ]
                            )

                            costs = float(
                                t[
                                    "reverse_costs"
                                ]
                            )

                            net = float(
                                t[
                                    "reverse_net"
                                ]
                            )

                            exit_name = t[
                                "reverse_exit"
                            ]

                        rows.append({
                            "date":
                                t["date"],

                            "symbol":
                                t["symbol"],

                            "signal_ts":
                                t["signal_ts"],

                            "original_direction":
                                t["direction"],

                            "executed_direction":
                                direction,

                            "normal_or_reverse":
                                execute,

                            "filter_pass":
                                passed,

                            "adx":
                                t["adx"],

                            "plus_di":
                                t["plus_di"],

                            "minus_di":
                                t["minus_di"],

                            "clv01":
                                t["clv01"],

                            "volume_ratio":
                                t["volume_ratio"],

                            "qty":
                                t["qty"],

                            "entry":
                                t["entry"],

                            "exit":
                                exit_name,

                            "gross":
                                gross,

                            "costs":
                                costs,

                            "net":
                                net,

                            "mode":
                                mode,

                            "use_di":
                                use_di,

                            "adx_min":
                                adx_min,

                            "buy_clv_min":
                                buy_clv_min,

                            "sell_clv_max":
                                sell_clv_max,

                            "volume_min":
                                volume_min,
                        })

                    r = pd.DataFrame(
                        rows
                    )

                    m = metrics(r)

                    normal_r = (
                        r[
                            r[
                                "normal_or_reverse"
                            ]
                            ==
                            "NORMAL"
                        ]
                        if not r.empty
                        else pd.DataFrame()
                    )

                    reverse_r = (
                        r[
                            r[
                                "normal_or_reverse"
                            ]
                            ==
                            "REVERSE"
                        ]
                        if not r.empty
                        else pd.DataFrame()
                    )

                    mn = metrics(
                        normal_r
                    )

                    mr = metrics(
                        reverse_r
                    )

                    profitable_days = 0
                    losing_days = 0

                    if not r.empty:
                        day_net = (
                            r.groupby(
                                "date"
                            )["net"]
                            .sum()
                        )

                        profitable_days = int(
                            (
                                day_net > 0
                            ).sum()
                        )

                        losing_days = int(
                            (
                                day_net < 0
                            ).sum()
                        )

                    summary_rows.append({
                        "mode":
                            mode,

                        "use_di":
                            use_di,

                        "adx_min":
                            adx_min,

                        "buy_clv_min":
                            buy_clv_min,

                        "sell_clv_max":
                            sell_clv_max,

                        "volume_min":
                            volume_min,

                        **m,

                        "normal_trades":
                            mn["trades"],

                        "normal_wins":
                            mn["wins"],

                        "normal_losses":
                            mn["losses"],

                        "normal_net":
                            mn["net"],

                        "reverse_trades":
                            mr["trades"],

                        "reverse_wins":
                            mr["wins"],

                        "reverse_losses":
                            mr["losses"],

                        "reverse_net":
                            mr["net"],

                        "profitable_days":
                            profitable_days,

                        "losing_days":
                            losing_days,
                    })

                    if not r.empty:
                        trade_rows.append(
                            r
                        )

summary = pd.DataFrame(
    summary_rows
)

# --------------------------------------------------
# ROBUSTNESS SCORE
# --------------------------------------------------

summary["robustness_score"] = (
    summary["net"]
    +
    summary[
        "profit_factor"
    ].replace(
        np.inf,
        5
    ).fillna(0)
    * 25
    +
    summary[
        "win_rate_pct"
    ]
    * 0.25
    +
    summary[
        "profitable_days"
    ]
    * 10
    -
    summary[
        "losing_days"
    ]
    * 5
    +
    summary[
        "max_drawdown"
    ]
    * 0.50
)

# Penalize tiny samples
summary.loc[
    summary["trades"] < 20,
    "robustness_score"
] -= 100

summary = summary.sort_values(
    "robustness_score",
    ascending=False
)

summary.to_csv(
    OUT / "threshold_sweep.csv",
    index=False
)

if trade_rows:
    all_trades = pd.concat(
        trade_rows,
        ignore_index=True
    )

    all_trades.to_csv(
        OUT / "trade_level_results.csv",
        index=False
    )

# --------------------------------------------------
# PRINT
# --------------------------------------------------

print(
    "\n===== CURRENT NORMAL BASELINE ====="
)

print(
    pd.DataFrame(
        [baseline_metrics]
    ).to_string(
        index=False,
        formatters={
            "win_rate_pct":
                lambda x:
                    f"{x:.1f}%",

            "gross":
                lambda x:
                    f"Rs {x:,.2f}",

            "costs":
                lambda x:
                    f"Rs {x:,.2f}",

            "net":
                lambda x:
                    f"Rs {x:,.2f}",

            "avg_net":
                lambda x:
                    f"Rs {x:,.2f}",

            "avg_winner":
                lambda x:
                    f"Rs {x:,.2f}",

            "avg_loser":
                lambda x:
                    f"Rs {x:,.2f}",

            "max_drawdown":
                lambda x:
                    f"Rs {x:,.2f}",
        }
    )
)

print(
    "\n===== TOP 30 CONFIGURATIONS ====="
)

cols = [
    "mode",
    "use_di",
    "adx_min",
    "buy_clv_min",
    "volume_min",
    "trades",
    "wins",
    "losses",
    "win_rate_pct",
    "net",
    "profit_factor",
    "max_drawdown",
    "normal_trades",
    "normal_net",
    "reverse_trades",
    "reverse_net",
    "profitable_days",
    "losing_days",
    "robustness_score",
]

print(
    summary[
        cols
    ].head(30).to_string(
        index=False,
        formatters={
            "win_rate_pct":
                lambda x:
                    f"{x:.1f}%",

            "net":
                lambda x:
                    f"Rs {x:,.2f}",

            "normal_net":
                lambda x:
                    f"Rs {x:,.2f}",

            "reverse_net":
                lambda x:
                    f"Rs {x:,.2f}",

            "max_drawdown":
                lambda x:
                    f"Rs {x:,.2f}",

            "profit_factor":
                lambda x:
                    (
                        "INF"
                        if math.isinf(x)
                        else
                        f"{x:.2f}"
                        if pd.notna(x)
                        else
                        "NA"
                    ),

            "robustness_score":
                lambda x:
                    f"{x:.2f}",
        }
    )
)

print(
    "\n===== BEST BY EACH MODE ====="
)

best_by_mode = (
    summary
    .sort_values(
        "robustness_score",
        ascending=False
    )
    .groupby(
        ["mode", "use_di"],
        as_index=False
    )
    .first()
)

print(
    best_by_mode[
        cols
    ].to_string(
        index=False,
        formatters={
            "win_rate_pct":
                lambda x:
                    f"{x:.1f}%",

            "net":
                lambda x:
                    f"Rs {x:,.2f}",

            "normal_net":
                lambda x:
                    f"Rs {x:,.2f}",

            "reverse_net":
                lambda x:
                    f"Rs {x:,.2f}",

            "max_drawdown":
                lambda x:
                    f"Rs {x:,.2f}",

            "profit_factor":
                lambda x:
                    (
                        "INF"
                        if math.isinf(x)
                        else
                        f"{x:.2f}"
                        if pd.notna(x)
                        else
                        "NA"
                    ),
        }
    )
)

summary.head(50).to_csv(
    OUT / "best_configurations.csv",
    index=False
)

pd.DataFrame(
    unreplayable
).to_csv(
    OUT / "unreplayable.csv",
    index=False
)

print(
    "\n===== COVERAGE ====="
)

print(
    "Source candidates :",
    len(df)
)

print(
    "Feature replayable:",
    len(f)
)

print(
    "Normal/reverse simulated:",
    len(base)
)

print(
    "Unreplayable:",
    len(unreplayable)
)

print(
    "\nWrote:",
    OUT
)
