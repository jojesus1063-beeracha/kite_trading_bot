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
from data_feed import get_instrument_token, fetch_candles, get_company_name
from indicators import add_indicators, atr as atr_indicator
from strategy import evaluate, latest_completed_15m_trend, latest_completed_15m_row
from patterns import is_bear_trap, is_bull_trap
from news_filter import evaluate_news, get_news_confidence
from price_action import evaluate_price_action
from market_trend import get_market_trend, get_sector_trend, sector_for_symbol, compute_market_alignment
from signal_log import log_signal
from risk_manager import RiskManager
from executor import place_entry_order, place_exit_order, cap_quantity_by_margin
from trade_log import record_trade, save_bot_status, load_bot_status
from position_analytics import build_full_analytics_snapshot
from daily_report import load_trades as load_todays_trades
from costs import net_pnl_for_trade
from position_store import save_positions, load_positions, clear_positions
from scheduler import candle_interval_minutes, last_completed_candle_close, next_scan_time, ScanGuard

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


def run_full_scan(kite, symbols, tokens, exchange_map, open_positions, risk):
    """
    One full pass over the watchlist: manage open positions (via
    check_position_exit) and look for new entries on everything else.
    Extracted verbatim from the original while-loop body -- same
    symbols_to_check construction, same per-symbol logic, same status
    dict shape. Used by both the original polling loop and the new
    candle-aligned FULL_SCAN state, so both modes scan identically.

    Returns status_this_cycle (list of per-symbol status dicts), same
    as the original loop produced for save_bot_status().
    """
    symbols_to_check = list(dict.fromkeys(symbols + list(open_positions.keys())))
    status_this_cycle = []


    # Step 4c: market/sector trend, fetched/cached ONCE per scan cycle.
    nifty_fetches = 0
    sector_fetches = 0
    sector_cache_hits = 0
    sector_cache = {}
    try:
        market_trend = get_market_trend(kite, cfg)
        nifty_fetches = 1
    except Exception as e:
        logger.warning(f"Market trend fetch failed, using UNKNOWN: {e}")
        market_trend = "UNKNOWN"

    for symbol in symbols_to_check:
        if symbol not in tokens:
            continue
        token = tokens[symbol]

        if symbol in open_positions:
            status = check_position_exit(kite, symbol, tokens, exchange_map, open_positions, risk, check_trend=True)
            status_this_cycle.append({"symbol": symbol, "status": status})
            continue

        if symbol not in symbols:
            continue

        if not within_trading_window():
            status_this_cycle.append({"symbol": symbol, "status": "outside trading window"})
            continue
        if not risk.can_take_new_trade(current_open_count=len(open_positions)):
            status_this_cycle.append({"symbol": symbol, "status": "risk limit reached"})
            continue

        exchange = exchange_map[symbol]
        df_15m = fetch_candles(kite, token, cfg.TREND_TIMEFRAME, lookback_days=5)
        time.sleep(0.5)
        df_5m = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME, lookback_days=5)
        time.sleep(0.5)
        if df_15m.empty or df_5m.empty:
            status_this_cycle.append({"symbol": symbol, "status": "no candle data"})
            continue

        df_15m, df_5m = add_indicators(df_15m, df_5m, cfg)
        signal = evaluate(symbol, df_15m, df_5m, cfg)

        if signal:
            _snapshot_row = latest_completed_15m_row(df_15m, signal.timestamp)
            try:
                sector = sector_for_symbol(symbol)
                if sector is None:
                    sector_trend = "Sideways"
                elif sector in sector_cache:
                    sector_trend = sector_cache[sector]
                    sector_cache_hits += 1
                else:
                    sector_trend = get_sector_trend(kite, symbol, cfg)
                    sector_cache[sector] = sector_trend
                    sector_fetches += 1
                signal.market_alignment = compute_market_alignment(signal.direction, market_trend, sector_trend)
            except Exception as e:
                logger.warning(f"Market alignment computation failed for {symbol}, using UNKNOWN: {e}")
                signal.market_alignment = "UNKNOWN"

            if getattr(cfg, "ENABLE_MARKET_ALIGNMENT_FILTER", False) and \
               signal.market_alignment in ("MISALIGNED", "STRONG_MISALIGNMENT"):
                logger.info(f"{symbol}: skipped -- market_alignment={signal.market_alignment} "
                            f"(trading against market/sector trend)")
                status_this_cycle.append({"symbol": symbol,
                                          "status": f"skipped, misaligned ({signal.market_alignment})"})
                continue

            # News filter: additional risk layer only -- never generates
            # BUY/SELL signals, only evaluates whether this existing signal
            # should proceed. Confidence tier mapped to a numeric base score.
            _CONF_TO_SCORE = {"REJECTED": 0, "MEDIUM": 50, "HIGH": 75, "VERY_STRONG": 90}
            _base_score = _CONF_TO_SCORE.get(signal.confidence, 70)
            pa_score, pa_detail = evaluate_price_action(df_5m, signal.direction, cfg)
            _base_score += pa_score
            signal.price_action_score = pa_score
            signal.price_action_detail = pa_detail
            if getattr(cfg, "ENABLE_NEWS_FILTER", False):
                try:
                    company_name = get_company_name(kite, symbol, exchange)
                    news_result = evaluate_news(symbol, company_name, cfg)
                except Exception as e:
                    logger.warning(f"News filter failed for {symbol}, treating as UNKNOWN: {e}")
                    news_result = {"sentiment": "UNKNOWN", "headline": None, "published_at": None}
                news_score, news_decision, news_reason = get_news_confidence(
                    signal.direction, news_result["sentiment"], _base_score, cfg)
                signal.news_sentiment = news_result["sentiment"]
                signal.news_headline = news_result["headline"]
                signal.news_confidence_score = news_score
                if news_decision == "REJECT":
                    logger.info(f"{symbol}: skipped -- news={signal.news_sentiment} ({news_reason}) "
                                f"headline: {signal.news_headline}")
                    status_this_cycle.append({"symbol": symbol,
                                              "status": f"skipped, negative news ({signal.news_headline})"})
                    log_signal({
                        "timestamp": str(signal.timestamp), "symbol": symbol,
                        "market_trend": market_trend, "sector": sector_for_symbol(symbol),
                        "market_alignment": signal.market_alignment, "technical_confidence": signal.confidence,
                        "entry_price": signal.entry_price, "direction": signal.direction, "executed": False,
                        "bear_trap": is_bear_trap(df_5m), "bull_trap": is_bull_trap(df_5m),
                        "rejection_reason": news_reason,
                        "news_sentiment": signal.news_sentiment, "news_headline": signal.news_headline,
                        "news_confidence_score": signal.news_confidence_score,
                        "price_action_score": signal.price_action_score,
                        "market_structure": (signal.price_action_detail or {}).get("market_structure"),
                        "support": (signal.price_action_detail or {}).get("support"),
                        "resistance": (signal.price_action_detail or {}).get("resistance"),
                        "breakout": (signal.price_action_detail or {}).get("breakout"),
                        "pullback": (signal.price_action_detail or {}).get("pullback"),
                        "bos": (signal.price_action_detail or {}).get("bos"),
                        "choch": (signal.price_action_detail or {}).get("choch"),
                    })
                    continue


            qty = risk.position_size(signal.entry_price, signal.stop_loss)
            if qty > 0 and not cfg.PAPER_TRADING:
                qty = cap_quantity_by_margin(kite, symbol, signal.direction, qty, exchange, cfg)
            result = place_entry_order(kite, symbol, signal.direction, qty, exchange, cfg)
            if result["success"]:
                target_price = signal.target
                if getattr(cfg, "ENABLE_FIXED_TARGET", False):
                    try:
                        pct = getattr(cfg, "PROFIT_TARGET_PERCENT", 1.5) / 100
                        target_price = (signal.entry_price * (1 + pct) if signal.direction == "BUY"
                                        else signal.entry_price * (1 - pct))
                    except Exception as e:
                        logger.warning(f"{symbol}: fixed target calculation failed, "
                                        f"using strategy-computed target instead: {e}")
                        target_price = signal.target
                open_positions[symbol] = {
                    "direction": signal.direction,
                    "qty": qty,
                    "entry": signal.entry_price,
                    "stop": signal.stop_loss,
                    "target": target_price,
                    "exchange": exchange,
                    "peak_price": signal.entry_price,
                    "tight_mode": False,
                    "entry_time": str(signal.timestamp),
                }
                save_positions(open_positions)
                logger.info(f"ENTRY {signal.direction} {exchange}:{symbol} qty={qty} "
                            f"entry={signal.entry_price:.2f} stop={signal.stop_loss:.2f} "
                            f"target={signal.target:.2f} | {signal.reason} "
                            f"[market_alignment: {signal.market_alignment}]")
                status_this_cycle.append({
                    "symbol": symbol,
                    "status": f"ENTRY {signal.direction} @ {signal.entry_price:.2f}",
                    "confidence": signal.confidence,
                    "market_alignment": signal.market_alignment,
                })
                log_signal({
                    "timestamp": str(signal.timestamp), "symbol": symbol,
                    "market_trend": market_trend, "sector": sector_for_symbol(symbol),
                    "market_alignment": signal.market_alignment, "technical_confidence": signal.confidence,
                    "entry_price": signal.entry_price, "direction": signal.direction, "executed": True,
                    "bear_trap": is_bear_trap(df_5m), "bull_trap": is_bull_trap(df_5m),
                    "raw_close": _snapshot_row.get("close") if _snapshot_row is not None else None,
                    "raw_ema_fast": _snapshot_row.get("ema_fast") if _snapshot_row is not None else None,
                    "raw_ema_slow": _snapshot_row.get("ema_slow") if _snapshot_row is not None else None,
                    "raw_vwap": _snapshot_row.get("vwap") if _snapshot_row is not None else None,
                    "raw_adx": _snapshot_row.get("adx") if _snapshot_row is not None else None,
                    "news_sentiment": signal.news_sentiment, "news_headline": signal.news_headline,
                    "news_confidence_score": signal.news_confidence_score,
                    "price_action_score": signal.price_action_score,
                    "market_structure": (signal.price_action_detail or {}).get("market_structure"),
                    "support": (signal.price_action_detail or {}).get("support"),
                    "resistance": (signal.price_action_detail or {}).get("resistance"),
                    "breakout": (signal.price_action_detail or {}).get("breakout"),
                    "pullback": (signal.price_action_detail or {}).get("pullback"),
                    "bos": (signal.price_action_detail or {}).get("bos"),
                    "choch": (signal.price_action_detail or {}).get("choch"),
                })
            else:
                status_this_cycle.append({"symbol": symbol, "status": "signal found, order failed"})
                log_signal({
                    "timestamp": str(signal.timestamp), "symbol": symbol,
                    "market_trend": market_trend, "sector": sector_for_symbol(symbol),
                    "market_alignment": signal.market_alignment, "technical_confidence": signal.confidence,
                    "entry_price": signal.entry_price, "direction": signal.direction, "executed": False,
                    "rejection_reason": result["reason"],
                    "bear_trap": is_bear_trap(df_5m), "bull_trap": is_bull_trap(df_5m),
                    "raw_close": _snapshot_row.get("close") if _snapshot_row is not None else None,
                    "raw_ema_fast": _snapshot_row.get("ema_fast") if _snapshot_row is not None else None,
                    "raw_ema_slow": _snapshot_row.get("ema_slow") if _snapshot_row is not None else None,
                    "raw_vwap": _snapshot_row.get("vwap") if _snapshot_row is not None else None,
                    "raw_adx": _snapshot_row.get("adx") if _snapshot_row is not None else None,
                    "news_sentiment": signal.news_sentiment, "news_headline": signal.news_headline,
                    "news_confidence_score": signal.news_confidence_score,
                    "price_action_score": signal.price_action_score,
                    "market_structure": (signal.price_action_detail or {}).get("market_structure"),
                    "support": (signal.price_action_detail or {}).get("support"),
                    "resistance": (signal.price_action_detail or {}).get("resistance"),
                    "breakout": (signal.price_action_detail or {}).get("breakout"),
                    "pullback": (signal.price_action_detail or {}).get("pullback"),
                    "bos": (signal.price_action_detail or {}).get("bos"),
                    "choch": (signal.price_action_detail or {}).get("choch"),
                })
        else:
            status_this_cycle.append({"symbol": symbol, "status": "no signal"})

    logger.info(f"Scan Summary\n------------\nNifty fetches: {nifty_fetches}\n"
                f"Sector fetches: {sector_fetches}\nSector cache hits: {sector_cache_hits}\n"
                f"Symbols scanned: {len(symbols_to_check)}\nMarket trend: {market_trend}")
    return status_this_cycle


