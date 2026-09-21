"""Launcher for the paper CE/PE premium-selling strategy.

No broker order functions are imported or called. LIVE remains refused by
the strategy session and is not enabled by this change.
"""
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import fno_bot.config as cfg
from fno_bot.strategies.premium_rotation import TickSample, RotationParams
from fno_bot.strategies.premium_rotation_gate import EntryParams
from fno_bot.strategies.premium_rotation_exits import ExitParams
from fno_bot.strategies.premium_rotation_session import ShadowSession
from fno_bot.strategies.premium_rotation_state import (
    load_day_state, save_day_state, KillSwitchParams, can_take_new_trade,
    record_trade_result, is_within_opening_protection, should_reselect_atm, DAY_STATE_PATH,
)
from fno_bot.strategies.premium_rotation_costs_log import (
    net_pnl_for_closed_trade, RotationAuditLog, summarize_closed_trades,
)
from fno_bot.strategies.options_selling import otm_strike
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fno.options_selling")
IST = ZoneInfo("Asia/Kolkata")
AUDIT_LOG_PATH_TEMPLATE = "runtime/state/premium_rotation_shadow/audit_{date}.jsonl"
OPENING_PROTECTION_SECONDS = 60.0
ATM_RESELECT_THRESHOLD = 100.0

def get_kite_client():
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=cfg.API_KEY)
    with open(cfg.ACCESS_TOKEN_FILE) as f:
        access_token = f.read().strip()
    kite.set_access_token(access_token)
    return kite

def select_atm_pair(kite, underlying_config: dict, target_date):
    """Select nearest expiry. SELL_PREMIUM uses one modestly-OTM CE and
    PE around the ATM reference; LONG_PREMIUM preserves the old ATM pair."""
    index_symbol = underlying_config["index_symbol"]
    exchange = underlying_config["exchange"]
    index_exchange = underlying_config["index_exchange"]
    strike_interval = underlying_config["strike_interval"]
    underlying_quote = kite.ltp([f"{index_exchange}:{index_symbol}"])[f"{index_exchange}:{index_symbol}"]
    spot = underlying_quote["last_price"]
    instruments = kite.instruments(exchange)
    name = underlying_config.get("name", index_symbol.split()[0])
    opts = [i for i in instruments if i["name"] == name and i["segment"] == f"{exchange}-OPT"]
    if not opts:
        raise RuntimeError(f"no options found for {name} on {exchange}")
    expiries = sorted({i["expiry"] for i in opts if i["expiry"] >= target_date})
    if not expiries:
        raise RuntimeError(f"no valid expiry on/after {target_date}")
    nearest_expiry = expiries[0]
    same_expiry = [i for i in opts if i["expiry"] == nearest_expiry]
    strikes = sorted({i["strike"] for i in same_expiry})
    atm_strike = min(strikes, key=lambda s: abs(s - spot))
    steps = int(getattr(cfg, "SELL_OTM_STEPS", 1))
    if getattr(cfg, "OPTION_STRATEGY", "SELL_PREMIUM") == "SELL_PREMIUM":
        ce_strike = otm_strike(spot, atm_strike, strike_interval, "CE", steps)
        pe_strike = otm_strike(spot, atm_strike, strike_interval, "PE", steps)
    else:
        ce_strike = pe_strike = atm_strike
    ce = next(i for i in same_expiry if i["strike"] == ce_strike and i["instrument_type"] == "CE")
    pe = next(i for i in same_expiry if i["strike"] == pe_strike and i["instrument_type"] == "PE")
    option_quotes = kite.ltp([f"{exchange}:{ce['tradingsymbol']}", f"{exchange}:{pe['tradingsymbol']}"])
    ce_price = option_quotes[f"{exchange}:{ce['tradingsymbol']}"]["last_price"]
    pe_price = option_quotes[f"{exchange}:{pe['tradingsymbol']}"]["last_price"]
    return {
        "spot": spot, "strike": atm_strike, "ce_strike": ce_strike, "pe_strike": pe_strike,
        "expiry": nearest_expiry, "underlying_token": underlying_quote["instrument_token"],
        "ce_token": ce["instrument_token"], "ce_symbol": ce["tradingsymbol"],
        "pe_token": pe["instrument_token"], "pe_symbol": pe["tradingsymbol"],
        "ce_price": ce_price, "pe_price": pe_price, "lot_size": int(ce["lot_size"]),
        "strike_interval": strike_interval,
    }

