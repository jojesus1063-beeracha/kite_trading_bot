import math
from pathlib import Path

import pandas as pd


# ------------------------------------------------------------
# SETTINGS
# ------------------------------------------------------------

RPT_TO_TEST = 0.20

LOOKBACK = 20

# "Strong close" = close in top/bottom 35% of candle.
BUY_CLV_MIN = 0.65
SELL_CLV_MAX = 0.35

# Breakout close must exceed structure by at least this ATR fraction.
MIN_BREAK_ATR = 0.05

# Retest may come within this ATR distance of broken level.
RETEST_TOL_ATR = 0.15

RETEST_LOOKAHEAD = 3

SL_PCT = 0.004
T1_PCT = 0.005
T2_PCT = 0.010


TRADE_FILE = Path(
    "runtime/watchlist_missed_opportunity/"
    "top120_ranked_watchlist/"
    "rpt_sweep_trade_level.csv"
)

TOP120_CANDLES = Path(
    "runtime/watchlist_missed_opportunity/"
    "top120_ranked_watchlist/candles"
)

HISTORY_CANDLES = Path(
    "runtime/trade_replay_history/"
    "candles_3minute"
)

REJECTED_CANDLES = Path(
    "runtime/watchlist_missed_opportunity/"
    "rejected_candles_3minute"
)

OUT = Path(
    "runtime/watchlist_missed_opportunity/"
    "breakout_confirmation_test"
)

OUT.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------
# INDICATORS
# ------------------------------------------------------------

def ema(s, n):
    return s.ewm(
        span=n,
        adjust=False
    ).mean()


def atr(frame, n=14):

    prev = frame["close"].shift(1)

    tr = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - prev).abs(),
        (frame["low"] - prev).abs(),
    ], axis=1).max(axis=1)

    return tr.rolling(n).mean()


def prepare(c):

    c = c.copy()

    c["timestamp"] = pd.to_datetime(
        c["timestamp"]
    )

    c = (
        c.sort_values("timestamp")
        .reset_index(drop=True)
    )

    c["ema9"] = ema(
        c["close"],
        9
    )

    c["ema21"] = ema(
        c["close"],
        21
    )

    c["atr14"] = atr(
        c,
        14
    )

    rng = c["high"] - c["low"]

    c["clv01"] = (
        (c["close"] - c["low"])
        /
        rng.replace(0, float("nan"))
    )

    return c


# ------------------------------------------------------------
# LOAD CANDLES
# ------------------------------------------------------------

def load_day(symbol, date):

    # First use exact Top120 cached day.
    p = (
        TOP120_CANDLES
        /
        f"{date}_{symbol}.parquet"
    )

    if p.exists():
        return prepare(
            pd.read_parquet(p)
        )

    # Then exact rejected-candle download.
    p = (
        REJECTED_CANDLES
        /
        f"{date}_{symbol}.parquet"
    )

    if p.exists():
        return prepare(
            pd.read_parquet(p)
        )

    # Finally long history file.
    p = (
        HISTORY_CANDLES
        /
        f"{symbol}.parquet"
    )

    if not p.exists():
        return None

    c = pd.read_parquet(p)

    c["timestamp"] = pd.to_datetime(
        c["timestamp"]
    )

    d = pd.Timestamp(date).date()

    c = c[
        c["timestamp"].dt.date == d
    ].copy()

    if c.empty:
        return None

    return prepare(c)


# ------------------------------------------------------------
# COST MODEL
# Same approximation used in Top120 script.
# ------------------------------------------------------------