def _market_structure_broken(df_5m, direction, lookback=10):
    """SECONDARY exit: approximate swing-structure violation using the
    last `lookback` completed 5m candles (excludes current forming one)."""
    if len(df_5m) < lookback + 2:
        return False
    ref = df_5m.iloc[-(lookback + 1):-1]
    close = df_5m.iloc[-1]["close"]
    if direction == "BUY":
        return close < ref["low"].min()
    return close > ref["high"].max()


def _trend_reversed(kite, symbol, token, direction):
    """PRIMARY exit: 15m trend flipped away from the trade's direction."""
    try:
        df_15m = fetch_candles(kite, token, cfg.TREND_TIMEFRAME, lookback_days=5)
        time.sleep(0.3)
        if df_15m.empty:
            return False, None
        df_15m, _ = add_indicators(df_15m, df_15m.copy(), cfg)
        as_of = df_15m["date"].max()
        current_trend = latest_completed_15m_trend(df_15m, as_of, cfg)
        latest_row = df_15m[df_15m["date"] <= as_of].iloc[-1]
        current_adx = latest_row.get("adx")
        wanted = "UP" if direction == "BUY" else "DOWN"
        return (current_trend != wanted), current_adx
    except Exception as e:
        logger.warning(f"Trend-reversal check for {symbol} failed ({e}), staying in trade")
        return False, None


