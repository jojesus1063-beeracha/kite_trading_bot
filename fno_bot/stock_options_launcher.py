"""Market-wide Opening Scalper for all NSE stock options, ATM pair only.

The original single-index launcher remains unchanged.  This opt-in launcher
discovers today's stock-option universe from Kite's instrument dumps, streams
each equity underlying plus its nearest-expiry ATM CE/PE pair, ranks authorized
signals across the universe, and permits at most one PAPER position at a time.

LIVE is deliberately blocked until multi-underlying PAPER evidence is reviewed.
"""
import logging
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fno_bot import config as cfg
from fno_bot.audit.event_log import log_event
from fno_bot.execution.position_store import load_positions
from fno_bot.instruments.contract_master import load_contract_master
from fno_bot.instruments.stock_option_universe import (
    StockOptionPair,
    discover_stock_option_underlyings,
    select_all_atm_pairs,
)
from fno_bot.launcher import (
    _get_kite_client,
    _today_at,
    get_broker,
    run_entry,
    run_monitor_and_exit,
)
from fno_bot.market_data.tick_store import TickStore
from fno_bot.market_data.ticker import FnoTicker
from fno_bot.market_data.historical_candles import HistoricalCandleCache
from fno_bot.strategies.intraday_momentum import evaluate_intraday_momentum
from fno_bot.strategies.opening_scalper import build_snapshot, evaluate_signals
from fno_bot.strategies.session_router import TradingSession, route_session
from fno_bot.strategies.signal_candidates import TickPoint

logger = logging.getLogger("fno.stock_options_launcher")
IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class RankedCandidate:
    pair: StockOptionPair
    authorized_result: object
    confidence: float
    max_spread_pct: float
    previous_close: Optional[float]


def _chunks(values: list, size: int):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _fetch_previous_closes(kite, symbols: list[str]) -> dict[str, Optional[float]]:
    """Kite quote supports bounded batches; missing symbols abstain safely."""
    out: dict[str, Optional[float]] = {symbol: None for symbol in symbols}
    for batch in _chunks(symbols, 500):
        keys = [f"NSE:{symbol}" for symbol in batch]
        try:
            response = kite.quote(keys)
        except Exception as exc:
            logger.warning("previous-close batch failed: %s", exc)
            continue
        for symbol in batch:
            quote = response.get(f"NSE:{symbol}") or {}
            close = (quote.get("ohlc") or {}).get("close")
            if close is not None and float(close) > 0:
                out[symbol] = float(close)
    return out


def _wait_until(moment: datetime, sleep_fn=time.sleep):
    while datetime.now(IST) < moment:
        sleep_fn(0.25)


def _wait_for_any_ticks(tick_store: TickStore, tokens: list[int], timeout_seconds: float) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        count = sum(1 for token in tokens if tick_store.has_tick(token))
        if count == len(tokens):
            return count
        time.sleep(0.1)
    return sum(1 for token in tokens if tick_store.has_tick(token))