def estimate_cost(
    entry,
    exit_price,
    qty
):

    turnover = (
        entry + exit_price
    ) * qty

    brokerage = (
        min(
            20.0,
            entry * qty * 0.0003
        )
        +
        min(
            20.0,
            exit_price * qty * 0.0003
        )
    )

    exchange = (
        turnover * 0.0000345
    )

    sebi = (
        turnover * 0.000001
    )

    stamp = (
        entry * qty * 0.00003
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


# ------------------------------------------------------------
# TRUE EXIT SIMULATION
# ------------------------------------------------------------

def simulate(
    day,
    side,
    entry,
    entry_ts,
    qty
):

    if side == "BUY":

        stop = (
            entry
            * (1 - SL_PCT)
        )

        t1 = (
            entry
            * (1 + T1_PCT)
        )

        t2 = (
            entry
            * (1 + T2_PCT)
        )

    else:

        stop = (
            entry
            * (1 + SL_PCT)
        )

        t1 = (
            entry
            * (1 - T1_PCT)
        )

        t2 = (
            entry
            * (1 - T2_PCT)
        )

    q1 = qty // 2
    q2 = qty - q1

    if q1 == 0:
        q1 = 1
        q2 = 0

    gross = 0.0
    costs = 0.0

    t1_hit = False

    after = day[
        day["timestamp"] >= entry_ts
    ].copy()

    if after.empty:
        return None

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

            # Conservative:
            # if stop and target touched
            # inside same candle,
            # assume stop first.
            if sl_hit:

                gross = (
                    (stop - entry) * qty
                    if side == "BUY"
                    else
                    (entry - stop) * qty
                )

                costs = estimate_cost(
                    entry,
                    stop,
                    qty
                )

                return {
                    "exit": "SL_0.4",
                    "exit_time": ts,
                    "gross": gross,
                    "costs": costs,
                    "net": gross-costs
                }

            if t1_now:

                t1_hit = True

                gross += (
                    (t1-entry) * q1
                    if side == "BUY"
                    else
                    (entry-t1) * q1
                )

                costs += estimate_cost(
                    entry,
                    t1,
                    q1
                )

                if q2 == 0:

                    return {
                        "exit": "T1_ONLY",
                        "exit_time": ts,
                        "gross": gross,
                        "costs": costs,
                        "net": gross-costs
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

                    costs += estimate_cost(
                        entry,
                        entry,
                        q2
                    )

                return {
                    "exit": "T1_PLUS_BE",
                    "exit_time": ts,
                    "gross": gross,
                    "costs": costs,
                    "net": gross-costs
                }

            if t2_hit:

                if q2 > 0:

                    gross += (
                        (t2-entry) * q2
                        if side == "BUY"
                        else
                        (entry-t2) * q2
                    )

                    costs += estimate_cost(
                        entry,
                        t2,
                        q2
                    )

                return {
                    "exit": "T1_PLUS_T2",
                    "exit_time": ts,
                    "gross": gross,
                    "costs": costs,
                    "net": gross-costs
                }

    last = after.iloc[-1]

    eod = float(
        last["close"]
    )

    if not t1_hit:

        gross = (
            (eod-entry) * qty
            if side == "BUY"
            else
            (entry-eod) * qty
        )

        costs = estimate_cost(
            entry,
            eod,
            qty
        )

        return {
            "exit": "EOD_NO_T1",
            "exit_time":
                last["timestamp"],
            "gross": gross,
            "costs": costs,
            "net": gross-costs
        }

    if q2 > 0:

        gross += (
            (eod-entry) * q2
            if side == "BUY"
            else
            (entry-eod) * q2
        )

        costs += estimate_cost(
            entry,
            eod,
            q2
        )

    return {
        "exit": "T1_PLUS_EOD",
        "exit_time":
            last["timestamp"],
        "gross": gross,
        "costs": costs,
        "net": gross-costs
    }


# ------------------------------------------------------------
# STRUCTURE INFORMATION
# ------------------------------------------------------------

def structure_info(
    day,
    signal_ts,
    side
):

    ix = day.index[
        day["timestamp"] == signal_ts
    ]

    if len(ix) == 0:
        return None

    i = int(ix[0])

    if i < LOOKBACK:
        return None

    r = day.iloc[i]

    atr_now = float(
        r["atr14"]
    )

    if (
        not math.isfinite(atr_now)
        or atr_now <= 0
    ):
        return None

    prior = day.iloc[
        i-LOOKBACK:i
    ]

    resistance = float(
        prior["high"].max()
    )

    support = float(
        prior["low"].min()
    )

    candle_range = (
        float(r["high"])
        -
        float(r["low"])
    )

    if candle_range <= 0:
        return None

    clv = (
        float(r["close"])
        -
        float(r["low"])
    ) / candle_range

    if side == "BUY":

        level = resistance

        closed_outside = (
            float(r["close"])
            > level
        )

        break_atr = (
            float(r["close"])
            - level
        ) / atr_now

        strong_close = (
            clv >= BUY_CLV_MIN
        )

    else:

        level = support

        closed_outside = (
            float(r["close"])
            < level
        )

        break_atr = (
            level
            -
            float(r["close"])
        ) / atr_now

        strong_close = (
            clv <= SELL_CLV_MAX
        )

    structure_pass = (
        closed_outside
        and strong_close
        and break_atr >= MIN_BREAK_ATR
    )

    return {
        "i": i,
        "signal_row": r,
        "level": level,
        "resistance": resistance,
        "support": support,
        "atr": atr_now,
        "clv": clv,
        "break_atr": break_atr,
        "closed_outside":
            closed_outside,
        "strong_close":
            strong_close,
        "structure_pass":
            structure_pass,
    }


# ------------------------------------------------------------
# CONFIRMATION MODES
# ------------------------------------------------------------

def confirmation_entry(
    day,
    signal_ts,
    side,
    mode
):

    info = structure_info(
        day,
        signal_ts,
        side
    )

    if info is None:
        return None

    i = info["i"]
    level = info["level"]
    atr_now = info["atr"]

    signal_row = info[
        "signal_row"
    ]

    # CURRENT:
    # Original signal candle close.
    if mode == "CURRENT":

        return {
            "entry_ts":
                signal_row["timestamp"],

            "entry":
                float(
                    signal_row["close"]
                ),

            "level": level,

            "clv":
                info["clv"],

            "break_atr":
                info["break_atr"],

            "structure_pass":
                info["structure_pass"],

            "confirmation":
                "ORIGINAL"
        }

    # All proposed modes require
    # true structural breakout.
    if not info["structure_pass"]:
        return None

    if mode == "STRUCTURE_CLOSE":

        return {
            "entry_ts":
                signal_row["timestamp"],

            "entry":
                float(
                    signal_row["close"]
                ),

            "level": level,

            "clv":
                info["clv"],

            "break_atr":
                info["break_atr"],

            "structure_pass": True,

            "confirmation":
                "STRUCTURE_CLOSE"
        }

    # Need at least one candle later.
    if i + 1 >= len(day):
        return None

    nxt = day.iloc[i+1]

    if mode == "HOLD_1":

        if side == "BUY":

            hold = (
                float(nxt["close"])
                > level
            )

        else:

            hold = (
                float(nxt["close"])
                < level
            )

        if not hold:
            return None

        return {
            "entry_ts":
                nxt["timestamp"],

            "entry":
                float(
                    nxt["close"]
                ),

            "level": level,

            "clv":
                info["clv"],

            "break_atr":
                info["break_atr"],

            "structure_pass": True,

            "confirmation":
                "ONE_CANDLE_HOLD"
        }

    if mode == "RETEST_HOLD":

        end = min(
            len(day),
            i + 1 + RETEST_LOOKAHEAD
        )

        for j in range(
            i+1,
            end
        ):

            x = day.iloc[j]

            if side == "BUY":

                touched = (
                    float(x["low"])
                    <=
                    level
                    +
                    RETEST_TOL_ATR
                    * atr_now
                )

                held = (
                    float(x["close"])
                    > level
                )

                directional = (
                    float(x["close"])
                    >=
                    float(x["open"])
                )

            else:

                touched = (
                    float(x["high"])
                    >=
                    level
                    -
                    RETEST_TOL_ATR
                    * atr_now
                )

                held = (
                    float(x["close"])
                    < level
                )

                directional = (
                    float(x["close"])
                    <=
                    float(x["open"])
                )

            if (
                touched
                and held
                and directional
            ):

                return {
                    "entry_ts":
                        x["timestamp"],

                    "entry":
                        float(
                            x["close"]
                        ),

                    "level":
                        level,

                    "clv":
                        info["clv"],

                    "break_atr":
                        info["break_atr"],

                    "structure_pass":
                        True,

                    "confirmation":
                        "RETEST_HOLD"
                }

        return None

    raise ValueError(mode)


# ------------------------------------------------------------
# LOAD ORIGINAL 0.20% RPT TRADES
# ------------------------------------------------------------

trades = pd.read_csv(
    TRADE_FILE
)

trades["rpt_pct"] = pd.to_numeric(
    trades["rpt_pct"],
    errors="coerce"
)

base = trades[
    trades["rpt_pct"].round(6)
    ==
    RPT_TO_TEST
].copy()

if base.empty:

    raise SystemExit(
        "No 0.20% RPT trades found."
    )

base["signal_ts"] = pd.to_datetime(
    base["signal_ts"]
)

base["original_net"] = pd.to_numeric(
    base["net"],
    errors="coerce"
)

base["original_winner"] = (
    base["original_net"] > 0
)

print(
    "===== BASELINE 0.20% RPT ====="
)

print(
    "Trades :",
    len(base)
)

print(
    "Wins   :",
    int(
        base["original_winner"].sum()
    )
)

print(
    "Losses :",
    int(
        (~base["original_winner"]).sum()
    )
)

print(
    "Net    :",
    f"Rs {base['original_net'].sum():,.2f}"
)


# ------------------------------------------------------------
# TEST MODES
# ------------------------------------------------------------

MODES = [
    "CURRENT",
    "STRUCTURE_CLOSE",
    "HOLD_1",
    "RETEST_HOLD",
]

result_rows = []


for _, t in base.iterrows():

    symbol = str(
        t["symbol"]
    )

    date = str(
        t["date"]
    )

    side = str(
        t["direction"]
    ).upper()

    qty = int(
        t["qty"]
    )

    signal_ts = pd.Timestamp(
        t["signal_ts"]
    )

    day = load_day(
        symbol,
        date
    )

    if day is None:

        print(
            "NO CANDLES:",
            date,
            symbol
        )
        continue

    # Make timestamps comparable.
    candle_tz = (
        day["timestamp"].dt.tz
    )

    if candle_tz is not None:

        if signal_ts.tzinfo is None:

            signal_ts = (
                signal_ts.tz_localize(
                    "Asia/Kolkata"
                )
            )

        else:

            signal_ts = (
                signal_ts.tz_convert(
                    candle_tz
                )
            )

    for mode in MODES:

        conf = confirmation_entry(
            day,
            signal_ts,
            side,
            mode
        )

        if conf is None:

            result_rows.append({
                "date": date,
                "symbol": symbol,
                "direction": side,
                "mode": mode,

                "accepted": False,

                "original_net":
                    float(
                        t["original_net"]
                    ),

                "original_winner":
                    bool(
                        t["original_winner"]
                    ),

                "new_net": 0.0,

                "reason":
                    "CONFIRMATION_FAIL",
            })

            continue

        sim = simulate(
            day,
            side,
            conf["entry"],
            conf["entry_ts"],
            qty
        )

        if sim is None:
            continue

        result_rows.append({
            "date": date,
            "symbol": symbol,
            "direction": side,
            "mode": mode,

            "accepted": True,

            "qty": qty,

            "original_entry":
                float(
                    t["entry"]
                ),

            "new_entry":
                conf["entry"],

            "original_signal_ts":
                signal_ts,

            "new_entry_ts":
                conf["entry_ts"],

            "level":
                conf["level"],

            "clv":
                conf["clv"],

            "break_atr":
                conf["break_atr"],

            "confirmation":
                conf["confirmation"],

            "original_net":
                float(
                    t["original_net"]
                ),

            "original_winner":
                bool(
                    t["original_winner"]
                ),

            "new_exit":
                sim["exit"],

            "new_exit_time":
                sim["exit_time"],

            "gross":
                sim["gross"],

            "costs":
                sim["costs"],

            "new_net":
                sim["net"],

            "reason":
                "ACCEPTED",
        })


r = pd.DataFrame(
    result_rows
)

r.to_csv(
    OUT / "trade_level.csv",
    index=False
)


# ------------------------------------------------------------
# SUMMARY
# ------------------------------------------------------------

summary_rows = []

for mode in MODES:

    x = r[
        r["mode"] == mode
    ].copy()

    accepted = x[
        x["accepted"] == True
    ].copy()

    rejected = x[
        x["accepted"] == False
    ].copy()

    wins = int(
        (
            accepted["new_net"] > 0
        ).sum()
    )

    losses = int(
        (
            accepted["new_net"] <= 0
        ).sum()
    )

    old_losers_removed = int(
        (
            (~rejected["original_winner"])
        ).sum()
    )

    old_winners_lost = int(
        (
            rejected["original_winner"]
        ).sum()
    )

    summary_rows.append({
        "mode": mode,

        "original_trades":
            len(x),

        "trades_taken":
            len(accepted),

        "trades_rejected":
            len(rejected),

        "wins":
            wins,

        "losses":
            losses,

        "win_rate_pct":
            (
                wins
                /
                len(accepted)
                * 100
                if len(accepted)
                else 0
            ),

        "gross":
            accepted["gross"].sum()
            if not accepted.empty
            else 0,

        "costs":
            accepted["costs"].sum()
            if not accepted.empty
            else 0,

        "net":
            accepted["new_net"].sum()
            if not accepted.empty
            else 0,

        "old_losers_removed":
            old_losers_removed,

        "old_winners_lost":
            old_winners_lost,
    })


summary = pd.DataFrame(
    summary_rows
)

summary.to_csv(
    OUT / "summary.csv",
    index=False
)


print(
    "\n===== BREAKOUT CONFIRMATION COMPARISON ====="
)

print(
    summary.to_string(
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
        }
    )
)


# ------------------------------------------------------------
# LOSERS REMOVED
# ------------------------------------------------------------

print(
    "\n===== ORIGINAL LOSERS REMOVED ====="
)

removed = r[
    (r["accepted"] == False)
    &
    (r["original_winner"] == False)
].copy()

if removed.empty:

    print("None")

else:

    print(
        removed[
            [
                "mode",
                "date",
                "symbol",
                "direction",
                "original_net",
                "reason",
            ]
        ]
        .sort_values(
            ["mode", "date", "symbol"]
        )
        .to_string(index=False)
    )


# ------------------------------------------------------------
# WINNERS LOST
# ------------------------------------------------------------

print(
    "\n===== ORIGINAL WINNERS LOST ====="
)

lost = r[
    (r["accepted"] == False)
    &
    (r["original_winner"] == True)
].copy()

if lost.empty:

    print("None")

else:

    print(
        lost[
            [
                "mode",
                "date",
                "symbol",
                "direction",
                "original_net",
                "reason",
            ]
        ]
        .sort_values(
            ["mode", "date", "symbol"]
        )
        .to_string(index=False)
    )


# ------------------------------------------------------------
# ACCEPTED TRADE DETAIL
# ------------------------------------------------------------

print(
    "\n===== ACCEPTED TRADE DETAIL ====="
)

accepted_all = r[
    r["accepted"] == True
].copy()

cols = [
    "mode",
    "date",
    "symbol",
    "direction",
    "qty",
    "original_net",
    "new_entry",
    "level",
    "clv",
    "break_atr",
    "confirmation",
    "new_exit",
    "gross",
    "costs",
    "new_net",
]

print(
    accepted_all[
        cols
    ].sort_values(
        [
            "mode",
            "date",
            "symbol"
        ]
    ).to_string(index=False)
)


print(
    "\nWrote:",
    OUT
)