def check_position_exit(kite, symbol, tokens, exchange_map, open_positions, risk, check_trend=False):
    """
    Checks ONE open position for stop-loss/target hit, and closes it if
    so. Extracted verbatim from the original inline loop body -- same
    fetch call, same hit_stop/hit_target math, same exit/record/log
    calls, same side effects on open_positions/risk. Used by both the
    original polling loop and the new candle-aligned position monitor,
    so both modes manage open positions identically.

    Returns a status string ("position open", or the same value the
    original loop would have appended to status_this_cycle) -- caller
    decides what to do with it (e.g. append to a status list, or just
    log it in the lightweight monitor).
    """
    import pandas as pd
    pos = open_positions[symbol]
    exchange = pos.get("exchange", exchange_map.get(symbol, "NSE"))
    token = tokens[symbol]
    df_5m = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME, lookback_days=1, trim_incomplete=False)  # real-time price needed for stop/trailing-stop monitoring
    time.sleep(0.5)
    if df_5m.empty:
        return f"position open | {pos['direction']} entry {pos['entry']:.2f} (current price unavailable)"
    last_price = df_5m.iloc[-1]["close"]
    direction = pos["direction"]
    hit_hard_stop = (last_price <= pos["stop"]) if direction == "BUY" else (last_price >= pos["stop"])

    hit_trailing_stop = False
    structure_broken = False
    trend_reversed = False
    hit_target = False

    if getattr(cfg, "ENABLE_FIXED_TARGET", False):
        # Pure Fixed Target Mode: ONLY hard stop-loss + fixed target are
        # checked. ATR trailing stop, market structure break, and 15m
        # trend reversal are intentionally bypassed entirely -- by
        # explicit design choice, temporary pullbacks and higher-
        # timeframe trend changes must NOT close the trade early.
        target_price = pos.get("target")
        try:
            if target_price is not None:
                hit_target = (last_price >= target_price) if direction == "BUY" else (last_price <= target_price)
        except Exception as e:
            logger.warning(f"{symbol}: fixed target check failed, falling back to stop-loss only: {e}")
            hit_target = False
    else:
        # ORIGINAL exit-stack logic, completely unchanged from before
        # fixed-target mode existed.
        atr_series = atr_indicator(df_5m, 14)
        current_atr = atr_series.iloc[-1] if not atr_series.empty else None
        tight_mode = pos.get("tight_mode", False)
        multiplier = 1.2 if tight_mode else 2.5
        if current_atr is None or pd.isna(current_atr):
            logger.info(f"{symbol}: ATR trailing stop inactive (warming up, needs 14 candles of "
                        f"history) -- protected only by hard stop-loss and structure-break checks")
        else:
            if direction == "BUY":
                pos["peak_price"] = max(pos.get("peak_price", pos["entry"]), last_price)
                trailing_stop = pos["peak_price"] - current_atr * multiplier
                hit_trailing_stop = last_price <= trailing_stop
            else:
                pos["peak_price"] = min(pos.get("peak_price", pos["entry"]), last_price)
                trailing_stop = pos["peak_price"] + current_atr * multiplier
                hit_trailing_stop = last_price >= trailing_stop
            save_positions(open_positions)
        structure_broken = _market_structure_broken(df_5m, direction)
        if check_trend and not (hit_hard_stop or hit_trailing_stop or structure_broken):
            trend_reversed, current_adx = _trend_reversed(kite, symbol, token, direction)
            pos["tight_mode"] = (current_adx is not None and not pd.isna(current_adx)
                                  and current_adx < getattr(cfg, "ADX_THRESHOLD", 25))
            save_positions(open_positions)

    if hit_hard_stop or hit_trailing_stop or structure_broken or trend_reversed or hit_target:
        cost_result = net_pnl_for_trade(direction, pos["qty"], pos["entry"], last_price)
        gross_pnl = cost_result["gross_pnl"]
        costs = cost_result["costs"]
        pnl = cost_result["net_pnl"]  # TRUE NET -- used for kill-switch and logging
        if hit_hard_stop:
            result = "stop"
        elif hit_trailing_stop:
            result = "trailing_stop"
        elif structure_broken:
            result = "structure_break"
        elif hit_target:
            result = "fixed_target"
        else:
            result = "trend_reversal"

        gross_pct = ((last_price - pos["entry"]) / pos["entry"] * 100 if direction == "BUY"
                     else (pos["entry"] - last_price) / pos["entry"] * 100)
        net_pct = (pnl / (pos["entry"] * pos["qty"]) * 100) if pos["entry"] and pos["qty"] else None
        time_in_trade_str = "N/A"
        try:
            if pos.get("entry_time"):
                entry_dt = pd.to_datetime(pos["entry_time"])
                exit_dt = pd.Timestamp.now(tz=entry_dt.tz) if entry_dt.tz is not None else pd.Timestamp.now()
                minutes = (exit_dt - entry_dt).total_seconds() / 60
                time_in_trade_str = f"{minutes:.0f} minutes"
        except Exception:
            pass

        place_exit_order(kite, symbol, direction, pos["qty"], exchange, cfg)
        risk.record_trade_result(pnl)
        record_trade(symbol, direction, pos["qty"], pos["entry"], last_price, pnl,
                     result, exchange=exchange, gross_pnl=gross_pnl, costs=costs)
        logger.info(f"Closed {exchange}:{symbol} ({result}) net P&L={pnl:.2f} (gross={gross_pnl:.2f}, "
                    f"costs={costs:.2f}) | target={pos.get('target')} | gross_pct={gross_pct:+.2f}% | "
                    f"net_pct={net_pct:+.2f}% | time_in_trade={time_in_trade_str}")
        del open_positions[symbol]
        save_positions(open_positions)
        return f"CLOSED ({result}) @ {last_price:.2f} | P&L {pnl:+.2f}"

    unrealized_per_share = (last_price - pos["entry"]) if direction == "BUY" else (pos["entry"] - last_price)
    unrealized = unrealized_per_share * pos["qty"]
    return (f"position open | {direction} entry {pos['entry']:.2f} -> current {last_price:.2f} "
            f"| unrealized {unrealized:+.2f}")