def rank_candidates(
    tick_store: TickStore,
    pairs: list[StockOptionPair],
    previous_closes: dict[str, Optional[float]],
    option_histories=None,
    confirmation_streaks=None,
    audit_fn=None,
) -> list[RankedCandidate]:
    """Returns fresh, spread-valid authorized candidates in best-first order."""
    ranked = []
    for pair in pairs:
        underlying = pair.underlying
        selection = pair.selection
        tokens = [
            underlying.instrument_token,
            selection.ce_contract.instrument_token,
            selection.pe_contract.instrument_token,
        ]
        if not all(tick_store.is_fresh(token, cfg.MAX_TICK_AGE_MS) for token in tokens):
            continue

        ce_spread = tick_store.spread_pct(tokens[1])
        pe_spread = tick_store.spread_pct(tokens[2])
        if ce_spread is None or pe_spread is None:
            continue
        max_spread = max(ce_spread, pe_spread)
        professional = cfg.AUTHORIZED_SIGNAL == "professional_momentum"
        spread_limit = min(cfg.MAX_SPREAD_PCT, 1.5) if professional else cfg.MAX_SPREAD_PCT
        if max_spread > spread_limit:
            continue
        if professional and selection.expiry <= date.today():
            continue

        snapshot = build_snapshot(
            tick_store, underlying.instrument_token, tokens[1], tokens[2],
            previous_closes.get(underlying.symbol),
            ce_history=tuple((option_histories or {}).get(tokens[1], ())),
            pe_history=tuple((option_histories or {}).get(tokens[2], ())),
            underlying_history=tuple((option_histories or {}).get(tokens[0], ())),
        )
        if snapshot is None:
            continue
        all_results, authorized = evaluate_signals(snapshot, cfg.AUTHORIZED_SIGNAL)
        observed = next((item for item in all_results if item.candidate == cfg.AUTHORIZED_SIGNAL), None)
        if audit_fn is not None and observed is not None:
            audit_fn(
                "PROFESSIONAL_SIGNAL_EVALUATED",
                symbol=underlying.symbol,
                direction=observed.direction,
                confidence=observed.confidence,
                reason=observed.reason,
                metrics=observed.raw_metrics,
                max_spread_pct=max_spread,
            )
        if authorized is None or authorized.confidence is None:
            if confirmation_streaks is not None:
                confirmation_streaks[underlying.symbol] = 0
            continue
        selected_tick = tick_store.latest(tokens[1] if authorized.direction == "CE" else tokens[2])
        selected_contract = selection.ce_contract if authorized.direction == "CE" else selection.pe_contract
        if professional and (
            selected_tick.last_price < 20.0
            or selected_tick.best_bid_qty is None
            or selected_tick.best_bid_qty < selected_contract.lot_size
        ):
            if confirmation_streaks is not None:
                confirmation_streaks[underlying.symbol] = 0
            continue
        if professional and confirmation_streaks is not None:
            confirmation_streaks[underlying.symbol] += 1
            if confirmation_streaks[underlying.symbol] < 3:
                continue
        ranked.append(RankedCandidate(
            pair=pair,
            authorized_result=authorized,
            confidence=float(authorized.confidence),
            max_spread_pct=max_spread,
            previous_close=previous_closes.get(underlying.symbol),
        ))

    return sorted(
        ranked,
        key=lambda item: (-item.confidence, item.max_spread_pct, item.pair.underlying.symbol),
    )


def discover_and_subscribe_underlyings(kite, ticker: FnoTicker):
    nfo_records = load_contract_master(kite, "NFO", as_of=date.today())
    nse_instruments = kite.instruments("NSE")
    underlyings = discover_stock_option_underlyings(nfo_records, nse_instruments, date.today())
    if cfg.ALL_STOCK_OPTIONS_MAX_UNDERLYINGS > 0:
        underlyings = underlyings[:cfg.ALL_STOCK_OPTIONS_MAX_UNDERLYINGS]
    if not underlyings:
        raise RuntimeError("no NSE stock-option underlyings discovered from today's instrument dumps")
    underlying_tokens = [item.instrument_token for item in underlyings]
    ticker.subscribe(underlying_tokens, mode="ltp")
    previous_closes = _fetch_previous_closes(kite, [item.symbol for item in underlyings])
    return underlyings, previous_closes


def select_and_subscribe_atm_pairs(
    ticker: FnoTicker,
    tick_store: TickStore,
    underlyings,
    previous_closes,
):
    underlying_tokens = [item.instrument_token for item in underlyings]
    received = _wait_for_any_ticks(tick_store, underlying_tokens, timeout_seconds=30)
    spot_by_token = {
        token: tick_store.latest(token).last_price
        for token in underlying_tokens if tick_store.has_tick(token)
    }
    pairs = select_all_atm_pairs(underlyings, spot_by_token)
    if not pairs:
        raise RuntimeError("underlying ticks arrived but no complete nearest-expiry ATM CE/PE pairs were selectable")

    option_tokens = []
    for pair in pairs:
        option_tokens.extend([
            pair.selection.ce_contract.instrument_token,
            pair.selection.pe_contract.instrument_token,
        ])
    total_subscriptions = len(set(underlying_tokens + option_tokens))
    if total_subscriptions > cfg.ALL_STOCK_OPTIONS_WEBSOCKET_LIMIT:
        raise RuntimeError(
            f"subscription count {total_subscriptions} exceeds configured Kite limit "
            f"{cfg.ALL_STOCK_OPTIONS_WEBSOCKET_LIMIT}"
        )
    ticker.subscribe(option_tokens, mode="full")
    option_ticks_received = _wait_for_any_ticks(tick_store, option_tokens, timeout_seconds=30)
    log_event(
        "STOCK_OPTION_UNIVERSE_READY",
        discovered_underlyings=len(underlyings),
        selected_atm_pairs=len(pairs),
        underlying_ticks_received=received,
        option_ticks_received=option_ticks_received,
        subscriptions=total_subscriptions,
    )
    return pairs, previous_closes