def build_session(quantity=1):
    rotation = RotationParams(
        ce_momentum_min_pct=1.5,
        pe_weakness_max_pct=0.25,
        velocity_min=3.0,
        both_rising_threshold_pct=2.5,
        both_falling_threshold_pct=-2.5,
        underlying_confirm_min_pct=0.05,
    )
    entry = EntryParams(
        score_threshold=cfg.FNO_SCORE_THRESHOLD,
        dominance_margin=cfg.FNO_DOMINANCE_MARGIN,
        anti_chase_lookback_seconds=cfg.FNO_ANTI_CHASE_LOOKBACK_SECONDS,
        anti_chase_max_extension_pct=cfg.FNO_ANTI_CHASE_MAX_EXTENSION_PCT,
    )
    exits = ExitParams(
        stop_loss_pct=cfg.FNO_HARD_STOP_PCT,
        profit_target_enabled=cfg.FNO_PROFIT_TARGET_ENABLED,
        trailing_activation_pct=cfg.FNO_TRAILING_ACTIVATION_PCT,
        trailing_distance_pct=cfg.FNO_TRAILING_DISTANCE_PCT,
        time_stop_seconds=cfg.FNO_TIME_STOP_SECONDS,
        time_stop_min_progress_pct=cfg.FNO_TIME_STOP_MIN_PROGRESS_PCT,
        momentum_exit_min_profit_pct=cfg.FNO_MOMENTUM_EXIT_MIN_PROFIT_PCT,
        session_cutoff_hhmm=cfg.FNO_EXIT_CUTOFF,
    )
    return ShadowSession(
        rotation, entry, exits,
        window_seconds=cfg.FNO_WINDOW_SECONDS,
        confirmation_required_count=cfg.FNO_CONFIRMATION_COUNT,
        quantity=quantity, mode=cfg.MODE,
        paper_slippage_pct=cfg.PAPER_SLIPPAGE_PCT,
        strategy_mode=getattr(cfg, "OPTION_STRATEGY", "SELL_PREMIUM"),
    )