def run():
    start_time = time.time()
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

    # --- Startup health check ---
    # Real incident this addresses: the systemd-scheduled run on
    # 2026-07-31 started with an empty watchlist (user_config.json's
    # watchlist field happened to be empty at that exact moment) and
    # silently scanned 0 symbols for the entire session without any
    # clear error -- looked "running" but did nothing. Fail loudly and
    # exit instead of running all day doing nothing.
    try:
        auth_ok = bool(kite.margins())
    except Exception as e:
        auth_ok = False
        logger.error(f"STARTUP HEALTH CHECK: Kite authentication check failed: {e}")

    logger.info(
        "STARTUP HEALTH CHECK | "
        f"watchlist_size={len(cfg.WATCHLIST)} | "
        f"symbols_loaded={len(symbols)} | "
        f"mode={'PAPER' if cfg.PAPER_TRADING else 'LIVE'} | "
        f"candle_aligned_polling={cfg.ENABLE_CANDLE_ALIGNED_POLLING} | "
        f"market_alignment_filter={getattr(cfg, 'ENABLE_MARKET_ALIGNMENT_FILTER', False)} | "
        f"auth_ok={auth_ok}"
    )

    if len(symbols) == 0:
        logger.error("STARTUP HEALTH CHECK FAILED: watchlist is empty (0 symbols loaded). "
                      "Refusing to start a session that would silently scan nothing all day. "
                      "Check user_config.json's watchlist and restart.")
        return

    if not auth_ok:
        logger.error("STARTUP HEALTH CHECK FAILED: Kite authentication is not working. "
                      "Refusing to start. Re-run auth.py and restart.")
        return

    if cfg.ENABLE_CANDLE_ALIGNED_POLLING:
        logger.info(f"Candle-aligned polling ENABLED | entry timeframe: {cfg.ENTRY_TIMEFRAME} | "
                    f"position check every {cfg.POSITION_CHECK_SECONDS}s | "
                    f"scan buffer: {cfg.SCAN_BUFFER_SECONDS}s")
    scan_guard = ScanGuard()

    while True:
        now = datetime.now()

        # Force square-off at end of day regardless of signals
        if past_square_off():
            for symbol, pos in list(open_positions.items()):
                logger.info(f"Force square-off: {symbol}")
                exchange = pos.get("exchange", exchange_map.get(symbol, "NSE"))
                token = tokens[symbol]
                try:
                    df_5m = fetch_candles(kite, token, cfg.ENTRY_TIMEFRAME, lookback_days=1, trim_incomplete=False)  # real-time price needed for accurate square-off
                    last_price = df_5m.iloc[-1]["close"] if not df_5m.empty else pos["entry"]
                except Exception:
                    last_price = pos["entry"]

                cost_result = net_pnl_for_trade(pos["direction"], pos["qty"], pos["entry"], last_price)
                gross_pnl = cost_result["gross_pnl"]
                costs = cost_result["costs"]
                pnl = cost_result["net_pnl"]
                place_exit_order(kite, symbol, pos["direction"], pos["qty"], exchange, cfg)
                risk.record_trade_result(pnl)
                record_trade(symbol, pos["direction"], pos["qty"], pos["entry"], last_price, pnl,
                             "square_off", exchange=exchange, gross_pnl=gross_pnl, costs=costs)
                logger.info(f"Force-closed {exchange}:{symbol} net P&L={pnl:.2f} (gross={gross_pnl:.2f}, costs={costs:.2f})")
                del open_positions[symbol]
                save_positions(open_positions)
            clear_positions()
            logger.info("Trading day complete. Exiting.")
            break

        if not cfg.ENABLE_CANDLE_ALIGNED_POLLING:
            # --- ORIGINAL BEHAVIOR, byte-for-byte unchanged ---
            status_this_cycle = run_full_scan(kite, symbols, tokens, exchange_map, open_positions, risk)
            try:
                todays_trades = load_todays_trades(datetime.now().strftime("%Y-%m-%d"))
                prev_status = load_bot_status()
                pos_analytics, portfolio_sum, session_sum, health = build_full_analytics_snapshot(
                    kite, cfg, open_positions, symbols, start_time,
                    previous_bot_status=prev_status, todays_trades=todays_trades)
                save_bot_status(status_this_cycle, positions=pos_analytics,
                                portfolio_summary=portfolio_sum, session_summary=session_sum, health=health)
            except Exception as e:
                logger.warning(f"Analytics snapshot failed this cycle, saving basic status only: {e}")
                save_bot_status(status_this_cycle)

            if risk.day.halted:
                logger.warning(f"Trading halted (no new entries, still managing open positions): {risk.day.halt_reason}")

            time.sleep(POLL_SECONDS)
            continue

        # --- NEW: candle-aligned scheduler ---
        interval_min = candle_interval_minutes(cfg.ENTRY_TIMEFRAME)
        target_scan_time = next_scan_time(datetime.now(), interval_min, cfg.SCAN_BUFFER_SECONDS)

        # Position-monitor sub-loop: lightweight checks only, until it's
        # time for the next full scan (or the trading day ends).
        while datetime.now() < target_scan_time and not past_square_off():
            if open_positions:
                pc_start = time.time()
                for sym in list(open_positions.keys()):
                    check_position_exit(kite, sym, tokens, exchange_map, open_positions, risk)
                pc_elapsed = time.time() - pc_start

                if pc_elapsed > cfg.POSITION_CHECK_CRITICAL_SECONDS:
                    logger.error(f"CRITICAL: position check took {pc_elapsed:.1f}s "
                                 f"(threshold {cfg.POSITION_CHECK_CRITICAL_SECONDS}s) -- "
                                 f"possible API/network degradation")
                elif pc_elapsed > cfg.POSITION_CHECK_WARNING_SECONDS:
                    logger.warning(f"Position check took {pc_elapsed:.1f}s "
                                   f"(threshold {cfg.POSITION_CHECK_WARNING_SECONDS}s)")

                remaining = max(0, (target_scan_time - datetime.now()).total_seconds())
                logger.info(f"Position monitor cycle | {len(open_positions)} open "
                            f"({', '.join(open_positions.keys())}) | next scan in {remaining:.0f}s")
            else:
                logger.info("No open positions.")

            try:
                todays_trades = load_todays_trades(datetime.now().strftime("%Y-%m-%d"))
                prev_status = load_bot_status()
                pos_analytics, portfolio_sum, session_sum, health = build_full_analytics_snapshot(
                    kite, cfg, open_positions, symbols, start_time,
                    previous_bot_status=prev_status, todays_trades=todays_trades)
                save_bot_status([], positions=pos_analytics, portfolio_summary=portfolio_sum,
                                session_summary=session_sum, health=health)
            except Exception as e:
                logger.warning(f"Analytics snapshot failed this position-monitor cycle: {e}")

            sleep_for = min(cfg.POSITION_CHECK_SECONDS,
                             max(0, (target_scan_time - datetime.now()).total_seconds()))
            if sleep_for > 0:
                time.sleep(sleep_for)

        if past_square_off():
            continue  # let the top of the loop handle force square-off

        # Time for the full scan -- but guard against scanning the same
        # completed candle twice (e.g. if we looped back around fast).
        current_candle = last_completed_candle_close(datetime.now(), interval_min)
        if scan_guard.should_scan(current_candle):
            scan_delay = (datetime.now() - target_scan_time).total_seconds()
            if scan_delay > cfg.SCAN_DELAY_CRITICAL_SECONDS:
                logger.error(f"CRITICAL: full scan starting {scan_delay:.0f}s late "
                             f"(threshold {cfg.SCAN_DELAY_CRITICAL_SECONDS}s) -- "
                             f"scheduler may be falling behind")
            elif scan_delay > cfg.SCAN_DELAY_WARNING_SECONDS:
                logger.warning(f"Full scan starting {scan_delay:.0f}s late "
                               f"(threshold {cfg.SCAN_DELAY_WARNING_SECONDS}s)")

            logger.info(f"Entry scan starting (5-minute candle) | last completed candle: {current_candle.strftime('%H:%M')}")
            scan_start = time.time()
            status_this_cycle = run_full_scan(kite, symbols, tokens, exchange_map, open_positions, risk)
            scan_elapsed = time.time() - scan_start
            try:
                todays_trades = load_todays_trades(datetime.now().strftime("%Y-%m-%d"))
                prev_status = load_bot_status()
                pos_analytics, portfolio_sum, session_sum, health = build_full_analytics_snapshot(
                    kite, cfg, open_positions, symbols, start_time,
                    previous_bot_status=prev_status, todays_trades=todays_trades)
                save_bot_status(status_this_cycle, positions=pos_analytics,
                                portfolio_summary=portfolio_sum, session_summary=session_sum, health=health)
            except Exception as e:
                logger.warning(f"Analytics snapshot failed this cycle, saving basic status only: {e}")
                save_bot_status(status_this_cycle)
            scan_guard.mark_scanned(current_candle)

            if scan_elapsed > cfg.SCHEDULER_CRITICAL_SCAN_SECONDS:
                logger.error(f"CRITICAL: full scan took {scan_elapsed:.1f}s "
                             f"(threshold {cfg.SCHEDULER_CRITICAL_SCAN_SECONDS}s) -- "
                             f"consider reducing watchlist size or investigating API latency")
            elif scan_elapsed > cfg.SCHEDULER_WARNING_SCAN_SECONDS:
                logger.warning(f"Full scan took {scan_elapsed:.1f}s "
                               f"(threshold {cfg.SCHEDULER_WARNING_SCAN_SECONDS}s)")

            next_target = next_scan_time(datetime.now(), interval_min, cfg.SCAN_BUFFER_SECONDS)
            logger.info(f"Entry scan completed (5-minute candle) | took {scan_elapsed:.1f}s | next scan: {next_target.strftime('%H:%M:%S')}")
        else:
            logger.info(f"Skipped duplicate scan for candle {current_candle.strftime('%H:%M')}")

        if risk.day.halted:
            logger.warning(f"Trading halted (no new entries, still managing open positions): {risk.day.halt_reason}")


if __name__ == "__main__":
    run()
