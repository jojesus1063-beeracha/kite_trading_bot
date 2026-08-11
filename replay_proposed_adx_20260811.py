#!/usr/bin/env python3
"""Historical PAPER replay for 11-Aug-2026 under the proposed loss-reduction stack.

This is analysis-only. It does not import/run main.py and cannot place orders.

Counterfactual policy tested:
- capital: current cfg.CAPITAL (expected Rs 5,000)
- risk/trade: 0.50%
- ADX < 20: reversed EMA9/EMA21 direction
- ADX >= 20: normal EMA9/EMA21 direction
- RSI >= 70 BUY override; RSI <= 30 SELL override
- strategy stop geometry retained at 0.45% for sizing/hybrid 1R/2R
- executable PAPER emergency stop: 0.75%
- hybrid scalp/runner: 1R / 2R, 50/50, runner stop -> breakeven
- MAE: age >10m, MAE <=-0.30%, current <=-0.15%, MFE <+0.30%,
  3 consecutive completed adverse 3-minute EMA candles
- MFE/time: current selected 20-40 rules; >=40m dead loser; >40m giveback
- max 30 accepted entries/day
- max 2 accepted entries/symbol/day
- 30-minute cooldown after the latest completed losing trade in that symbol
- daily realized-loss halt at 5% of capital
- max simultaneous positions from cfg.MAX_OPEN_POSITIONS
- 15:08 square-off

Important replay limitations:
1. Entry opportunities are today's actually executed unique entries. This is a
   same-opportunity counterfactual, not a complete alternate-history rescan of
   every candidate the bot may have skipped during the live session.
2. New quantity is recomputed from 0.50% risk and the 0.45% strategy stop, but
   is capped at the quantity actually accepted today for that opportunity. That
   prevents the replay from assuming more historical margin than was observed.
3. Kite historical 1-minute closes are used as the retrospective proxy for the
   live 25-second PAPER position monitor. Intraminute execution cannot be
   reconstructed exactly from historical bars.
4. The current timing behavior is preserved: MAE/MFE age and excursions begin
   at the stored signal candle start, while the position starts existing at the
   recorded order-submission time when available.
"""
from __future__ import annotations

import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import config as cfg
from auth import get_kite_client
from costs import net_pnl_for_trade

SESSION_DATE = "2026-08-11"
IST = ZoneInfo("Asia/Kolkata")
CAPITAL = float(getattr(cfg, "CAPITAL", 5000.0))
RISK_PER_TRADE_PCT = 0.50
RISK_AMOUNT = CAPITAL * RISK_PER_TRADE_PCT / 100.0
STRATEGY_STOP_PCT = 0.45
EMERGENCY_STOP_PCT = 0.75
NON_HYBRID_TARGET_PCT = float(getattr(cfg, "PROFIT_TARGET_PERCENT", 0.70))

HYBRID_ENABLED = True
SCALP_FRACTION = 0.50
SCALP_R = 1.0
RUNNER_R = 2.0
MOVE_BE = True

ADX_NORMAL_FROM = 20.0
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0

MAE_MIN_AGE = 10.0
MAE_THRESHOLD = -0.30
CURRENT_LOSS_THRESHOLD = -0.15
MAX_MFE_FAILURE = 0.30
ADVERSE_CANDLES = 3

MFE_MIN_HOLD = 20.0
MFE_MID_END = 40.0
MFE_MID_THRESHOLD = 0.40
MFE_LOCK_THRESHOLD = 0.50
MFE_LOCK_CURRENT = 0.30
MFE_LATE_THRESHOLD = 0.30
MFE_GIVEBACK = 50.0
DEAD_TRADE_MINUTES = 40.0
DEAD_TRADE_MAX_MFE = 0.30

MAX_ENTRIES = 30
MAX_PER_SYMBOL = 2
LOSS_COOLDOWN_MINUTES = 30.0
MAX_OPEN = int(getattr(cfg, "MAX_OPEN_POSITIONS", 10) or 10)
DAILY_LOSS_LIMIT = CAPITAL * 5.0 / 100.0
SQUARE_OFF = pd.Timestamp(f"{SESSION_DATE} {getattr(cfg, 'FORCE_SQUARE_OFF_TIME', '15:08')}", tz=IST)