def build_signal_validator(pair, tick_store, histories, previous_close, expected_direction):
    """Require three consecutive live failures before invalidating a position."""
    invalid_streak = 0

    def validate():
        nonlocal invalid_streak
        sampled_at = time.monotonic()
        tokens = (
            pair.underlying.instrument_token,
            pair.selection.ce_contract.instrument_token,
            pair.selection.pe_contract.instrument_token,
        )
        for token in tokens:
            tick = tick_store.latest(token)
            history = histories[token]
            if tick is not None and (not history or sampled_at - history[-1].at_monotonic >= 0.8):
                history.append(TickPoint(
                    tick.last_price, sampled_at, tick.volume, tick.open_interest,
                    tick.total_buy_qty, tick.total_sell_qty,
                ))
        snapshot = build_snapshot(
            tick_store, tokens[0], tokens[1], tokens[2], previous_close,
            ce_history=tuple(histories[tokens[1]]),
            pe_history=tuple(histories[tokens[2]]),
            underlying_history=tuple(histories[tokens[0]]),
        )
        if snapshot is None:
            invalid_streak += 1
        else:
            _, current = evaluate_signals(snapshot, cfg.AUTHORIZED_SIGNAL)
            invalid_streak = 0 if current is not None and current.direction == expected_direction else invalid_streak + 1
        return invalid_streak < 3

    return validate


def rank_intraday_candidates(
    live_ranked,
    tick_store,
    histories,
    candle_cache,
    audit_fn=None,
):
    """Apply completed-candle confirmation only to the live shortlist.

    This bounded funnel prevents historical API calls across the entire F&O
    universe. The cache guarantees at most one request per shortlisted symbol
    per minute.
    """
    confirmed = []
    for candidate in live_ranked[:cfg.INTRADAY_HISTORICAL_SHORTLIST_SIZE]:
        pair = candidate.pair
        tokens = (
            pair.underlying.instrument_token,
            pair.selection.ce_contract.instrument_token,
            pair.selection.pe_contract.instrument_token,
        )
        snapshot = build_snapshot(
            tick_store, tokens[0], tokens[1], tokens[2], candidate.previous_close,
            ce_history=tuple(histories[tokens[1]]),
            pe_history=tuple(histories[tokens[2]]),
            underlying_history=tuple(histories[tokens[0]]),
        )
        if snapshot is None:
            continue
        try:
            candles = candle_cache.completed_minute_candles(tokens[0])
            decision = evaluate_intraday_momentum(candles, snapshot)
        except Exception as exc:
            if audit_fn:
                audit_fn("INTRADAY_DATA_REJECTED", symbol=pair.underlying.symbol, reason=str(exc))
            continue
        if audit_fn:
            audit_fn(
                "INTRADAY_SIGNAL_EVALUATED",
                symbol=pair.underlying.symbol,
                direction=decision.direction,
                confidence=decision.confidence,
                reason=decision.reason,
                metrics=decision.metrics,
            )
        if decision.direction is None or decision.confidence is None:
            continue
        confirmed.append(RankedCandidate(
            pair=pair,
            authorized_result=decision,
            confidence=float(decision.confidence),
            max_spread_pct=candidate.max_spread_pct,
            previous_close=candidate.previous_close,
        ))
    return sorted(confirmed, key=lambda item: (-item.confidence, item.max_spread_pct,
                                               item.pair.underlying.symbol))


