"""Read-only historical replay for the paper-only EMA9/EMA21 experiment."""

import math
import time
from datetime import datetime, timedelta

import pandas as pd

import config as cfg
from auth import get_kite_client
from costs import net_pnl_for_trade


SESSION_DATE = "2026-08-10"
START_TIME = "09:30"
ENTRY_CUTOFF = getattr(cfg, "NO_ENTRY_AFTER", "15:05")
SQUARE_OFF = getattr(cfg, "FORCE_SQUARE_OFF_TIME", "15:08")
SHORTLIST_SIZE = int(getattr(cfg, "ENTRY_SCAN_SHORTLIST_SIZE", 60))


def watchlist_rows():
    rows = []
    for item in cfg.WATCHLIST[:SHORTLIST_SIZE]:
        if isinstance(item, dict):
            rows.append((str(item["symbol"]), str(item.get("exchange", "NSE"))))
        else:
            rows.append((str(item), "NSE"))
    return rows


def frame(rows):
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").drop_duplicates("date").reset_index(drop=True)


def touch(row, direction, price):
    return row["high"] >= price if direction == "BUY" else row["low"] <= price


def stop_touch(row, direction, price):
    return row["low"] <= price if direction == "BUY" else row["high"] >= price


def replay_trade(signal, bars):
    direction = signal["direction"]
    qty = signal["qty"]
    entry = signal["entry"]
    stop = signal["stop"]
    risk = abs(entry - stop)
    one_r = entry + risk if direction == "BUY" else entry - risk
    two_r = entry + 2 * risk if direction == "BUY" else entry - 2 * risk
    scalp_qty = qty // 2 if qty >= 2 else 0
    runner_qty = qty - scalp_qty
    remaining = qty
    runner_stop = stop
    legs = []

    def exit_leg(exit_qty, exit_price, exit_time, reason):
        nonlocal remaining
        if exit_qty <= 0:
            return
        result = net_pnl_for_trade(direction, exit_qty, entry, exit_price)
        legs.append({
            "qty": exit_qty,
            "exit": float(exit_price),
            "exit_time": exit_time,
            "reason": reason,
            **result,
        })
        remaining -= exit_qty

    for _, row in bars.iterrows():
        if remaining <= 0:
            break

        # OHLC cannot reveal ordering if stop and target occur in one candle.
        # Resolve that ambiguity conservatively in favour of the stop.
        if stop_touch(row, direction, runner_stop):
            exit_leg(remaining, runner_stop, row["date"], "stop")
            break

        if scalp_qty and remaining == qty and touch(row, direction, one_r):
            exit_leg(scalp_qty, one_r, row["date"], "hybrid_1R")
            runner_stop = entry

        if remaining > 0 and touch(row, direction, two_r):
            exit_leg(remaining, two_r, row["date"], "hybrid_2R")
            break

    if remaining > 0:
        eligible = bars[bars["date"].dt.strftime("%H:%M") <= SQUARE_OFF]
        if not eligible.empty:
            last = eligible.iloc[-1]
            exit_leg(remaining, float(last["close"]), last["date"], "square_off_approx")

    return legs


if not bool(getattr(cfg, "PAPER_TRADING", False)):
    raise SystemExit("SAFETY BLOCK: PAPER_TRADING must be True")

kite = get_kite_client()
profile = kite.profile()
print("AUTH_OK =", bool(profile.get("user_id")))
print("MODE = PAPER READ-ONLY HISTORICAL REPLAY")
print("SESSION_DATE =", SESSION_DATE)
print("CAPITAL =", float(cfg.CAPITAL))
print("RISK_PER_TRADE_PCT =", float(cfg.RISK_PER_TRADE_PCT))
print("SHORTLIST_SIZE =", SHORTLIST_SIZE)

instrument_map = {}
for exchange in sorted({exchange for _, exchange in watchlist_rows()}):
    for item in kite.instruments(exchange):
        instrument_map[(exchange, item.get("tradingsymbol"))] = item.get("instrument_token")

day = datetime.strptime(SESSION_DATE, "%Y-%m-%d")
history_from = day - timedelta(days=30)
history_to = day.replace(hour=15, minute=30)
data = {}
errors = []

for number, (symbol, exchange) in enumerate(watchlist_rows(), 1):
    token = instrument_map.get((exchange, symbol))
    if not token:
        errors.append(f"{exchange}:{symbol}: token missing")
        continue
    try:
        candles_15m = frame(kite.historical_data(token, history_from, history_to, "15minute"))
        time.sleep(0.36)
        candles_3m = frame(kite.historical_data(token, day, history_to, "3minute"))
        time.sleep(0.36)
        if candles_15m.empty or candles_3m.empty:
            errors.append(f"{exchange}:{symbol}: candle data empty")
            continue
        candles_15m["ema9"] = candles_15m["close"].ewm(span=9, adjust=False).mean()
        candles_15m["ema21"] = candles_15m["close"].ewm(span=21, adjust=False).mean()
        data[symbol] = {"exchange": exchange, "m15": candles_15m, "m3": candles_3m}
    except Exception as exc:
        errors.append(f"{exchange}:{symbol}: {type(exc).__name__}: {exc}")
    if number % 10 == 0:
        print(f"FETCH_PROGRESS = {number}/{len(watchlist_rows())}")