TRADE_HISTORY = Path(__file__).resolve().parent / "trade_history.jsonl"
OUT_CSV = Path("/tmp/proposed_adx_replay_20260811.csv")


def ts(value):
    if value in (None, ""):
        return None
    x = pd.Timestamp(value)
    if x.tzinfo is None:
        return x.tz_localize(IST)
    return x.tz_convert(IST)


def first_value(rows, *keys):
    for key in keys:
        for row in rows:
            value = row.get(key)
            if value is not None and value != "":
                return value
    return None


def group_history():
    if not TRADE_HISTORY.exists():
        raise SystemExit(f"Missing {TRADE_HISTORY}")

    records = []
    for raw in TRADE_HISTORY.open(encoding="utf-8"):
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if row.get("date") == SESSION_DATE:
            records.append(row)

    groups = defaultdict(list)
    for row in records:
        sid = row.get("signal_id")
        if sid:
            key = f"signal:{sid}"
        else:
            key = "fallback:{symbol}|{direction}|{entry}|{entry_time}".format(
                symbol=row.get("symbol"),
                direction=row.get("direction"),
                entry=row.get("entry"),
                entry_time=row.get("entry_time"),
            )
        groups[key].append(row)

    opportunities = []
    for key, rows in groups.items():
        symbol = str(rows[0].get("symbol") or "")
        direction = str(rows[0].get("direction") or "").upper()
        entry = float(rows[0].get("entry") or 0.0)
        qty = sum(int(r.get("qty") or 0) for r in rows)
        actual_net = sum(float(r.get("pnl") or 0.0) for r in rows)
        adx_raw = first_value(rows, "adx_current", "adx")
        adx = float(adx_raw) if adx_raw is not None else None

        signal_start = ts(first_value(rows, "entry_time", "signal_candle_start"))
        signal_close = ts(first_value(rows, "signal_candle_close"))
        order_time = ts(first_value(rows, "order_submitted_at"))
        if order_time is None:
            order_time = signal_close
        if order_time is None and signal_start is not None:
            order_time = signal_start + pd.Timedelta(minutes=3)

        if not symbol or direction not in {"BUY", "SELL"} or entry <= 0 or qty <= 0:
            continue
        if signal_start is None or order_time is None:
            continue

        opportunities.append({
            "key": key,
            "symbol": symbol,
            "actual_direction": direction,
            "entry": entry,
            "actual_qty": qty,
            "actual_net": actual_net,
            "adx": adx,
            "signal_start": signal_start,
            "order_time": order_time,
        })

    opportunities.sort(key=lambda x: (x["order_time"], x["symbol"]))
    return opportunities


def prepare_df(data):
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    if getattr(df["date"].dt, "tz", None) is None:
        df["date"] = df["date"].dt.tz_localize(IST)
    else:
        df["date"] = df["date"].dt.tz_convert(IST)
    return df.sort_values("date").reset_index(drop=True)