def run_all_stock_options():
    cfg.validate_mode()
    if cfg.MODE == "LIVE" and not cfg.ALL_STOCK_OPTIONS_LIVE_ENABLED:
        raise RuntimeError("ALL_STOCK_OPTIONS is PAPER/SHADOW only; LIVE is structurally disabled")
    if load_positions():
        raise RuntimeError("local F&O position state is not empty; refusing a new market-wide session")

    kite = _get_kite_client()
    tick_store = TickStore()
    with open(cfg.ACCESS_TOKEN_FILE) as handle:
        access_token = handle.read().strip()
    ticker = FnoTicker(cfg.API_KEY, access_token, tick_store=tick_store)
    ticker.connect(threaded=True)
    if not ticker.wait_connected(timeout_seconds=15):
        raise RuntimeError("WebSocket did not connect within 15 seconds")

    try:
        # Contract discovery, previous-close snapshots, and underlying
        # subscriptions happen before 09:15 so the five-minute decision
        # window is not consumed by large instrument/quote requests.
        underlyings, previous_closes = discover_and_subscribe_underlyings(kite, ticker)
        _wait_until(_today_at(cfg.ENTRY_START_TIME))
        pairs, previous_closes = select_and_subscribe_atm_pairs(
            ticker, tick_store, underlyings, previous_closes
        )
        broker = get_broker(kite, tick_store) if cfg.MODE == "PAPER" else None
        opening_end = _today_at(cfg.ENTRY_END_TIME)
        session_end = _today_at(
            cfg.INTRADAY_ENTRY_END_TIME if cfg.INTRADAY_OPTIONS_ENABLED else cfg.ENTRY_END_TIME
        )
        option_histories = defaultdict(lambda: deque(maxlen=31))
        confirmation_streaks = defaultdict(int)
        cooldown_until = {}
        candle_cache = HistoricalCandleCache(
            kite, ttl_seconds=cfg.INTRADAY_HISTORICAL_CACHE_SECONDS
        )

        while datetime.now(IST) < session_end:
            sampled_at = time.monotonic()
            for pair in pairs:
                for token in (
                    pair.underlying.instrument_token,
                    pair.selection.ce_contract.instrument_token,
                    pair.selection.pe_contract.instrument_token,
                ):
                    tick = tick_store.latest(token)
                    if tick is not None:
                        option_histories[token].append(TickPoint(
                            tick.last_price, sampled_at, tick.volume, tick.open_interest,
                            tick.total_buy_qty, tick.total_sell_qty,
                        ))
            live_ranked = rank_candidates(
                tick_store, pairs, previous_closes, option_histories,
                confirmation_streaks=confirmation_streaks,
                audit_fn=log_event,
            )
            live_ranked = [
                candidate for candidate in live_ranked
                if time.monotonic() >= cooldown_until.get(candidate.pair.underlying.symbol, 0)
            ]
            session = route_session(datetime.now(IST))
            ranked = live_ranked
            if session == TradingSession.INTRADAY and cfg.INTRADAY_OPTIONS_ENABLED:
                ranked = rank_intraday_candidates(
                    live_ranked, tick_store, option_histories, candle_cache,
                    audit_fn=log_event,
                )
            elif datetime.now(IST) >= opening_end and not cfg.INTRADAY_OPTIONS_ENABLED:
                break
            if ranked:
                top = ranked[0]
                log_event(
                    "STOCK_OPTION_CANDIDATE_RANKED",
                    session=session.value,
                    symbol=top.pair.underlying.symbol,
                    strike=top.pair.selection.atm_strike,
                    expiry=str(top.pair.selection.expiry),
                    direction=top.authorized_result.direction,
                    confidence=top.confidence,
                    max_spread_pct=top.max_spread_pct,
                    eligible_count=len(ranked),
                )
                if cfg.MODE == "PAPER":
                    terminal_kill_switch = False
                    for candidate in ranked:
                        signal_validator = build_signal_validator(
                            candidate.pair, tick_store, option_histories,
                            candidate.previous_close, candidate.authorized_result.direction,
                        ) if cfg.AUTHORIZED_SIGNAL == "professional_momentum" else None
                        position, kill_switch, error = run_entry(
                            broker, cfg, tick_store, candidate.pair.selection,
                            candidate.authorized_result, ticker=ticker,
                            position_key=candidate.pair.underlying.symbol,
                            signal_still_valid_fn=signal_validator,
                            recent_option_prices=[point.price for point in option_histories[
                                candidate.pair.selection.ce_contract.instrument_token
                                if candidate.authorized_result.direction == "CE"
                                else candidate.pair.selection.pe_contract.instrument_token
                            ]],
                            entry_spread_pct=candidate.max_spread_pct,
                        )
                        if position is not None:
                            traded_symbol = candidate.pair.underlying.symbol
                            run_monitor_and_exit(
                                broker, ticker, tick_store, position, kill_switch, cfg,
                                underlying_name=candidate.pair.underlying.symbol,
                                signal_still_valid_fn=signal_validator,
                            )
                            cooldown_until[traded_symbol] = (
                                time.monotonic() + cfg.REENTRY_COOLDOWN_MINUTES * 60
                            )
                            log_event(
                                "REENTRY_COOLDOWN_STARTED",
                                symbol=traded_symbol,
                                cooldown_minutes=cfg.REENTRY_COOLDOWN_MINUTES,
                            )
                            # Histories collected before/during a blocking position
                            # monitor no longer represent a contiguous 30-second
                            # window. Re-warm them before authorizing another trade.
                            option_histories.clear()
                            confirmation_streaks.clear()
                            break
                        if error and "kill switch halted" in str(error):
                            logger.warning("market-wide entry stopped by kill switch: %s", error)
                            terminal_kill_switch = True
                            break
                    if terminal_kill_switch:
                        return
            time.sleep(1.0)
    finally:
        ticker.close()


def main():
    try:
        run_all_stock_options()
    except Exception as exc:
        log_event("ERROR", reason=f"all-stock-options launcher failed: {exc}")
        logger.exception("All-stock-options launcher failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
