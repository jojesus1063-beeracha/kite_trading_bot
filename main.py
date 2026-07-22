"""
Main trading loop.

Run each trading day AFTER auth.py has generated a fresh access token:
    python auth.py
    python main.py

This polls for new completed candles roughly every 5 minutes, evaluates
the strategy on your watchlist, and places (paper or live) orders.

Open positions are persisted to disk (position_store.py) after every
change, so a crash or restart mid-day picks up exactly where it left
off instead of "forgetting" a live position.
"""

import logging
import time
from datetime import datetime

import config as cfg
from auth import get_kite_client
from data_feed import get_instrument_token, fetch_candles
from indicators import add_indicators
from strategy import evaluate
from risk_manager import RiskManager
from executor import place_entry_order, place_exit_order
from trade_log import record_trade
from position_store import save_positions, load_positions, clear_positions

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")

POLL_SECONDS = 60  # check every minute; candles only close every 5, but this keeps stop/target checks responsive


def within_trading_window() -> bool:
    now = datetime.now().time()
    start = datetime.strptime(cfg.NO_ENTRY_BEFORE, "%H:%M").time()
    end = datetime.strptime(cfg.NO_ENTRY_AFTER, "%H:%M").time()
    return start <= now <= end


def past_square_off() -> bool:
    now = datetime.now().time()
    cutoff = datetime.strptime(cfg.FORCE_SQUARE_OFF_TIME, "%H:%M").time()
    return now >= cutoff


def run():
    kite = get_kite_client()
    risk = RiskManager(cfg)

    # cfg.WATCHLIST is a list of {"symbol": ..., "exchange": ...} dicts,
    # so each stock can be NSE or BSE independently.
    symbols = [w["symbol"] for w in cfg.WATCHLIST]
    exchange_map = {w["symbol"]: w["exchange"] for w in cfg.WATCHLIST}
    tokens = {s: get_instrument_token(kite, s, exchange_map[s]) for s in symbols}

    # --- Restore any open positions from before a crash/restart today ---
    open_positions = load_positions()
    if open_positions:
        logger.info(f"Restored {len(open_positions)} open position(s) from a previous "
                    f"session: {list(open_positions.keys())}")
        for sym, pos in open_positions.items():
            # A restored position might reference a symbol that's no
            # longer in the current watchlist (e.g. you changed it via
            # the dashboard since the crash) -- make sure we can still
            # fetch its price and place its exit order regardless.
            exch = pos.get("exchange", exchange_map.get(sym, "NSE"))
            if sym not in tokens:
                tokens[sym] = get_instrument_token(kite, sym, exch)
            exchange_map.setdefault(sym, exch)

    logger.info(f"Starting {'PAPER' if cfg.PAPER_TRADING else 'LIVE'} trading on "
                f"{[(s, exchange_map[s]) for s in symbols]}")

    while True:
        now = datetime.now()

        # Force square-off at end of day regardless of signals
        if past_square_off():
            for symbol, pos in list(open_positions.items()):
                logger.info(f"Force square-off: {symbol}")
                exchange = pos.get("exchange", exchange_map.get(symbol, "NSE"))
                token = tokens[symbol]
                try:
                    df_5m = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME, lookback_days=1)
                    last_price = df_5m.iloc[-1]["close"] if not df_5m.empty else pos["entry"]
                except Exception:
                    last_price = pos["entry"]

                pnl_per_share = (last_price - pos["entry"]) if pos["direction"] == "BUY" else (pos["entry"] - last_price)
                pnl = pnl_per_share * pos["qty"]
                place_exit_order(kite, symbol, pos["direction"], pos["qty"], exchange, cfg)
                risk.record_trade_result(pnl)
                record_trade(symbol, pos["direction"], pos["qty"], pos["entry"], last_price, pnl,
                             "square_off", exchange=exchange)
                logger.info(f"Force-closed {exchange}:{symbol} P&L={pnl:.2f}")
                del open_positions[symbol]
                save_positions(open_positions)
            clear_positions()
            logger.info("Trading day complete. Exiting.")
            break

        # Check every symbol that's either in the current watchlist OR
        # still has an open position (covers the "removed from watchlist
        # mid-trade" case) -- order matters for de-duplication, not logic.
        symbols_to_check = list(dict.fromkeys(symbols + list(open_positions.keys())))

        for symbol in symbols_to_check:
            if symbol not in tokens:
                continue  # shouldn't happen given the restore-time backfill above, but stay safe
            token = tokens[symbol]

            # --- Manage open position: check stop-loss / target ---
            if symbol in open_positions:
                pos = open_positions[symbol]
                exchange = pos.get("exchange", exchange_map.get(symbol, "NSE"))
                df_5m = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME, lookback_days=1)
                if df_5m.empty:
                    continue
                last_price = df_5m.iloc[-1]["close"]

                hit_stop = (last_price <= pos["stop"]) if pos["direction"] == "BUY" else (last_price >= pos["stop"])
                hit_target = (last_price >= pos["target"]) if pos["direction"] == "BUY" else (last_price <= pos["target"])

                if hit_stop or hit_target:
                    pnl_per_share = (last_price - pos["entry"]) if pos["direction"] == "BUY" else (pos["entry"] - last_price)
                    pnl = pnl_per_share * pos["qty"]
                    result = "target" if hit_target else "stop"
                    place_exit_order(kite, symbol, pos["direction"], pos["qty"], exchange, cfg)
                    risk.record_trade_result(pnl)
                    record_trade(symbol, pos["direction"], pos["qty"], pos["entry"], last_price, pnl,
                                 result, exchange=exchange)
                    logger.info(f"Closed {exchange}:{symbol} ({result}) P&L={pnl:.2f}")
                    del open_positions[symbol]
                    save_positions(open_positions)
                continue  # don't look for new entries while a position is open in this symbol

            # A symbol with no open position that's no longer in the
            # current watchlist has nothing left to do here.
            if symbol not in symbols:
                continue

            # --- Look for new entries ---
            if not within_trading_window() or not risk.can_take_new_trade():
                continue

            exchange = exchange_map[symbol]
            df_15m = fetch_candles(kite, token, cfg.TREND_TIMEFRAME, lookback_days=5)
            df_5m = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME, lookback_days=5)
            if df_15m.empty or df_5m.empty:
                continue

            df_15m, df_5m = add_indicators(df_15m, df_5m, cfg)
            signal = evaluate(symbol, df_15m, df_5m, cfg)

            if signal:
                qty = risk.position_size(signal.entry_price, signal.stop_loss)
                result = place_entry_order(kite, symbol, signal.direction, qty, exchange, cfg)
                if result:
                    open_positions[symbol] = {
                        "direction": signal.direction,
                        "qty": qty,
                        "entry": signal.entry_price,
                        "stop": signal.stop_loss,
                        "target": signal.target,
                        "exchange": exchange,
                    }
                    save_positions(open_positions)
                    logger.info(f"ENTRY {signal.direction} {exchange}:{symbol} qty={qty} "
                                f"entry={signal.entry_price:.2f} stop={signal.stop_loss:.2f} "
                                f"target={signal.target:.2f} | {signal.reason}")

        if risk.day.halted:
            logger.warning(f"Trading halted: {risk.day.halt_reason}")
            break

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