def add_rsi_ema(df):
    if df.empty:
        return df
    out = df.copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    out["ema9"] = close.ewm(span=9, adjust=False).mean()
    out["ema21"] = close.ewm(span=21, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
    avg_loss = loss.ewm(alpha=1.0 / RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, math.nan)
    rsi = 100 - (100 / (1 + rs))
    zero_loss = avg_loss == 0
    rsi = rsi.where(~zero_loss, 100.0)
    both_zero = zero_loss & (avg_gain == 0)
    rsi = rsi.where(~both_zero, 50.0)
    out["rsi14"] = rsi
    return out


def fetch_market_data(kite, symbols):
    instruments = {
        x["tradingsymbol"]: x["instrument_token"]
        for x in kite.instruments("NSE")
        if x.get("instrument_type") == "EQ" and x.get("tradingsymbol") in symbols
    }

    missing = sorted(set(symbols) - set(instruments))
    if missing:
        print("WARNING missing NSE EQ instruments:", ", ".join(missing))

    minute = {}
    three = {}
    print(f"Fetching Kite history for {len(instruments)} symbols...")
    for idx, symbol in enumerate(sorted(instruments), start=1):
        token = instruments[symbol]
        one = kite.historical_data(
            token,
            pd.Timestamp(f"{SESSION_DATE} 09:15", tz=IST).to_pydatetime(),
            pd.Timestamp(f"{SESSION_DATE} 15:09", tz=IST).to_pydatetime(),
            "minute",
        )
        time.sleep(0.35)
        # Five calendar days is sufficient for the same EMA/RSI warm-up used
        # by the current entry-timeframe lookback around this session.
        tri = kite.historical_data(
            token,
            pd.Timestamp("2026-08-05 09:15", tz=IST).to_pydatetime(),
            pd.Timestamp(f"{SESSION_DATE} 15:09", tz=IST).to_pydatetime(),
            "3minute",
        )
        time.sleep(0.35)
        minute[symbol] = prepare_df(one)
        three[symbol] = add_rsi_ema(prepare_df(tri))
        print(f"[{idx:02d}/{len(instruments):02d}] {symbol:<14} minute={len(minute[symbol]):3d} 3minute={len(three[symbol]):4d}")
    return minute, three


def indicator_row(df3, signal_start):
    if df3 is None or df3.empty:
        return None
    exact = df3.loc[df3["date"] == signal_start]
    if not exact.empty:
        return exact.iloc[-1]
    prior = df3.loc[df3["date"] <= signal_start]
    if prior.empty:
        return None
    return prior.iloc[-1]


def proposed_direction(adx, ema9, ema21, rsi):
    if adx is None or not math.isfinite(adx):
        normal = False
    else:
        normal = adx >= ADX_NORMAL_FROM

    if ema9 > ema21:
        base = "BUY" if normal else "SELL"
    elif ema9 < ema21:
        base = "SELL" if normal else "BUY"
    else:
        return None, None, None

    override = None
    if rsi is not None and math.isfinite(rsi):
        if rsi >= RSI_OVERBOUGHT:
            override = "BUY"
        elif rsi <= RSI_OVERSOLD:
            override = "SELL"
    return override or base, base, override


def current_policy_direction(adx, ema9, ema21, rsi):
    normal = adx is not None and math.isfinite(adx) and adx > 40.0
    if ema9 > ema21:
        base = "BUY" if normal else "SELL"
    elif ema9 < ema21:
        base = "SELL" if normal else "BUY"
    else:
        return None
    if rsi is not None and math.isfinite(rsi):
        if rsi >= RSI_OVERBOUGHT:
            return "BUY"
        if rsi <= RSI_OVERSOLD:
            return "SELL"
    return base


def signed_pct(direction, entry, price):
    if direction == "BUY":
        return (price - entry) / entry * 100.0
    return (entry - price) / entry * 100.0


def excursions(direction, entry, df_since, current_price):
    if df_since is None or df_since.empty:
        highs = pd.Series([current_price], dtype=float)
        lows = pd.Series([current_price], dtype=float)
    else:
        highs = pd.to_numeric(df_since["high"], errors="coerce").dropna()
        lows = pd.to_numeric(df_since["low"], errors="coerce").dropna()
        if highs.empty:
            highs = pd.Series([current_price], dtype=float)
        if lows.empty:
            lows = pd.Series([current_price], dtype=float)

    if direction == "BUY":
        mfe = (float(highs.max()) - entry) / entry * 100.0
        mae = (float(lows.min()) - entry) / entry * 100.0
    else:
        mfe = (entry - float(lows.min())) / entry * 100.0
        mae = (entry - float(highs.max())) / entry * 100.0
    current = signed_pct(direction, entry, current_price)
    mfe = max(0.0, mfe)
    mae = min(0.0, mae)
    giveback = 0.0 if mfe <= 0 else max(0.0, (mfe - current) / mfe * 100.0)
    return mfe, mae, current, giveback


def adverse_three(direction, df3, now):
    if df3 is None or df3.empty:
        return False
    completed = df3.loc[df3["date"] + pd.Timedelta(minutes=3) <= now].copy()
    if len(completed) < ADVERSE_CANDLES:
        return False
    recent = completed.tail(ADVERSE_CANDLES)
    if direction == "BUY":
        flags = (
            (recent["close"] < recent["ema9"])
            & (recent["ema9"] < recent["ema21"])
        )
    else:
        flags = (
            (recent["close"] > recent["ema9"])
            & (recent["ema9"] > recent["ema21"])
        )
    return bool(flags.all())


def mfe_reason(age, mfe, current, giveback):
    if age < MFE_MIN_HOLD:
        return None
    if age >= DEAD_TRADE_MINUTES and mfe < DEAD_TRADE_MAX_MFE and current < 0.0:
        return "mfe_time_dead_loser_40m"
    if age > MFE_MID_END:
        if mfe >= MFE_LATE_THRESHOLD and giveback >= MFE_GIVEBACK:
            return "mfe_time_late_giveback"
        return None
    if mfe >= MFE_LOCK_THRESHOLD and current <= MFE_LOCK_CURRENT:
        return "mfe_time_lock_20_40"
    if mfe >= MFE_MID_THRESHOLD and giveback >= MFE_GIVEBACK:
        return "mfe_time_giveback_20_40"
    return None


def cost_leg(direction, qty, entry, exit_price):
    c = net_pnl_for_trade(direction, int(qty), float(entry), float(exit_price))
    return {
        "gross": float(c["gross_pnl"]),
        "costs": float(c["costs"]),
        "net": float(c["net_pnl"]),
    }


def replay_trade(op, direction, qty, df1, df3):
    entry = float(op["entry"])
    timer_start = op["signal_start"]
    order_time = op["order_time"]
    strategy_risk = entry * STRATEGY_STOP_PCT / 100.0
    sign = 1.0 if direction == "BUY" else -1.0
    emergency_stop = entry - sign * entry * EMERGENCY_STOP_PCT / 100.0

    hybrid = HYBRID_ENABLED and qty >= 2
    if hybrid:
        scalp_qty = int(math.floor(qty * SCALP_FRACTION))
        scalp_qty = min(max(scalp_qty, 1), qty - 1)
        runner_qty = qty - scalp_qty
        scalp_target = entry + sign * strategy_risk * SCALP_R
        runner_target = entry + sign * strategy_risk * RUNNER_R
    else:
        scalp_qty = 0
        runner_qty = qty
        scalp_target = None
        runner_target = entry + sign * entry * NON_HYBRID_TARGET_PCT / 100.0

    rows = df1.loc[(df1["date"] >= order_time.floor("min")) & (df1["date"] <= SQUARE_OFF)].copy()
    if rows.empty:
        return None

    remaining = qty
    scalp_pending = hybrid
    be_active = False
    legs = []
    max_mfe = 0.0
    min_mae = 0.0

    def close_leg(now, price, amount, reason):
        nonlocal remaining
        amount = min(int(amount), remaining)
        if amount <= 0:
            return
        c = cost_leg(direction, amount, entry, price)
        legs.append({
            "time": now,
            "qty": amount,
            "price": float(price),
            "reason": reason,
            **c,
        })
        remaining -= amount

    for _, row in rows.iterrows():
        now = row["date"]
        price = float(row["close"])
        since = df1.loc[(df1["date"] >= timer_start.floor("min")) & (df1["date"] <= now)]
        mfe, mae, current, giveback = excursions(direction, entry, since, price)
        max_mfe = max(max_mfe, mfe)
        min_mae = min(min_mae, mae)
        age = max(0.0, (now - timer_start).total_seconds() / 60.0)

        # Native executable stop has first priority.
        hit_emergency = price <= emergency_stop if direction == "BUY" else price >= emergency_stop
        if not be_active and hit_emergency:
            close_leg(now, price, remaining, "paper_emergency_stop_0_75")
            break

        # After the scalp, the runner's stop is breakeven.
        if be_active:
            hit_be = price <= entry if direction == "BUY" else price >= entry
            if hit_be:
                close_leg(now, price, remaining, "hybrid_breakeven_stop")
                break

        # Target / hybrid target handling.
        if scalp_pending:
            hit = price >= scalp_target if direction == "BUY" else price <= scalp_target
            if hit:
                close_leg(now, price, scalp_qty, "hybrid_scalp_1r")
                scalp_pending = False
                be_active = MOVE_BE
                # The live stack returns after the partial exit, so do not also
                # close the runner during this same observation.
                continue
        else:
            hit = price >= runner_target if direction == "BUY" else price <= runner_target
            if hit:
                close_leg(now, price, remaining, "hybrid_runner_2r" if hybrid else "fixed_target")
                break

        # MAE/adverse-trend overlay.
        if age > MAE_MIN_AGE:
            adverse = adverse_three(direction, df3, now)
            if (
                mae <= MAE_THRESHOLD
                and current <= CURRENT_LOSS_THRESHOLD
                and mfe < MAX_MFE_FAILURE
                and adverse
            ):
                close_leg(now, price, remaining, "mae_adverse_trend_10m")
                break

        # MFE/time overlay.
        reason = mfe_reason(age, mfe, current, giveback)
        if reason:
            close_leg(now, price, remaining, reason)
            break

        if now >= SQUARE_OFF:
            close_leg(now, price, remaining, "square_off")
            break

    if remaining > 0:
        last = rows.iloc[-1]
        close_leg(last["date"], float(last["close"]), remaining, "square_off_fallback")

    return {
        "legs": legs,
        "exit_time": max(x["time"] for x in legs),
        "net": sum(x["net"] for x in legs),
        "gross": sum(x["gross"] for x in legs),
        "costs": sum(x["costs"] for x in legs),
        "mfe": max_mfe,
        "mae": min_mae,
        "reasons": " + ".join(x["reason"] for x in legs),
    }


def main():
    opportunities = group_history()
    if not opportunities:
        raise SystemExit("No 11-Aug-2026 trade history found")

    symbols = sorted({x["symbol"] for x in opportunities})
    print("Connecting to Kite...")
    kite = get_kite_client()
    minute, three = fetch_market_data(kite, symbols)

    enriched = []
    reconstruction_mismatches = []
    for op in opportunities:
        df3 = three.get(op["symbol"])
        row = indicator_row(df3, op["signal_start"])
        if row is None:
            op["indicator_error"] = "missing 3-minute indicator row"
            enriched.append(op)
            continue
        ema9 = float(row["ema9"])
        ema21 = float(row["ema21"])
        rsi = float(row["rsi14"]) if not pd.isna(row["rsi14"]) else None
        proposed, base, override = proposed_direction(op["adx"], ema9, ema21, rsi)
        reconstructed_current = current_policy_direction(op["adx"], ema9, ema21, rsi)
        op.update({
            "ema9": ema9,
            "ema21": ema21,
            "rsi": rsi,
            "proposed_direction": proposed,
            "proposed_base": base,
            "rsi_override": override,
            "reconstructed_current": reconstructed_current,
        })
        if reconstructed_current and reconstructed_current != op["actual_direction"]:
            reconstruction_mismatches.append(op)
        enriched.append(op)

    # The replay intentionally uses the same 54 executed signal opportunities,
    # then applies the new historical constraints in chronological order.
    accepted = []
    blocked = []
    per_symbol = Counter()
    halt = False
    halt_time = None

    for op in enriched:
        now = op["order_time"]

        # Recalculate realized P&L from exit legs that would already have been
        # booked by this candidate's time. Once the daily halt is hit it stays hit.
        prior_legs = sorted(
            [leg for trade in accepted for leg in trade["replay"]["legs"] if leg["time"] <= now],
            key=lambda x: x["time"],
        )
        running = 0.0
        for leg in prior_legs:
            running += leg["net"]
            if running <= -DAILY_LOSS_LIMIT:
                halt = True
                if halt_time is None:
                    halt_time = leg["time"]
                break

        if halt:
            blocked.append((op, "DAILY_LOSS_5PCT_HALT"))
            continue
        if len(accepted) >= MAX_ENTRIES:
            blocked.append((op, "MAX_30_ENTRIES"))
            continue
        if per_symbol[op["symbol"]] >= MAX_PER_SYMBOL:
            blocked.append((op, "MAX_2_PER_SYMBOL"))
            continue

        open_now = [
            x for x in accepted
            if x["entry_time"] <= now < x["replay"]["exit_time"]
        ]
        if any(x["symbol"] == op["symbol"] for x in open_now):
            blocked.append((op, "SYMBOL_ALREADY_OPEN"))
            continue
        if len(open_now) >= MAX_OPEN:
            blocked.append((op, "MAX_OPEN_POSITIONS"))
            continue

        completed_symbol = [
            x for x in accepted
            if x["symbol"] == op["symbol"] and x["replay"]["exit_time"] <= now
        ]
        if completed_symbol:
            latest = max(completed_symbol, key=lambda x: x["replay"]["exit_time"])
            elapsed = (now - latest["replay"]["exit_time"]).total_seconds() / 60.0
            if latest["replay"]["net"] < 0 and elapsed < LOSS_COOLDOWN_MINUTES:
                blocked.append((op, "30M_LOSS_COOLDOWN"))
                continue

        direction = op.get("proposed_direction")
        if direction not in {"BUY", "SELL"}:
            blocked.append((op, "DIRECTION_UNAVAILABLE"))
            continue

        per_share_risk = op["entry"] * STRATEGY_STOP_PCT / 100.0
        risk_qty = int(RISK_AMOUNT / per_share_risk) if per_share_risk > 0 else 0
        qty = min(risk_qty, op["actual_qty"])
        if qty <= 0:
            blocked.append((op, "QTY_ZERO_AT_0_5PCT_RISK"))
            continue

        df1 = minute.get(op["symbol"])
        df3 = three.get(op["symbol"])
        if df1 is None or df1.empty or df3 is None or df3.empty:
            blocked.append((op, "MISSING_HISTORY"))
            continue

        replay = replay_trade(op, direction, qty, df1, df3)
        if replay is None:
            blocked.append((op, "NO_EXIT_HISTORY"))
            continue

        accepted.append({
            **op,
            "direction": direction,
            "qty": qty,
            "risk_qty": risk_qty,
            "entry_time": now,
            "replay": replay,
        })
        per_symbol[op["symbol"]] += 1

    # Re-evaluate halt on all booked legs at EOD.
    all_legs = sorted([leg for x in accepted for leg in x["replay"]["legs"]], key=lambda x: x["time"])
    running = 0.0
    eod_halt_time = halt_time
    for leg in all_legs:
        running += leg["net"]
        if eod_halt_time is None and running <= -DAILY_LOSS_LIMIT:
            eod_halt_time = leg["time"]

    net = sum(x["replay"]["net"] for x in accepted)
    gross = sum(x["replay"]["gross"] for x in accepted)
    costs = sum(x["replay"]["costs"] for x in accepted)
    wins = sum(1 for x in accepted if x["replay"]["net"] > 0)
    losses = sum(1 for x in accepted if x["replay"]["net"] < 0)
    flat = len(accepted) - wins - losses
    changed_direction = sum(1 for x in accepted if x["direction"] != x["actual_direction"])

    print("\n" + "=" * 168)
    print("11 AUG 2026 — PROPOSED ADX + LOSS-REDUCTION PAPER REPLAY")
    print("ADX <20 REVERSE | ADX >=20 NORMAL | RSI override | risk 0.50% | emergency stop 0.75% | new MAE/MFE guards")
    print("=" * 168)
    print(f"{'#':<3} {'SYMBOL':<13} {'ADX':>6} {'OLD':>5} {'NEW':>5} {'RSI':>7} {'QTY':>4} {'ENTRY':>9} {'ACTUAL':>10} {'REPLAY':>10} {'DELTA':>10} {'MFE%':>7} {'MAE%':>7} EXIT")
    print("-" * 168)
    rows_out = []
    for idx, x in enumerate(accepted, start=1):
        r = x["replay"]
        delta = r["net"] - x["actual_net"]
        rsi_text = "NA" if x.get("rsi") is None else f"{x['rsi']:.1f}"
        adx_text = "NA" if x.get("adx") is None else f"{x['adx']:.2f}"
        print(
            f"{idx:<3} {x['symbol']:<13} {adx_text:>6} {x['actual_direction']:>5} {x['direction']:>5} "
            f"{rsi_text:>7} {x['qty']:>4} {x['entry']:>9.2f} {x['actual_net']:>10.2f} {r['net']:>10.2f} "
            f"{delta:>+10.2f} {r['mfe']:>7.3f} {r['mae']:>7.3f} {r['reasons']}"
        )
        rows_out.append({
            "symbol": x["symbol"], "adx": x.get("adx"), "old_direction": x["actual_direction"],
            "new_direction": x["direction"], "rsi14": x.get("rsi"), "qty": x["qty"],
            "entry": x["entry"], "actual_net": x["actual_net"], "replay_net": r["net"],
            "delta": delta, "mfe_pct": r["mfe"], "mae_pct": r["mae"], "exit": r["reasons"],
            "order_time": x["order_time"], "exit_time": r["exit_time"],
        })

    print("=" * 168)
    print(f"SOURCE UNIQUE EXECUTED OPPORTUNITIES : {len(opportunities)}")
    print(f"ACCEPTED UNDER NEW GUARDS            : {len(accepted)}")
    print(f"BLOCKED BY NEW GUARDS                : {len(blocked)}")
    print(f"DIRECTION CHANGED                    : {changed_direction}")
    print(f"WINS                                 : {wins}")
    print(f"LOSSES                               : {losses}")
    print(f"FLAT                                 : {flat}")
    print(f"WIN RATE                             : {(wins / len(accepted) * 100 if accepted else 0):.2f}%")
    print()
    print(f"REPLAY GROSS P&L                     : Rs {gross:.2f}")
    print(f"REPLAY COSTS                         : Rs {costs:.2f}")
    print(f"REPLAY NET P&L                       : Rs {net:.2f}")
    print(f"REPLAY RETURN                        : {(net / CAPITAL * 100):.3f}%")
    print(f"VS ACTUAL DAY NET (-372.11)          : Rs {net - (-372.11):+.2f}")
    print(f"VS PRIOR CURRENT-LOGIC REPLAY (-426.23): Rs {net - (-426.23):+.2f}")
    print(f"5% DAILY-LOSS HALT THRESHOLD         : Rs {-DAILY_LOSS_LIMIT:.2f}")
    print(f"HALT WOULD HAVE TRIGGERED            : {'YES at ' + str(eod_halt_time) if eod_halt_time is not None else 'NO'}")

    exit_dist = Counter()
    exit_net = defaultdict(float)
    for x in accepted:
        for leg in x["replay"]["legs"]:
            exit_dist[leg["reason"]] += 1
            exit_net[leg["reason"]] += leg["net"]
    print("\n" + "=" * 100)
    print("REPLAY EXIT DISTRIBUTION")
    print("=" * 100)
    for reason, count in exit_dist.most_common():
        print(f"{reason:<38} count={count:<3} net=Rs {exit_net[reason]:>10.2f}")

    print("\n" + "=" * 100)
    print("BLOCKED OPPORTUNITIES")
    print("=" * 100)
    block_counts = Counter(reason for _, reason in blocked)
    for reason, count in block_counts.most_common():
        print(f"{reason:<38} count={count}")
    for op, reason in blocked:
        adx_text = "NA" if op.get("adx") is None else f"{op['adx']:.2f}"
        print(f"{op['order_time'].strftime('%H:%M:%S')} {op['symbol']:<13} ADX={adx_text:>6} old={op['actual_direction']:<4} -> {reason}")

    print("\n" + "=" * 100)
    print("ENTRY-DIRECTION RECONSTRUCTION CHECK")
    print("=" * 100)
    print(f"Historical old-policy direction mismatches vs actual: {len(reconstruction_mismatches)} / {len(opportunities)}")
    if reconstruction_mismatches:
        print("These do not invalidate the proposed replay direction, but they show that historical 3-minute reconstruction")
        print("did not exactly reproduce the runtime entry snapshot for these opportunities:")
        for x in reconstruction_mismatches:
            print(
                f"{x['symbol']:<13} {x['order_time'].strftime('%H:%M:%S')} ADX={x.get('adx')} "
                f"actual={x['actual_direction']} reconstructed_old={x.get('reconstructed_current')} "
                f"EMA9={x.get('ema9')} EMA21={x.get('ema21')} RSI={x.get('rsi')}"
            )

    pd.DataFrame(rows_out).to_csv(OUT_CSV, index=False)
    print(f"\nCSV SAVED: {OUT_CSV}")
    print("NOTE: This is a same-executed-opportunity counterfactual, not a full alternate-history rescan.")


if __name__ == "__main__":
    main()
