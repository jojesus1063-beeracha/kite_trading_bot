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
import pandas as pd

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
from entry_quality import assess_entry_quality, fetch_live_prices, rank_entry_candidates, validate_live_price
from entry_confirmation import assess_entry_context
from market_trend import get_market_trend, get_sector_trend, sector_for_symbol, compute_market_alignment
from market_trend import (
    clear_relative_strength_cache,
    get_cached_market_candles,
    get_cached_sector_candles,
)
from relative_strength import assess_relative_strength
from validation_recorder import (
    candidate_snapshot,
    record_validation_event,
    signal_snapshot,
)
from signal_log import log_signal
from risk_manager import RiskManager
from executor import place_entry_order, place_exit_order, place_force_exit_order, cap_quantity_by_margin
from trade_log import record_trade, save_bot_status, load_bot_status
from position_analytics import build_full_analytics_snapshot
from daily_report import load_trades as load_todays_trades
from costs import net_pnl_for_trade
from trade_levels import fixed_levels_from_fill
from position_store import save_positions, load_positions, clear_positions
from scheduler import candle_interval_minutes, last_completed_candle_close, next_scan_time, ScanGuard
from cooperative_position_monitor import CooperativeScanMonitor

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



def _cooperative_position_check_if_due(
    kite,
    tokens,
    exchange_map,
    open_positions,
    risk,
    monitor,
):
    """
    Run the existing position-exit checks between scan operations.

    This stays single-threaded, preventing concurrent mutation of
    positions, risk state, broker orders and persistence files.
    """

    if not open_positions:
        return []

    if not monitor.due():
        return []

    started_at = time.monotonic()
    checked_symbols = []

    for position_symbol in list(
        open_positions.keys()
    ):
        if position_symbol not in open_positions:
            continue

        if position_symbol not in tokens:
            logger.error(
                "Cooperative position check skipped "
                f"{position_symbol}: token unavailable"
            )
            continue

        try:
            check_position_exit(
                kite,
                position_symbol,
                tokens,
                exchange_map,
                open_positions,
                risk,
                check_trend=False,
            )

            checked_symbols.append(
                position_symbol
            )
        except Exception as exc:
            logger.exception(
                "Cooperative position check failed "
                f"for {position_symbol}: {exc}"
            )

    monitor.mark_checked()

    elapsed = (
        time.monotonic()
        - started_at
    )

    warning_seconds = float(
        getattr(
            cfg,
            "POSITION_CHECK_WARNING_SECONDS",
            5,
        )
    )

    critical_seconds = float(
        getattr(
            cfg,
            "POSITION_CHECK_CRITICAL_SECONDS",
            15,
        )
    )

    if elapsed > critical_seconds:
        logger.error(
            "CRITICAL: cooperative position check "
            f"took {elapsed:.1f}s"
        )
    elif elapsed > warning_seconds:
        logger.warning(
            "Cooperative position check "
            f"took {elapsed:.1f}s"
        )

    logger.info(
        "Cooperative position check during scan "
        f"| checked={len(checked_symbols)} "
        f"| remaining_open={len(open_positions)} "
        f"| elapsed={elapsed:.1f}s "
        f"| completed_checks="
        f"{monitor.completed_checks}"
    )

    return checked_symbols


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

    # Symbols with positions at scan start cannot be reopened during
    # the same scan if a cooperative stop/target check closes them.
    protected_position_symbols = set(
        open_positions.keys()
    )

    cooperative_monitor = CooperativeScanMonitor(
        interval_seconds=getattr(
            cfg,
            "POSITION_CHECK_SECONDS",
            25,
        )
    )
    entry_candidates = []


    # Step 4c: market/sector trend, fetched/cached ONCE per scan cycle.
    nifty_fetches = 0
    sector_fetches = 0
    sector_cache_hits = 0
    sector_cache = {}
    sector_candle_cache = {}

    clear_relative_strength_cache()
    market_df_15m = pd.DataFrame()

    try:
        market_trend = get_market_trend(kite, cfg)
        market_df_15m = get_cached_market_candles()
        nifty_fetches = 1
    except Exception as e:
        logger.warning(f"Market trend fetch failed, using UNKNOWN: {e}")
        market_trend = "UNKNOWN"
        market_df_15m = pd.DataFrame()

    for symbol in symbols_to_check:
        _cooperative_position_check_if_due(
            kite,
            tokens,
            exchange_map,
            open_positions,
            risk,
            cooperative_monitor,
        )

        if (
            symbol in protected_position_symbols
            and symbol not in open_positions
        ):
            status_this_cycle.append(
                {
                    "symbol": symbol,
                    "status": (
                        "position closed during "
                        "cooperative scan monitoring"
                    ),
                }
            )
            continue

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
            sector = None
            sector_df_15m = pd.DataFrame()

            try:
                sector = sector_for_symbol(symbol)
                if sector is None:
                    sector_trend = "UNKNOWN"
                elif sector in sector_cache:
                    sector_trend = sector_cache[sector]
                    sector_df_15m = sector_candle_cache.get(
                        sector,
                        pd.DataFrame(),
                    )
                    sector_cache_hits += 1
                else:
                    sector_trend = get_sector_trend(kite, symbol, cfg)
                    sector_df_15m = get_cached_sector_candles(
                        sector
                    )
                    sector_cache[sector] = sector_trend
                    sector_candle_cache[sector] = sector_df_15m
                    sector_fetches += 1
                signal.market_alignment = (
                    "UNKNOWN"
                    if sector_trend == "UNKNOWN"
                    else compute_market_alignment(
                        signal.direction,
                        market_trend,
                        sector_trend,
                    )
                )
            except Exception as e:
                logger.warning(f"Market alignment computation failed for {symbol}, using UNKNOWN: {e}")
                signal.market_alignment = "UNKNOWN"

            if getattr(cfg, "ENABLE_MARKET_ALIGNMENT_FILTER", False) and \
               signal.market_alignment not in ("ALIGNED", "STRONG_ALIGNMENT"):
                logger.info(f"{symbol}: skipped -- market_alignment={signal.market_alignment} "
                            f"(trading against market/sector trend)")
                status_this_cycle.append({"symbol": symbol,
                                          "status": f"skipped, misaligned ({signal.market_alignment})"})
                record_validation_event(
                    "candidate_rejected",
                    {
                        **signal_snapshot(signal),
                        "reason_code": "MARKET_ALIGNMENT_FILTER",
                        "reason": "market/sector alignment filter rejected signal",
                        "market_trend": market_trend,
                        "sector": sector_for_symbol(symbol),
                        "market_alignment": signal.market_alignment,
                    },
                )

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

            quality = assess_entry_quality(
                signal,
                df_5m,
            )

            signal.entry_quality_score = quality.score
            signal.entry_quality_detail = quality.detail

            if not quality.accepted:
                logger.info(
                    f"{symbol}: skipped -- poor entry location "
                    f"| quality_score={quality.score:.2f} "
                    f"| {quality.reason} "
                    f"| detail={quality.detail}"
                )

                status_this_cycle.append(
                    {
                        "symbol": symbol,
                        "status": (
                            "skipped, poor entry location: "
                            + quality.reason
                        ),
                    }
                )
                record_validation_event(
                    "candidate_rejected",
                    {
                        **signal_snapshot(signal),
                        "reason_code": "ENTRY_OVEREXTENDED",
                        "reason": quality.reason,
                        "entry_quality_score": quality.score,
                        "entry_quality_detail": quality.detail,
                        "market_trend": market_trend,
                        "sector": sector_for_symbol(symbol),
                    },
                )

                continue

            entry_context = assess_entry_context(
                signal,
                df_15m,
            )

            signal.entry_confirmation_score = (
                entry_context.score_adjustment
            )
            signal.entry_confirmation_detail = (
                entry_context.detail
            )

            if not entry_context.accepted:
                logger.info(
                    f"{symbol}: skipped -- opposing CHoCH "
                    f"| {entry_context.reason} "
                    f"| detail={entry_context.detail}"
                )

                status_this_cycle.append(
                    {
                        "symbol": symbol,
                        "status": (
                            "skipped, opposing CHoCH"
                        ),
                    }
                )
                record_validation_event(
                    "candidate_rejected",
                    {
                        **signal_snapshot(signal),
                        "reason_code": "OPPOSING_CHOCH",
                        "reason": entry_context.reason,
                        "entry_context_score": entry_context.score_adjustment,
                        "entry_context_detail": entry_context.detail,
                        "market_trend": market_trend,
                        "sector": sector_for_symbol(symbol),
                    },
                )

                continue

            relative_strength = assess_relative_strength(
                signal,
                df_15m,
                market_df_15m,
                sector_df_15m,
            )

            signal.relative_strength_score = (
                relative_strength.score_adjustment
            )
            signal.relative_strength_detail = (
                relative_strength.detail
            )

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
                    record_validation_event(
                        "candidate_rejected",
                        {
                            **signal_snapshot(signal),
                            "reason_code": "NEWS_FILTER",
                            "reason": news_reason,
                            "news_sentiment": signal.news_sentiment,
                            "news_headline": signal.news_headline,
                            "news_confidence_score": signal.news_confidence_score,
                            "market_trend": market_trend,
                            "sector": sector_for_symbol(symbol),
                        },
                    )

                    continue


            ranking_score = round(
                float(_base_score)
                + float(quality.score)
                + float(
                    entry_context.score_adjustment
                )
                + float(
                    relative_strength.score_adjustment
                ),
                2,
            )

            entry_candidates.append(
                {
                    "symbol": symbol,
                    "exchange": exchange,
                    "signal": signal,
                    "df_5m": df_5m,
                    "snapshot_row": _snapshot_row,
                    "ranking_score": ranking_score,
                    "quality_score": quality.score,
                    "entry_context_score": (
                        entry_context.score_adjustment
                    ),
                    "entry_context_detail": (
                        entry_context.detail
                    ),
                    "relative_strength_score": (
                        relative_strength.score_adjustment
                    ),
                    "relative_strength_detail": (
                        relative_strength.detail
                    ),
                }
            )

            logger.info(
                f"{symbol}: valid candidate collected "
                f"| ranking_score={ranking_score:.2f} "
                f"| technical_confidence={signal.confidence} "
                f"| price_action={pa_score} "
                f"| entry_quality={quality.score:.2f} "
                f"| entry_context="
                f"{entry_context.score_adjustment:+.2f} "
                f"| confirmations="
                f"{entry_context.detail.get('confirmation_count')} "
                f"| adx_state="
                f"{entry_context.detail.get('adx_state')} "
                f"| relative_strength="
                f"{relative_strength.score_adjustment:+.2f} "
                f"| market_edge="
                f"{relative_strength.detail.get('market_edge_pct')} "
                f"| sector_edge="
                f"{relative_strength.detail.get('sector_edge_pct')}"
            )

            record_validation_event(
                "candidate_collected",
                {
                    **candidate_snapshot(
                        entry_candidates[-1]
                    ),
                    "market_trend": market_trend,
                    "sector": sector_for_symbol(
                        symbol
                    ),
                    "exchange": exchange,
                },
            )
        else:
            status_this_cycle.append({"symbol": symbol, "status": "no signal"})

    # A complete watchlist pass can exceed the configured monitoring
    # interval, so check existing positions before ranking candidates.
    _cooperative_position_check_if_due(
        kite,
        tokens,
        exchange_map,
        open_positions,
        risk,
        cooperative_monitor,
    )

    ranked_candidates = rank_entry_candidates(
        entry_candidates
    )

    batch_live_prices = fetch_live_prices(
        kite,
        ranked_candidates,
    )

    if ranked_candidates:
        logger.info(
            "Candidate ranking complete "
            f"| valid_candidates={len(ranked_candidates)} "
            f"| ranking="
            f"{[(item['symbol'], item['ranking_score']) for item in ranked_candidates]}"
        )

    for candidate_rank, candidate in enumerate(
        ranked_candidates,
        start=1,
    ):
        # Entry verification can take several seconds. Recheck existing
        # positions before processing each ranked entry candidate.
        _cooperative_position_check_if_due(
            kite,
            tokens,
            exchange_map,
            open_positions,
            risk,
            cooperative_monitor,
        )

        symbol = candidate["symbol"]

        if (
            symbol in protected_position_symbols
            and symbol not in open_positions
        ):
            status_this_cycle.append(
                {
                    "symbol": symbol,
                    "status": (
                        "candidate skipped after "
                        "same-scan position exit"
                    ),
                }
            )
            continue
        exchange = candidate["exchange"]
        signal = candidate["signal"]
        df_5m = candidate["df_5m"]
        _snapshot_row = candidate["snapshot_row"]

        if not within_trading_window():
            status_this_cycle.append(
                {
                    "symbol": symbol,
                    "status": (
                        "outside trading window "
                        "after candidate ranking"
                    ),
                }
            )
            continue

        if not risk.can_take_new_trade(
            current_open_count=len(
                open_positions
            )
        ):
            status_this_cycle.append(
                {
                    "symbol": symbol,
                    "status": (
                        "risk limit reached "
                        "after candidate ranking"
                    ),
                }
            )

            logger.info(
                f"{symbol}: ranked candidate not executed "
                f"| rank={candidate_rank} "
                "| existing risk limit reached"
            )
            continue

        logger.info(
            f"{symbol}: executing ranked candidate "
            f"| rank={candidate_rank}/"
            f"{len(ranked_candidates)} "
            f"| ranking_score="
            f"{candidate['ranking_score']:.2f}"
        )
        planned_stop_price = signal.stop_loss
        if getattr(cfg, "ENABLE_FIXED_TARGET", False):
            planned_stop_price, _ = (
                fixed_levels_from_fill(
                    signal.direction,
                    signal.entry_price,
                    getattr(cfg, "STOP_LOSS_PERCENT", 0.45),
                    getattr(cfg, "PROFIT_TARGET_PERCENT", 1.5),
                )
            )
        fresh_live_price = batch_live_prices.get(
            symbol
        )

        fresh_validation = validate_live_price(
            signal,
            fresh_live_price,
        )

        if not fresh_validation.accepted:
            logger.info(
                f"{symbol}: skipped -- stale or "
                f"adverse entry price | "
                f"signal={fresh_validation.signal_price} "
                f"live={fresh_validation.live_price} "
                f"drift={fresh_validation.drift_pct} "
                f"| {fresh_validation.reason}"
            )

            status_this_cycle.append(
                {
                    "symbol": symbol,
                    "status": (
                        "skipped, stale entry price: "
                        + fresh_validation.reason
                    ),
                }
            )
            continue

        if fresh_validation.live_price is None:
            logger.warning(
                f"{symbol}: fresh quote unavailable; "
                "continuing with signal price"
            )
        else:
            logger.info(
                f"{symbol}: fresh price validated "
                f"| signal={fresh_validation.signal_price} "
                f"live={fresh_validation.live_price} "
                f"drift={fresh_validation.drift_pct:+.4f}%"
            )

        qty = risk.position_size(signal.entry_price, planned_stop_price)
        if qty > 0 and not cfg.PAPER_TRADING:
            qty = cap_quantity_by_margin(kite, symbol, signal.direction, qty, exchange, cfg)
        result = place_entry_order(kite, symbol, signal.direction, qty, exchange, cfg)
        if result["success"]:
            confirmed_qty = result["filled_quantity"]
            # Confirmed average fill price is the REAL entry price used for the
            # position/qty tracked below -- but stop/target stay computed from
            # the ORIGINAL signal price, per explicit requirement: never
            # silently redesign strategy-derived stop/target because the
            # actual fill differs from the signal price.
            confirmed_entry_price = result["average_price"] if result["average_price"] is not None else signal.entry_price
            stop_price = signal.stop_loss
            target_price = signal.target
            if getattr(cfg, "ENABLE_FIXED_TARGET", False):
                stop_price, target_price = fixed_levels_from_fill(
                    signal.direction,
                    confirmed_entry_price,
                    getattr(cfg, "STOP_LOSS_PERCENT", 0.45),
                    getattr(cfg, "PROFIT_TARGET_PERCENT", 1.5),
                )
            open_positions[symbol] = {
                "direction": signal.direction,
                "qty": confirmed_qty,
                "entry": confirmed_entry_price,
                "stop": stop_price,
                "target": target_price,
                "exchange": exchange,
                "peak_price": confirmed_entry_price,
                "tight_mode": False,
                "entry_time": str(signal.timestamp),
                "entry_order_id": result.get("order_id"),
                "entry_operation_id": result.get("operation_id"),
                "requested_quantity": result.get("requested_quantity", confirmed_qty),
                "filled_quantity": confirmed_qty,
                "entry_fill_status": result.get("status"),
                "entry_average_price": result.get("average_price"),
                "entry_confirmation_pending": result.get("entry_confirmation_pending", False),
                "entry_status_message": result.get("reason"),
            }
            save_positions(open_positions)
            logger.info(f"ENTRY {signal.direction} {exchange}:{symbol} qty={confirmed_qty} "
                        f"entry={confirmed_entry_price:.2f} stop={stop_price:.2f} "
                        f"target={signal.target:.2f} | {signal.reason} "
                        f"[market_alignment: {signal.market_alignment}] "
                        f"[fill_status: {result.get('status')}]")
            status_this_cycle.append({
                "symbol": symbol,
                "status": f"ENTRY {signal.direction} @ {confirmed_entry_price:.2f}",
                "confidence": signal.confidence,
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
        # Retain the most favourable closing price present in the
        # fetched monitoring window. This prevents a later pullback
        # from replacing an earlier peak and works during ATR warm-up.
        completed_closes = pd.to_numeric(
            df_5m["close"],
            errors="coerce",
        ).dropna()

        previous_peak = float(
            pos.get(
                "peak_price",
                pos["entry"],
            )
        )

        if completed_closes.empty:
            observed_extreme = float(
                last_price
            )
        elif direction == "BUY":
            observed_extreme = float(
                completed_closes.max()
            )
        else:
            observed_extreme = float(
                completed_closes.min()
            )

        if direction == "BUY":
            updated_peak = max(
                previous_peak,
                observed_extreme,
            )
        else:
            updated_peak = min(
                previous_peak,
                observed_extreme,
            )

        pos["peak_price"] = updated_peak

        if updated_peak != previous_peak:
            save_positions(open_positions)

        atr_series = atr_indicator(df_5m, 14)
        current_atr = atr_series.iloc[-1] if not atr_series.empty else None
        tight_mode = pos.get("tight_mode", False)
        multiplier = 1.2 if tight_mode else 2.5

        if current_atr is None or pd.isna(current_atr):
            logger.info(f"{symbol}: ATR trailing stop inactive (warming up, needs 14 candles of "
                        f"history) -- protected only by hard stop-loss and structure-break checks")
        else:
            if direction == "BUY":
                trailing_stop = (
                    pos["peak_price"]
                    - current_atr * multiplier
                )
                hit_trailing_stop = (
                    last_price <= trailing_stop
                )
            else:
                trailing_stop = (
                    pos["peak_price"]
                    + current_atr * multiplier
                )
                hit_trailing_stop = (
                    last_price >= trailing_stop
                )
        structure_broken = _market_structure_broken(df_5m, direction)
        if check_trend and not (hit_hard_stop or hit_trailing_stop or structure_broken):
            trend_reversed, current_adx = _trend_reversed(kite, symbol, token, direction)
            pos["tight_mode"] = (current_adx is not None and not pd.isna(current_adx)
                                  and current_adx < getattr(cfg, "ADX_THRESHOLD", 25))
            save_positions(open_positions)

    if hit_hard_stop or hit_trailing_stop or structure_broken or trend_reversed or hit_target:
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

        time_in_trade_str = "N/A"
        try:
            if pos.get("entry_time"):
                entry_dt = pd.to_datetime(pos["entry_time"])
                exit_dt = (
                    pd.Timestamp.now(tz=entry_dt.tz)
                    if entry_dt.tz is not None
                    else pd.Timestamp.now()
                )
                minutes = (
                    exit_dt - entry_dt
                ).total_seconds() / 60
                time_in_trade_str = f"{minutes:.0f} minutes"
        except Exception:
            pass

        requested_qty = int(pos["qty"])

        exit_result = place_exit_order(
            kite,
            symbol,
            direction,
            requested_qty,
            exchange,
            cfg,
        )

        confirmed_qty = int(
            exit_result.get("filled_quantity") or 0
        )

        # Persist the latest known broker-exit state even when nothing
        # has filled yet. This prevents a restart from forgetting that
        # an exit is still pending or needs reconciliation.
        if exit_result.get("order_id") is not None:
            pos["exit_order_id"] = exit_result.get("order_id")

        if exit_result.get("operation_id") is not None:
            pos["exit_operation_id"] = exit_result.get(
                "operation_id"
            )

        pos["exit_requested_quantity"] = (
            exit_result.get(
                "requested_quantity",
                requested_qty,
            )
        )
        pos["exit_fill_status"] = exit_result.get("status")
        pos["exit_confirmation_pending"] = exit_result.get(
            "exit_confirmation_pending",
            False,
        )
        pos["exit_status_message"] = exit_result.get("reason")
        pos["exit_reason"] = result

        def finalize_terminal_exit_operation():
            """
            Resolve the durable EXIT operation only AFTER the
            corresponding local position update has been saved.

            exit_result["resolved"] means the broker result is
            terminal. The pending-order record intentionally remains
            unresolved until this function is called.
            """
            operation_id = exit_result.get("operation_id")

            if (
                operation_id is None
                or not exit_result.get("resolved", False)
            ):
                return

            from pending_order_store import mark_order_resolved

            mark_order_resolved(
                operation_id,
                resolution_reason=exit_result.get("status"),
            )

        if confirmed_qty <= 0:
            save_positions(open_positions)
            finalize_terminal_exit_operation()

            if pos["exit_confirmation_pending"]:
                logger.warning(
                    f"{symbol}: exit trigger={result}, but broker "
                    f"confirmation is still pending "
                    f"({exit_result.get('status')}); "
                    f"position remains open locally"
                )
                return (
                    f"EXIT PENDING ({result}) | "
                    f"status={exit_result.get('status')} | "
                    f"confirmed 0/{requested_qty}"
                )

            logger.warning(
                f"{symbol}: exit trigger={result}, but no quantity "
                f"was filled ({exit_result.get('status')}); "
                f"position remains open"
            )
            return (
                f"EXIT NOT FILLED ({result}) | "
                f"status={exit_result.get('status')}"
            )

        if confirmed_qty > requested_qty:
            logger.critical(
                f"{symbol}: broker reported exit fill "
                f"{confirmed_qty} greater than local position "
                f"quantity {requested_qty}. "
                f"Manual reconciliation required."
            )
            raise RuntimeError(
                "confirmed exit quantity exceeds local position"
            )

        exit_price = exit_result.get("average_price")

        # Paper mode intentionally has no broker average price.
        if exit_price is None and cfg.PAPER_TRADING:
            exit_price = last_price

        if exit_price is None:
            # A real fill with no execution price must never have a
            # fabricated P&L. Reduce the known live exposure, but leave
            # an explicit reconciliation record instead of inventing a
            # price or silently recording inaccurate profit.
            remaining_qty = requested_qty - confirmed_qty

            pos["qty"] = remaining_qty
            pos["exit_filled_quantity"] = confirmed_qty
            pos["exit_average_price"] = None
            pos["manual_reconciliation_required"] = True
            pos["exit_status_message"] = (
                "confirmed broker exit fill has no average price; "
                "P&L was not recorded"
            )

            save_positions(open_positions)
            finalize_terminal_exit_operation()

            logger.critical(
                f"{symbol}: {confirmed_qty} exit shares confirmed, "
                f"but broker average price is unavailable. "
                f"Remaining local quantity={remaining_qty}. "
                f"Manual reconciliation required; P&L not recorded."
            )

            return (
                f"EXIT RECONCILIATION REQUIRED ({result}) | "
                f"confirmed {confirmed_qty}/{requested_qty}"
            )

        exit_price = float(exit_price)

        cost_result = net_pnl_for_trade(
            direction,
            confirmed_qty,
            pos["entry"],
            exit_price,
        )

        gross_pnl = cost_result["gross_pnl"]
        costs = cost_result["costs"]
        pnl = cost_result["net_pnl"]

        gross_pct = (
            (
                exit_price - pos["entry"]
            ) / pos["entry"] * 100
            if direction == "BUY"
            else (
                pos["entry"] - exit_price
            ) / pos["entry"] * 100
        )

        net_pct = (
            pnl / (
                pos["entry"] * confirmed_qty
            ) * 100
            if pos["entry"] and confirmed_qty
            else None
        )

        risk.record_trade_result(pnl)

        record_trade(
            symbol,
            direction,
            confirmed_qty,
            pos["entry"],
            exit_price,
            pnl,
            result,
            exchange=exchange,
            gross_pnl=gross_pnl,
            costs=costs,
        )

        remaining_qty = requested_qty - confirmed_qty

        pos["exit_filled_quantity"] = confirmed_qty
        pos["exit_average_price"] = exit_price
        pos["last_exit_price"] = exit_price
        pos["last_exit_pnl"] = pnl

        if remaining_qty == 0:
            logger.info(
                f"Closed {exchange}:{symbol} ({result}) "
                f"confirmed_qty={confirmed_qty} "
                f"exit={exit_price:.2f} "
                f"net P&L={pnl:.2f} "
                f"(gross={gross_pnl:.2f}, costs={costs:.2f}) "
                f"| target={pos.get('target')} "
                f"| gross_pct={gross_pct:+.2f}% "
                f"| net_pct={net_pct:+.2f}% "
                f"| time_in_trade={time_in_trade_str} "
                f"| fill_status={exit_result.get('status')}"
            )

            del open_positions[symbol]
            save_positions(open_positions)
            finalize_terminal_exit_operation()

            return (
                f"CLOSED ({result}) @ {exit_price:.2f} "
                f"| qty {confirmed_qty} "
                f"| P&L {pnl:+.2f}"
            )

        pos["qty"] = remaining_qty
        save_positions(open_positions)
        finalize_terminal_exit_operation()

        logger.warning(
            f"Partially closed {exchange}:{symbol} ({result}) "
            f"confirmed_qty={confirmed_qty}/{requested_qty} "
            f"exit={exit_price:.2f} "
            f"remaining_qty={remaining_qty} "
            f"net P&L={pnl:.2f} "
            f"| fill_status={exit_result.get('status')} "
            f"| confirmation_pending="
            f"{pos['exit_confirmation_pending']}"
        )

        return (
            f"PARTIAL EXIT ({result}) @ {exit_price:.2f} "
            f"| closed {confirmed_qty}/{requested_qty} "
            f"| remaining {remaining_qty} "
            f"| P&L {pnl:+.2f}"
        )
    unrealized_per_share = (last_price - pos["entry"]) if direction == "BUY" else (pos["entry"] - last_price)
    unrealized = unrealized_per_share * pos["qty"]
    return (f"position open | {direction} entry {pos['entry']:.2f} -> current {last_price:.2f} "
            f"| unrealized {unrealized:+.2f}")


def recover_unresolved_entries(kite, open_positions, cfg, positions_path=None):
    """
    Stage 3 restart recovery: inspects unresolved ENTRY operations
    BEFORE any new scanning begins. For orders with a broker order_id,
    resumes verification and applies only the NEWLY confirmed delta
    (exec_result.filled_quantity - quantity_already_applied_locally)
    -- this is what makes repeated recovery cycles idempotent; running
    this function twice in a row with no new fills applies a delta of
    zero both times. For intents with NO order_id at all, submission
    outcome is genuinely unknown -- never blindly resubmitted, only
    logged as a critical, requiring deliberate reconciliation.
    """
    from pending_order_store import list_unresolved_orders, update_order_verification, mark_order_resolved
    from order_verification import verify_order_execution

    unresolved_entries = [o for o in list_unresolved_orders() if o["action"] == "ENTRY"]
    if not unresolved_entries:
        return

    logger.info(f"Restart recovery: {len(unresolved_entries)} unresolved ENTRY operation(s) found")

    for op in unresolved_entries:
        symbol = op["symbol"]
        if op["order_id"] is None:
            logger.error(f"CRITICAL: unresolved ENTRY intent for {symbol} has no broker order_id "
                        f"(operation_id={op['operation_id']}) -- submission outcome is genuinely "
                        f"unknown. Requires deliberate reconciliation, NOT automatic resubmission. "
                        f"Leaving unresolved.")
            continue

        logger.info(f"Resuming verification for unresolved ENTRY: {symbol} (order_id={op['order_id']})")
        exec_result = verify_order_execution(
            kite, op["order_id"], op["requested_quantity"],
            max_wait_seconds=getattr(cfg, "ORDER_VERIFY_MAX_WAIT_SECONDS", 15),
            poll_interval_seconds=getattr(cfg, "ORDER_VERIFY_POLL_INTERVAL_SECONDS", 1),
        )
        update_order_verification(op["operation_id"], exec_result)

        already_applied = open_positions.get(symbol, {}).get("filled_quantity", 0)
        newly_confirmed = exec_result.filled_quantity - already_applied

        if newly_confirmed > 0:
            if symbol in open_positions:
                open_positions[symbol]["qty"] = open_positions[symbol].get("qty", 0) + newly_confirmed
                open_positions[symbol]["filled_quantity"] = exec_result.filled_quantity
                if exec_result.average_price is not None:
                    open_positions[symbol]["entry"] = exec_result.average_price
                    open_positions[symbol]["entry_average_price"] = exec_result.average_price
                logger.info(f"Recovery applied +{newly_confirmed} newly confirmed shares to existing "
                            f"{symbol} position (total filled_quantity now {exec_result.filled_quantity})")
            else:
                logger.error(f"CRITICAL: recovered a filled position for {symbol} with NO local "
                            f"record before restart -- stop/target are UNKNOWN, requires MANUAL review")
                open_positions[symbol] = {
                    "direction": op["side"], "qty": exec_result.filled_quantity,
                    "entry": exec_result.average_price, "stop": None, "target": None,
                    "exchange": op["exchange"], "peak_price": exec_result.average_price, "tight_mode": False,
                    "entry_time": op["created_at"], "entry_order_id": op["order_id"],
                    "entry_operation_id": op["operation_id"], "requested_quantity": op["requested_quantity"],
                    "filled_quantity": exec_result.filled_quantity, "entry_fill_status": exec_result.status,
                    "entry_average_price": exec_result.average_price,
                    "entry_confirmation_pending": not exec_result.terminal,
                    "entry_status_message": "recovered after restart with no prior local record -- "
                                             "stop/target unknown, requires manual review",
                }
        elif newly_confirmed == 0:
            logger.info(f"Recovery for {symbol}: no NEW fills since last check "
                        f"(idempotent, filled_quantity still {exec_result.filled_quantity})")

        if exec_result.terminal:
            mark_order_resolved(op["operation_id"], resolution_reason=exec_result.status)
            if symbol in open_positions:
                open_positions[symbol]["entry_confirmation_pending"] = False
            logger.info(f"Resolved recovered ENTRY for {symbol}: {exec_result.status}, "
                        f"filled={exec_result.filled_quantity}")
        else:
            logger.warning(f"ENTRY for {symbol} still unresolved after recovery attempt "
                           f"({exec_result.status})")

    save_positions(open_positions, positions_path=positions_path)



def apply_force_exit_result(
    symbol,
    position,
    exit_result,
    open_positions,
    risk,
    exchange,
    fallback_price,
    positions_path=None,
):
    """
    Apply only broker-confirmed FORCE_EXIT quantity.

    The pending FORCE_EXIT operation is resolved only after
    the local position change and confirmed P&L have been
    persisted.
    """
    requested_quantity = int(
        position.get("qty", 0) or 0
    )
    confirmed_quantity = int(
        exit_result.get("filled_quantity") or 0
    )

    if exit_result.get("order_id") is not None:
        position["force_exit_order_id"] = (
            exit_result.get("order_id")
        )

    if exit_result.get("operation_id") is not None:
        position["force_exit_operation_id"] = (
            exit_result.get("operation_id")
        )

    position["force_exit_requested_quantity"] = (
        exit_result.get(
            "requested_quantity",
            requested_quantity,
        )
    )
    position["force_exit_fill_status"] = (
        exit_result.get("status")
    )
    position["force_exit_confirmation_pending"] = (
        exit_result.get(
            "exit_confirmation_pending",
            False,
        )
    )
    position["force_exit_status_message"] = (
        exit_result.get("reason")
    )
    position["force_exit_reason"] = "square_off"

    def finalize_terminal_force_exit():
        operation_id = exit_result.get(
            "operation_id"
        )

        if (
            operation_id is None
            or not exit_result.get(
                "resolved",
                False,
            )
        ):
            return

        from pending_order_store import (
            mark_order_resolved,
        )

        mark_order_resolved(
            operation_id,
            resolution_reason=exit_result.get(
                "status"
            ),
        )

    if confirmed_quantity <= 0:
        save_positions(
            open_positions,
            positions_path=positions_path,
        )
        finalize_terminal_force_exit()

        if position[
            "force_exit_confirmation_pending"
        ]:
            logger.critical(
                f"FORCE EXIT pending for "
                f"{exchange}:{symbol}: "
                f"0/{requested_quantity} confirmed "
                f"(status={exit_result.get('status')}). "
                f"Position retained for restart recovery."
            )

            return (
                "FORCE EXIT PENDING | "
                f"status={exit_result.get('status')} | "
                f"confirmed 0/{requested_quantity}"
            )

        logger.error(
            f"FORCE EXIT not filled for "
            f"{exchange}:{symbol}: "
            f"status={exit_result.get('status')}. "
            f"Position remains open locally."
        )

        return (
            "FORCE EXIT NOT FILLED | "
            f"status={exit_result.get('status')}"
        )

    if confirmed_quantity > requested_quantity:
        position[
            "manual_reconciliation_required"
        ] = True
        position["force_exit_status_message"] = (
            "broker force-exit fill exceeds "
            "local position quantity"
        )

        save_positions(
            open_positions,
            positions_path=positions_path,
        )

        logger.critical(
            f"FORCE EXIT for {symbol} reported "
            f"{confirmed_quantity} fills against "
            f"local quantity {requested_quantity}. "
            f"Manual reconciliation required."
        )

        return "FORCE EXIT RECONCILIATION REQUIRED"

    exit_price = exit_result.get("average_price")

    if exit_price is None and cfg.PAPER_TRADING:
        exit_price = fallback_price

    remaining_quantity = (
        requested_quantity - confirmed_quantity
    )

    if exit_price is None:
        position["qty"] = remaining_quantity
        position[
            "force_exit_filled_quantity"
        ] = confirmed_quantity
        position["force_exit_average_price"] = None
        position[
            "manual_reconciliation_required"
        ] = True
        position["force_exit_status_message"] = (
            "confirmed force-exit fill has no "
            "broker average price; exposure reduced "
            "but P&L not recorded"
        )

        if remaining_quantity == 0:
            del open_positions[symbol]

        save_positions(
            open_positions,
            positions_path=positions_path,
        )
        finalize_terminal_force_exit()

        logger.critical(
            f"FORCE EXIT confirmed "
            f"{confirmed_quantity}/"
            f"{requested_quantity} shares for "
            f"{symbol}, but broker average price "
            f"is unavailable. P&L not fabricated."
        )

        return (
            "FORCE EXIT RECONCILIATION REQUIRED | "
            f"confirmed {confirmed_quantity}/"
            f"{requested_quantity}"
        )

    exit_price = float(exit_price)

    cost_result = net_pnl_for_trade(
        position["direction"],
        confirmed_quantity,
        position["entry"],
        exit_price,
    )
    gross_pnl = cost_result["gross_pnl"]
    costs = cost_result["costs"]
    pnl = cost_result["net_pnl"]

    risk.record_trade_result(pnl)

    record_trade(
        symbol,
        position["direction"],
        confirmed_quantity,
        position["entry"],
        exit_price,
        pnl,
        "square_off",
        exchange=exchange,
        gross_pnl=gross_pnl,
        costs=costs,
    )

    position[
        "force_exit_filled_quantity"
    ] = confirmed_quantity
    position["force_exit_average_price"] = (
        exit_price
    )
    position["last_exit_price"] = exit_price
    position["last_exit_pnl"] = pnl

    if remaining_quantity == 0:
        del open_positions[symbol]
    else:
        position["qty"] = remaining_quantity

    save_positions(
        open_positions,
        positions_path=positions_path,
    )
    finalize_terminal_force_exit()

    logger.info(
        f"FORCE EXIT applied for "
        f"{exchange}:{symbol}: "
        f"confirmed={confirmed_quantity}/"
        f"{requested_quantity}, "
        f"exit={exit_price:.2f}, "
        f"remaining={remaining_quantity}, "
        f"net P&L={pnl:.2f}, "
        f"status={exit_result.get('status')}"
    )

    if remaining_quantity == 0:
        return (
            f"FORCE CLOSED @ {exit_price:.2f} | "
            f"qty {confirmed_quantity} | "
            f"P&L {pnl:+.2f}"
        )

    return (
        f"FORCE PARTIAL EXIT @ {exit_price:.2f} | "
        f"closed {confirmed_quantity}/"
        f"{requested_quantity} | "
        f"remaining {remaining_quantity} | "
        f"P&L {pnl:+.2f}"
    )


def recover_unresolved_exits(
    kite,
    open_positions,
    risk,
    cfg,
    positions_path=None,
):
    """
    Stage 4 restart recovery for unresolved normal EXIT operations.

    FORCE_EXIT is intentionally excluded until Stage 5.

    The broker's filled_quantity is cumulative. The local position
    stores how much of that cumulative quantity has already been
    applied for the current exit operation. Recovery therefore applies:

        newly_confirmed =
            broker_cumulative_fill - locally_applied_fill

    Repeated recovery with no additional broker fill applies zero,
    making position reduction and P&L recording idempotent.
    """

    if getattr(cfg, "PAPER_TRADING", False):
        return

    from pending_order_store import (
        list_unresolved_orders,
        update_order_verification,
        mark_order_resolved,
    )
    from order_verification import verify_order_execution

    unresolved_exits = [
        order
        for order in list_unresolved_orders()
        if order["action"] == "EXIT"
    ]

    if not unresolved_exits:
        return

    logger.info(
        f"Restart recovery: {len(unresolved_exits)} "
        f"unresolved normal EXIT operation(s) found"
    )

    for operation in unresolved_exits:
        operation_id = operation["operation_id"]
        order_id = operation.get("order_id")
        symbol = operation["symbol"]
        exchange = operation["exchange"]
        requested_quantity = int(
            operation["requested_quantity"]
        )

        if order_id is None:
            logger.error(
                f"CRITICAL: unresolved EXIT intent for {symbol} "
                f"has no broker order_id "
                f"(operation_id={operation_id}). "
                f"Submission outcome is unknown. "
                f"Do not resubmit automatically; manual broker "
                f"reconciliation is required."
            )
            continue

        logger.info(
            f"Resuming verification for unresolved EXIT: "
            f"{exchange}:{symbol} "
            f"(order_id={order_id}, "
            f"operation_id={operation_id})"
        )

        execution = verify_order_execution(
            kite,
            order_id,
            requested_quantity,
            max_wait_seconds=getattr(
                cfg,
                "ORDER_VERIFY_MAX_WAIT_SECONDS",
                15,
            ),
            poll_interval_seconds=getattr(
                cfg,
                "ORDER_VERIFY_POLL_INTERVAL_SECONDS",
                1,
            ),
        )

        update_order_verification(
            operation_id,
            execution,
        )

        total_confirmed = int(
            execution.filled_quantity or 0
        )

        position = open_positions.get(symbol)

        # The normal full-close path records the trade and saves the
        # removed position before resolving the pending operation.
        # Therefore, a missing local position with a full cumulative
        # broker fill means the local full close may already have been
        # applied immediately before a crash.
        if position is None:
            if total_confirmed == requested_quantity:
                if execution.terminal:
                    mark_order_resolved(
                        operation_id,
                        resolution_reason=execution.status,
                    )
                    logger.info(
                        f"Recovered EXIT for {symbol}: local position "
                        f"is already absent and broker confirms the "
                        f"full {total_confirmed}/{requested_quantity} "
                        f"terminal fill. Pending operation resolved "
                        f"without applying or logging the fill again."
                    )
                else:
                    logger.warning(
                        f"EXIT for {symbol} has no local position and "
                        f"the full quantity is confirmed, but broker "
                        f"status remains non-terminal "
                        f"({execution.status}). Leaving unresolved."
                    )
            else:
                logger.error(
                    f"CRITICAL: unresolved EXIT for {symbol} has no "
                    f"local position, but broker confirms only "
                    f"{total_confirmed}/{requested_quantity}. "
                    f"Automatic recovery cannot safely reconstruct "
                    f"the remaining position. Leaving unresolved for "
                    f"manual reconciliation."
                )

            continue

        same_operation = (
            position.get("exit_operation_id")
            == operation_id
        )

        if same_operation:
            already_applied = int(
                position.get(
                    "exit_filled_quantity",
                    0,
                )
                or 0
            )
        else:
            # The process may have crashed after broker verification
            # but before saving any exit metadata to the position.
            already_applied = 0

        newly_confirmed = (
            total_confirmed - already_applied
        )

        if newly_confirmed < 0:
            logger.error(
                f"CRITICAL: EXIT recovery quantity moved backwards "
                f"for {symbol}: broker total={total_confirmed}, "
                f"locally applied={already_applied}. "
                f"No local changes made."
            )
            continue

        current_quantity = int(
            position.get("qty", 0) or 0
        )

        if newly_confirmed > current_quantity:
            logger.error(
                f"CRITICAL: EXIT recovery for {symbol} would apply "
                f"{newly_confirmed} shares, but the local remaining "
                f"position contains only {current_quantity}. "
                f"No automatic quantity change made."
            )

            position["manual_reconciliation_required"] = True
            position["exit_status_message"] = (
                "broker exit fill exceeds local remaining quantity"
            )
            save_positions(
                open_positions,
                positions_path=positions_path,
            )
            continue

        position["exit_order_id"] = order_id
        position["exit_operation_id"] = operation_id
        position["exit_requested_quantity"] = (
            requested_quantity
        )
        position["exit_fill_status"] = execution.status
        position["exit_confirmation_pending"] = (
            not execution.terminal
        )
        position["exit_status_message"] = (
            execution.status_message
        )

        if newly_confirmed == 0:
            save_positions(
                open_positions,
                positions_path=positions_path,
            )

            if execution.terminal:
                mark_order_resolved(
                    operation_id,
                    resolution_reason=execution.status,
                )
                position["exit_confirmation_pending"] = False

                save_positions(
                    open_positions,
                    positions_path=positions_path,
                )

                logger.info(
                    f"Resolved recovered EXIT for {symbol}: "
                    f"{execution.status}; no new fill needed "
                    f"applying (already applied "
                    f"{already_applied}/{requested_quantity})"
                )
            else:
                logger.info(
                    f"Recovery for EXIT {symbol}: no new broker "
                    f"fills since the previous application "
                    f"(idempotent; cumulative fill remains "
                    f"{total_confirmed}/{requested_quantity})"
                )

            continue

        cumulative_average = execution.average_price

        if cumulative_average is None:
            remaining_quantity = (
                current_quantity - newly_confirmed
            )

            position["qty"] = remaining_quantity
            position["exit_filled_quantity"] = (
                total_confirmed
            )
            position["exit_average_price"] = None
            position["manual_reconciliation_required"] = True
            position["exit_status_message"] = (
                "confirmed recovered exit fill has no broker "
                "average price; exposure was reduced but P&L "
                "was not recorded"
            )

            save_positions(
                open_positions,
                positions_path=positions_path,
            )

            logger.error(
                f"CRITICAL: recovery confirmed "
                f"{newly_confirmed} new exit shares for {symbol}, "
                f"but no broker average price was available. "
                f"Remaining local quantity={remaining_quantity}. "
                f"P&L was not fabricated or recorded. "
                f"Operation remains unresolved for manual review."
            )
            continue

        cumulative_average = float(
            cumulative_average
        )

        # Kite reports a cumulative average for all fills belonging
        # to the order. When previous partial fills were already
        # applied, derive the price of only the newly confirmed fill:
        #
        # new fill notional =
        #   latest cumulative notional - previous cumulative notional
        if already_applied == 0:
            incremental_exit_price = (
                cumulative_average
            )
        else:
            previous_average = position.get(
                "exit_average_price"
            )

            if previous_average is None:
                remaining_quantity = (
                    current_quantity - newly_confirmed
                )

                position["qty"] = remaining_quantity
                position["exit_filled_quantity"] = (
                    total_confirmed
                )
                position["exit_average_price"] = (
                    cumulative_average
                )
                position[
                    "manual_reconciliation_required"
                ] = True
                position["exit_status_message"] = (
                    "previous cumulative exit average is missing; "
                    "incremental recovery P&L cannot be calculated"
                )

                save_positions(
                    open_positions,
                    positions_path=positions_path,
                )

                logger.error(
                    f"CRITICAL: recovery applied "
                    f"{newly_confirmed} new exit shares for "
                    f"{symbol}, but the previous cumulative "
                    f"average price is unavailable. "
                    f"P&L was not fabricated. "
                    f"Operation remains unresolved."
                )
                continue

            previous_average = float(
                previous_average
            )

            latest_notional = (
                total_confirmed
                * cumulative_average
            )

            previous_notional = (
                already_applied
                * previous_average
            )

            incremental_notional = (
                latest_notional
                - previous_notional
            )

            incremental_exit_price = (
                incremental_notional
                / newly_confirmed
            )

            if incremental_exit_price <= 0:
                logger.error(
                    f"CRITICAL: derived invalid incremental exit "
                    f"price {incremental_exit_price} for {symbol}. "
                    f"No local change made."
                )
                continue

        direction = position["direction"]
        entry_price = float(position["entry"])

        cost_result = net_pnl_for_trade(
            direction,
            newly_confirmed,
            entry_price,
            incremental_exit_price,
        )

        gross_pnl = cost_result["gross_pnl"]
        costs = cost_result["costs"]
        pnl = cost_result["net_pnl"]

        exit_reason = (
            position.get("exit_reason")
            or "exit_recovery"
        )

        risk.record_trade_result(pnl)

        record_trade(
            symbol,
            direction,
            newly_confirmed,
            entry_price,
            incremental_exit_price,
            pnl,
            exit_reason,
            exchange=exchange,
            gross_pnl=gross_pnl,
            costs=costs,
        )

        remaining_quantity = (
            current_quantity - newly_confirmed
        )

        position["qty"] = remaining_quantity
        position["exit_filled_quantity"] = (
            total_confirmed
        )
        position["exit_average_price"] = (
            cumulative_average
        )
        position["last_exit_price"] = (
            incremental_exit_price
        )
        position["last_exit_pnl"] = pnl

        if remaining_quantity == 0:
            del open_positions[symbol]

        save_positions(
            open_positions,
            positions_path=positions_path,
        )

        # Resolution happens only after both confirmed P&L and the
        # revised local position have been applied and persisted.
        if execution.terminal:
            mark_order_resolved(
                operation_id,
                resolution_reason=execution.status,
            )

        logger.info(
            f"Recovered EXIT fill for {exchange}:{symbol}: "
            f"newly_confirmed={newly_confirmed}, "
            f"broker_total={total_confirmed}/"
            f"{requested_quantity}, "
            f"incremental_exit_price="
            f"{incremental_exit_price:.2f}, "
            f"remaining_quantity={remaining_quantity}, "
            f"net P&L={pnl:.2f}, "
            f"status={execution.status}, "
            f"terminal={execution.terminal}"
        )




def recover_unresolved_force_exits(
    kite,
    open_positions,
    risk,
    cfg,
    positions_path=None,
):
    """
    Stage 5 restart recovery for unresolved FORCE_EXIT operations.

    Recovery never submits a replacement order. It reads the broker
    history for the already-persisted order ID and applies only the
    newly confirmed cumulative fill delta.

    Repeated recovery with no additional broker fill applies zero,
    preventing duplicate quantity reduction and duplicate P&L.
    """

    if getattr(cfg, "PAPER_TRADING", False):
        return

    from pending_order_store import (
        list_unresolved_orders,
        update_order_verification,
        mark_order_resolved,
    )
    from order_verification import verify_order_execution

    unresolved_force_exits = [
        operation
        for operation in list_unresolved_orders()
        if operation["action"] == "FORCE_EXIT"
    ]

    if not unresolved_force_exits:
        return

    logger.info(
        f"Restart recovery: {len(unresolved_force_exits)} "
        f"unresolved FORCE_EXIT operation(s) found"
    )

    for operation in unresolved_force_exits:
        operation_id = operation["operation_id"]
        order_id = operation.get("order_id")
        symbol = operation["symbol"]
        exchange = operation["exchange"]
        requested_quantity = int(
            operation["requested_quantity"]
        )

        if order_id is None:
            logger.error(
                f"CRITICAL: unresolved FORCE_EXIT intent for "
                f"{symbol} has no broker order_id "
                f"(operation_id={operation_id}). "
                f"Submission outcome is unknown. "
                f"Do not submit another order automatically; "
                f"manual broker reconciliation is required."
            )
            continue

        logger.info(
            f"Resuming verification for unresolved FORCE_EXIT: "
            f"{exchange}:{symbol} "
            f"(order_id={order_id}, "
            f"operation_id={operation_id})"
        )

        execution = verify_order_execution(
            kite,
            order_id,
            requested_quantity,
            max_wait_seconds=getattr(
                cfg,
                "ORDER_VERIFY_MAX_WAIT_SECONDS",
                15,
            ),
            poll_interval_seconds=getattr(
                cfg,
                "ORDER_VERIFY_POLL_INTERVAL_SECONDS",
                1,
            ),
        )

        update_order_verification(
            operation_id,
            execution,
        )

        total_confirmed = int(
            execution.filled_quantity or 0
        )

        position = open_positions.get(symbol)

        # A crash may happen after a full local close was saved but
        # immediately before the pending operation was marked resolved.
        # In that case the missing position plus a full terminal broker
        # fill means there is nothing left to apply.
        if position is None:
            if (
                total_confirmed == requested_quantity
                and execution.terminal
            ):
                mark_order_resolved(
                    operation_id,
                    resolution_reason=execution.status,
                )

                logger.info(
                    f"Recovered FORCE_EXIT for {symbol}: "
                    f"local position is already absent and broker "
                    f"confirms terminal full fill "
                    f"{total_confirmed}/{requested_quantity}. "
                    f"Resolved without recording the fill again."
                )
            else:
                logger.error(
                    f"CRITICAL: unresolved FORCE_EXIT for {symbol} "
                    f"has no local position, while broker reports "
                    f"{total_confirmed}/{requested_quantity} "
                    f"(status={execution.status}). "
                    f"Automatic reconstruction is unsafe; "
                    f"leaving unresolved for manual reconciliation."
                )

            continue

        same_operation = (
            position.get("force_exit_operation_id")
            == operation_id
        )

        if same_operation:
            already_applied = int(
                position.get(
                    "force_exit_filled_quantity",
                    0,
                )
                or 0
            )
        else:
            # The process may have crashed before force-exit metadata
            # was written to the position.
            already_applied = 0

        newly_confirmed = (
            total_confirmed - already_applied
        )

        if newly_confirmed < 0:
            position[
                "manual_reconciliation_required"
            ] = True
            position["force_exit_status_message"] = (
                "broker cumulative force-exit quantity is below "
                "the quantity already applied locally"
            )

            save_positions(
                open_positions,
                positions_path=positions_path,
            )

            logger.critical(
                f"FORCE_EXIT recovery quantity moved backwards "
                f"for {symbol}: broker total={total_confirmed}, "
                f"locally applied={already_applied}. "
                f"No quantity change made."
            )
            continue

        current_quantity = int(
            position.get("qty", 0) or 0
        )

        if newly_confirmed > current_quantity:
            position[
                "manual_reconciliation_required"
            ] = True
            position["force_exit_status_message"] = (
                "newly confirmed force-exit quantity exceeds "
                "the remaining local position"
            )

            save_positions(
                open_positions,
                positions_path=positions_path,
            )

            logger.critical(
                f"FORCE_EXIT recovery for {symbol} would apply "
                f"{newly_confirmed} shares, but the local position "
                f"contains only {current_quantity}. "
                f"No automatic quantity change made."
            )
            continue

        position["force_exit_order_id"] = order_id
        position["force_exit_operation_id"] = operation_id
        position["force_exit_requested_quantity"] = (
            requested_quantity
        )
        position["force_exit_fill_status"] = execution.status
        position[
            "force_exit_confirmation_pending"
        ] = not execution.terminal
        position["force_exit_status_message"] = (
            execution.status_message
        )
        position["force_exit_reason"] = "square_off"

        if newly_confirmed == 0:
            save_positions(
                open_positions,
                positions_path=positions_path,
            )

            if execution.terminal:
                mark_order_resolved(
                    operation_id,
                    resolution_reason=execution.status,
                )

                position[
                    "force_exit_confirmation_pending"
                ] = False

                save_positions(
                    open_positions,
                    positions_path=positions_path,
                )

                logger.info(
                    f"Resolved recovered FORCE_EXIT for {symbol}: "
                    f"{execution.status}; no new fill remained to "
                    f"apply (already applied "
                    f"{already_applied}/{requested_quantity})"
                )
            else:
                logger.info(
                    f"FORCE_EXIT recovery for {symbol}: no new "
                    f"broker fills since the previous application "
                    f"(idempotent; cumulative fill remains "
                    f"{total_confirmed}/{requested_quantity})"
                )

            continue

        cumulative_average = execution.average_price

        if cumulative_average is None:
            remaining_quantity = (
                current_quantity - newly_confirmed
            )

            position["qty"] = remaining_quantity
            position[
                "force_exit_filled_quantity"
            ] = total_confirmed
            position["force_exit_average_price"] = None
            position[
                "manual_reconciliation_required"
            ] = True
            position["force_exit_status_message"] = (
                "confirmed recovered force-exit fill has no "
                "broker average price; exposure was reduced but "
                "P&L was not recorded"
            )

            if remaining_quantity == 0:
                del open_positions[symbol]

            save_positions(
                open_positions,
                positions_path=positions_path,
            )

            logger.critical(
                f"Recovery confirmed {newly_confirmed} new "
                f"FORCE_EXIT shares for {symbol}, but the broker "
                f"average price is unavailable. "
                f"Remaining quantity={remaining_quantity}. "
                f"P&L was not fabricated. Operation remains "
                f"unresolved for reconciliation."
            )
            continue

        cumulative_average = float(
            cumulative_average
        )

        # Kite returns the cumulative average for every fill in the
        # broker order. Derive the execution price of only the newly
        # confirmed fill quantity.
        if already_applied == 0:
            incremental_exit_price = (
                cumulative_average
            )
        else:
            previous_average = position.get(
                "force_exit_average_price"
            )

            if previous_average is None:
                remaining_quantity = (
                    current_quantity - newly_confirmed
                )

                position["qty"] = remaining_quantity
                position[
                    "force_exit_filled_quantity"
                ] = total_confirmed
                position["force_exit_average_price"] = (
                    cumulative_average
                )
                position[
                    "manual_reconciliation_required"
                ] = True
                position["force_exit_status_message"] = (
                    "previous cumulative force-exit average is "
                    "missing; incremental P&L cannot be calculated"
                )

                if remaining_quantity == 0:
                    del open_positions[symbol]

                save_positions(
                    open_positions,
                    positions_path=positions_path,
                )

                logger.critical(
                    f"Recovery applied {newly_confirmed} new "
                    f"FORCE_EXIT shares for {symbol}, but the "
                    f"previous cumulative average is unavailable. "
                    f"P&L was not fabricated."
                )
                continue

            previous_average = float(
                previous_average
            )

            latest_notional = (
                total_confirmed
                * cumulative_average
            )
            previous_notional = (
                already_applied
                * previous_average
            )
            incremental_notional = (
                latest_notional
                - previous_notional
            )
            incremental_exit_price = (
                incremental_notional
                / newly_confirmed
            )

            if incremental_exit_price <= 0:
                position[
                    "manual_reconciliation_required"
                ] = True
                position["force_exit_status_message"] = (
                    "derived incremental force-exit price is invalid"
                )

                save_positions(
                    open_positions,
                    positions_path=positions_path,
                )

                logger.critical(
                    f"Derived invalid incremental FORCE_EXIT "
                    f"price {incremental_exit_price} for {symbol}. "
                    f"No local quantity change made."
                )
                continue

        direction = position["direction"]
        entry_price = float(position["entry"])

        cost_result = net_pnl_for_trade(
            direction,
            newly_confirmed,
            entry_price,
            incremental_exit_price,
        )

        gross_pnl = cost_result["gross_pnl"]
        costs = cost_result["costs"]
        pnl = cost_result["net_pnl"]

        risk.record_trade_result(pnl)

        record_trade(
            symbol,
            direction,
            newly_confirmed,
            entry_price,
            incremental_exit_price,
            pnl,
            "square_off",
            exchange=exchange,
            gross_pnl=gross_pnl,
            costs=costs,
        )

        remaining_quantity = (
            current_quantity - newly_confirmed
        )

        position["qty"] = remaining_quantity
        position[
            "force_exit_filled_quantity"
        ] = total_confirmed
        position["force_exit_average_price"] = (
            cumulative_average
        )
        position["last_exit_price"] = (
            incremental_exit_price
        )
        position["last_exit_pnl"] = pnl

        if remaining_quantity == 0:
            del open_positions[symbol]

        save_positions(
            open_positions,
            positions_path=positions_path,
        )

        # Resolve only after confirmed quantity and P&L have been
        # applied and persisted.
        if execution.terminal:
            mark_order_resolved(
                operation_id,
                resolution_reason=execution.status,
            )

        logger.info(
            f"Recovered FORCE_EXIT fill for "
            f"{exchange}:{symbol}: "
            f"newly_confirmed={newly_confirmed}, "
            f"broker_total={total_confirmed}/"
            f"{requested_quantity}, "
            f"incremental_exit_price="
            f"{incremental_exit_price:.2f}, "
            f"remaining_quantity={remaining_quantity}, "
            f"net P&L={pnl:.2f}, "
            f"status={execution.status}, "
            f"terminal={execution.terminal}"
        )


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

    if not cfg.PAPER_TRADING:
        recover_unresolved_entries(
            kite,
            open_positions,
            cfg,
        )
        recover_unresolved_exits(
            kite,
            open_positions,
            risk,
            cfg,
        )
        recover_unresolved_force_exits(
            kite,
            open_positions,
            risk,
            cfg,
        )

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

        # Force square-off at end of day regardless
        # of signals. Only broker-confirmed quantities are
        # removed locally.
        if past_square_off():
            for symbol, pos in list(
                open_positions.items()
            ):
                logger.info(
                    f"Force square-off: {symbol}"
                )

                exchange = pos.get(
                    "exchange",
                    exchange_map.get(symbol, "NSE"),
                )
                token = tokens[symbol]

                try:
                    df_5m = fetch_candles(
                        kite,
                        token,
                        cfg.ENTRY_TIMEFRAME,
                        lookback_days=1,
                        trim_incomplete=False,
                    )
                    last_price = (
                        df_5m.iloc[-1]["close"]
                        if not df_5m.empty
                        else pos["entry"]
                    )
                except Exception:
                    last_price = pos["entry"]

                requested_quantity = int(
                    pos["qty"]
                )

                force_result = (
                    place_force_exit_order(
                        kite,
                        symbol,
                        pos["direction"],
                        requested_quantity,
                        exchange,
                        cfg,
                    )
                )

                status = apply_force_exit_result(
                    symbol,
                    pos,
                    force_result,
                    open_positions,
                    risk,
                    exchange,
                    last_price,
                )

                logger.info(
                    f"Force square-off result for "
                    f"{exchange}:{symbol}: {status}"
                )

            if open_positions:
                save_positions(open_positions)

                logger.critical(
                    "Trading day ended with unresolved "
                    "or partially closed positions still "
                    f"persisted: "
                    f"{list(open_positions.keys())}. "
                    "They will be reconciled by restart "
                    "recovery; positions were NOT cleared."
                )
            else:
                clear_positions()

            logger.info(
                "Trading day complete. Exiting."
            )
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