signals = []
for symbol, item in data.items():
    m15 = item["m15"]
    m3 = item["m3"]
    today_15m = m15[m15["date"].dt.strftime("%Y-%m-%d") == SESSION_DATE]
    for idx in today_15m.index:
        if idx == 0:
            continue
        current = m15.loc[idx]
        previous = m15.loc[idx - 1]
        close_time = current["date"] + pd.Timedelta(minutes=15)
        hm = close_time.strftime("%H:%M")
        if hm < START_TIME or hm > ENTRY_CUTOFF:
            continue
        direction = None
        if previous["ema9"] <= previous["ema21"] and current["ema9"] > current["ema21"]:
            direction = "BUY"
        elif previous["ema9"] >= previous["ema21"] and current["ema9"] < current["ema21"]:
            direction = "SELL"
        if not direction:
            continue

        signal_start = close_time - pd.Timedelta(minutes=3)
        prior_start = close_time - pd.Timedelta(minutes=6)
        signal_rows = m3[m3["date"] == signal_start]
        prior_rows = m3[m3["date"] == prior_start]
        if signal_rows.empty or prior_rows.empty:
            errors.append(f"{symbol}: missing 3m entry/prior candle at {close_time}")
            continue
        signal_row = signal_rows.iloc[-1]
        prior_row = prior_rows.iloc[-1]
        entry = float(signal_row["close"])
        buffer_pct = float(
            getattr(cfg, "SL_BUFFER_PCT_SELL", None)
            or getattr(cfg, "SL_BUFFER_PCT", 0.05)
        )
        if direction == "BUY":
            stop = float(prior_row["low"]) * (1 - float(getattr(cfg, "SL_BUFFER_PCT", 0.05)) / 100)
        else:
            stop = float(prior_row["high"]) * (1 + buffer_pct / 100)
        per_share_risk = abs(entry - stop)
        qty = math.floor((float(cfg.CAPITAL) * float(cfg.RISK_PER_TRADE_PCT) / 100) / per_share_risk) if per_share_risk > 0 else 0
        if qty <= 0:
            continue
        signals.append({
            "time": close_time,
            "symbol": symbol,
            "exchange": item["exchange"],
            "direction": direction,
            "entry": entry,
            "stop": stop,
            "qty": qty,
            "previous_ema9": float(previous["ema9"]),
            "previous_ema21": float(previous["ema21"]),
            "ema9": float(current["ema9"]),
            "ema21": float(current["ema21"]),
        })

signals.sort(key=lambda item: (item["time"], item["symbol"]))
trades = []
for signal in signals:
    m3 = data[signal["symbol"]]["m3"]
    future = m3[(m3["date"] >= signal["time"]) & (m3["date"].dt.strftime("%H:%M") <= SQUARE_OFF)]
    legs = replay_trade(signal, future)
    trades.append({**signal, "legs": legs})

print("\n===== FRESH EMA9/EMA21 CROSSOVERS =====")
if not trades:
    print("NO_FRESH_CROSSOVERS")
else:
    for trade in trades:
        net = sum(leg["net_pnl"] for leg in trade["legs"])
        outcomes = ", ".join(
            f"{leg['reason']}:{leg['qty']}@{leg['exit']:.2f} net={leg['net_pnl']:+.2f}"
            for leg in trade["legs"]
        )
        print(
            f"{trade['time']} | {trade['symbol']} {trade['direction']} | "
            f"qty={trade['qty']} entry={trade['entry']:.2f} stop={trade['stop']:.2f} | "
            f"{outcomes} | TRADE_NET={net:+.2f}"
        )

net_values = [sum(leg["net_pnl"] for leg in trade["legs"]) for trade in trades]
gross_total = sum(leg["gross_pnl"] for trade in trades for leg in trade["legs"])
cost_total = sum(leg["costs"] for trade in trades for leg in trade["legs"])
net_total = sum(net_values)

print("\n===== TRIAL SUMMARY =====")
print("SYMBOLS_FETCHED =", len(data))
print("FETCH_ERRORS =", len(errors))
print("FRESH_CROSSOVER_TRADES =", len(trades))
print("WINNERS =", sum(value > 0 for value in net_values))
print("LOSERS =", sum(value < 0 for value in net_values))
print("FLAT =", sum(value == 0 for value in net_values))
print("GROSS_PNL =", round(gross_total, 2))
print("ESTIMATED_COSTS =", round(cost_total, 2))
print("NET_PNL =", round(net_total, 2))
print("RETURN_ON_CAPITAL_PCT =", round(net_total / float(cfg.CAPITAL) * 100, 4))
print("NOTE = OHLC ambiguity is resolved stop-first; 15:08 exits use the latest completed 3m close")
if errors:
    print("\n===== DATA WARNINGS =====")
    for error in errors:
        print(error)
print("EMA921_READ_ONLY_TRIAL_COMPLETE")