def run_shadow_session(underlying_name=None, market_start_hour=9, market_start_minute=15):
    if cfg.MODE == "LIVE":
        raise RuntimeError("Options selling launcher is PAPER/SHADOW only; LIVE execution is disabled")
    kite = get_kite_client()
    today = datetime.now(IST).date()
    today_str = str(today)
    underlying_name = underlying_name or cfg.UNDERLYING
    underlying_config = cfg.UNDERLYING_REGISTRY[underlying_name]
    selection = select_atm_pair(kite, underlying_config, today)
    logger.info("OPTION SELLING selection: ATM=%s CE=%s@%s PE=%s@%s expiry=%s lot=%s",
                selection["strike"], selection["ce_symbol"], selection["ce_strike"],
                selection["pe_symbol"], selection["pe_strike"], selection["expiry"], selection["lot_size"])
    day_state = load_day_state(DAY_STATE_PATH, today_str)
    kill_params = KillSwitchParams(
        max_trades_per_day=cfg.MAX_TRADES_PER_DAY,
        max_daily_loss=cfg.MAX_DAILY_LOSS,
        max_consecutive_losses=cfg.MAX_CONSECUTIVE_LOSSES,
    )
    # A short option receives premium; premium-outlay sizing from the old
    # long-option engine is deliberately removed. Paper mode uses exactly
    # one exchange lot so results are interpretable. Real margin is NOT
    # inferred from this paper allocation.
    quantity = selection["lot_size"]
    session = build_session(quantity=quantity)
    audit_log = RotationAuditLog(AUDIT_LOG_PATH_TEMPLATE.format(date=today_str))
    market_open = datetime.now(IST).replace(hour=market_start_hour, minute=market_start_minute, second=0, microsecond=0)
    from kiteconnect import KiteTicker
    with open(cfg.ACCESS_TOKEN_FILE) as f:
        access_token = f.read().strip()
    kws = KiteTicker(cfg.API_KEY, access_token)
    latest_ticks = {}
    def on_ticks(ws, ticks):
        for t in ticks:
            latest_ticks[t["instrument_token"]] = t["last_price"]
    def on_connect(ws, response):
        tokens = [selection["underlying_token"], selection["ce_token"], selection["pe_token"]]
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)
        logger.info("WEBSOCKET_READY, subscribed underlying/CE/PE")
    kws.on_ticks = on_ticks
    kws.on_connect = on_connect
    kws.connect(threaded=True)
    logger.info(f"Waiting for market open at {market_open.isoformat()}")
    while datetime.now(IST) < market_open:
        time.sleep(1)
    session_start = time.monotonic()
    while True:
        now_dt = datetime.now(IST)
        now_hhmm = now_dt.strftime("%H:%M")
        if now_hhmm >= "15:35":
            logger.info("Session end, stopping")
            break
        seconds_since_open = (now_dt - market_open).total_seconds()
        elapsed_monotonic = time.monotonic() - session_start
        ce_price = latest_ticks.get(selection["ce_token"])
        pe_price = latest_ticks.get(selection["pe_token"])
        underlying_price = latest_ticks.get(selection["underlying_token"])
        if ce_price is None or pe_price is None or underlying_price is None:
            time.sleep(1)
            continue
        tick = TickSample(elapsed_monotonic, ce_price, pe_price, underlying_price)
        allowed, kill_reason = can_take_new_trade(day_state, kill_params)
        if now_hhmm < cfg.ENTRY_START_TIME:
            allowed, kill_reason = False, "options entry window has not opened"
        elif now_hhmm >= cfg.ENTRY_END_TIME and session.open_position is None:
            allowed, kill_reason = False, "options entry window closed"
        elif now_hhmm >= session.params_exit.session_cutoff_hhmm and session.open_position is None:
            allowed, kill_reason = False, "options exit cutoff reached"
        protected = is_within_opening_protection(seconds_since_open, OPENING_PROTECTION_SECONDS)
        record = session.on_tick(tick, now_hhmm=now_hhmm, kill_switch_allowed=allowed,
                                 kill_switch_reason=kill_reason, opening_protected=protected)
        audit_log.log_observation(record)
        if record.trade_opened:
            logger.info("PAPER SELL OPENED leg=%s underlying_direction=%s premium=%.2f quantity=%s",
                        record.trade_opened["direction"], record.trade_opened["underlying_direction"],
                        record.trade_opened["entry_price"], quantity)
        if record.trade_closed:
            pnl = net_pnl_for_closed_trade(record.trade_closed)
            audit_log.log_trade_closed(record.trade_closed, pnl)
            day_state = record_trade_result(day_state, pnl["net_pnl_estimate"], kill_params)
            save_day_state(DAY_STATE_PATH, day_state)
            logger.info("PAPER SELL CLOSED: %s net_pnl=%s", record.trade_closed.exit_reason, pnl["net_pnl_estimate"])
        if session.open_position is None and should_reselect_atm(
            underlying_price, selection["strike"], selection["strike_interval"], ATM_RESELECT_THRESHOLD, position_open=False):
            logger.info("Re-ATM trigger detected; re-selection wiring remains intentionally disabled until paper validation")
        time.sleep(1)
    kws.close()
    eod_summary = summarize_closed_trades(session.closed_trades)
    eod_summary.update({
        "date": today_str,
        "mode": cfg.MODE,
        "strategy": getattr(cfg, "OPTION_STRATEGY", "SELL_PREMIUM"),
        "underlying": underlying_name,
        "lot_size": quantity,
        "square_off_time": session.params_exit.session_cutoff_hhmm,
    })
    audit_log.log_eod_summary(eod_summary)
    logger.info(
        "EOD PAPER SUMMARY trades=%s gross=%s estimated_costs=%s net=%s reasons=%s",
        eod_summary["trade_count"], eod_summary["gross_pnl"],
        eod_summary["estimated_costs"], eod_summary["net_pnl_estimate"],
        eod_summary["exit_reason_counts"],
    )

def main():
    run_shadow_session(underlying_name=cfg.UNDERLYING)

if __name__ == "__main__":
    main()
