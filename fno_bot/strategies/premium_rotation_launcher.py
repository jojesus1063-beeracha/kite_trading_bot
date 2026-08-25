"""
PREMIUM_ROTATION_SHADOW -- launcher (live Kite wiring).

*** UNTESTED AGAINST REAL KITE DATA. This file has never run against a
live WebSocket connection -- it was written and reviewed for
structural correctness (matches fno_bot/launcher.py's proven auth and
subscription pattern) but has NOT been executed. Its first real test
must happen on the VM, exactly like every other piece of this system
so far. Do not treat this as verified. ***

UPDATE: the kill-switch/opening-protection gap originally flagged here
has been fixed -- evaluate_entry() in premium_rotation_gate.py now
accepts kill_switch_allowed/opening_protected directly and structurally
cannot return eligible=True when either blocks it (verified by
test_kill_switch_structurally_blocks_eligibility and
test_opening_protection_structurally_blocks_eligibility). This file
still needs to be updated to actually PASS those values through
ShadowSession into evaluate_entry() -- ShadowSession.on_tick() doesn't
yet accept them as parameters, so that plumbing is the next real gap,
now one level further down than before but still present. Flagging
this explicitly rather than assuming the gate fix alone is sufficient.

SHADOW mode ONLY: this file never imports anything from fno_bot.broker
or fno_bot.execution, and never calls place_order/modify_order/
cancel_order. Section 18's isolation requirement is enforced by
omission -- grep this file for "place_order" and it will not appear.
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
from fno_bot.strategies.premium_rotation_costs_log import net_pnl_for_closed_trade, RotationAuditLog
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fno.premium_rotation")
IST = ZoneInfo("Asia/Kolkata")

AUDIT_LOG_PATH_TEMPLATE = "runtime/state/premium_rotation_shadow/audit_{date}.jsonl"

OPENING_PROTECTION_SECONDS = 60.0   # section 14: configurable, NOT assumed optimal
ATM_RESELECT_THRESHOLD = 100.0      # section 15: configurable


def get_kite_client():
    """Identical pattern to fno_bot/launcher.py's _get_kite_client() --
    reuses the SAME shared access token, never regenerates it."""
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=cfg.API_KEY)
    with open(cfg.ACCESS_TOKEN_FILE) as f:
        access_token = f.read().strip()
    kite.set_access_token(access_token)
    return kite


def select_atm_pair(kite, underlying_config: dict, target_date):
    """Read-only ATM CE/PE lookup -- the same pattern already run
    successfully live in the Phase A capability probe (2026-08-20)
    and the NIFTY affordability check (2026-08-21). This function is
    a light adaptation of code already exercised against real Kite
    data; the surrounding launcher plumbing below is what's new and
    unverified."""
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

    ce = next(i for i in same_expiry if i["strike"] == atm_strike and i["instrument_type"] == "CE")
    pe = next(i for i in same_expiry if i["strike"] == atm_strike and i["instrument_type"] == "PE")

    option_quotes = kite.ltp([f"{exchange}:{ce['tradingsymbol']}", f"{exchange}:{pe['tradingsymbol']}"])
    ce_price = option_quotes[f"{exchange}:{ce['tradingsymbol']}"]["last_price"]
    pe_price = option_quotes[f"{exchange}:{pe['tradingsymbol']}"]["last_price"]

    return {
        "spot": spot, "strike": atm_strike, "expiry": nearest_expiry,
        "underlying_token": underlying_quote["instrument_token"],
        "ce_token": ce["instrument_token"], "ce_symbol": ce["tradingsymbol"],
        "pe_token": pe["instrument_token"], "pe_symbol": pe["tradingsymbol"],
        "ce_price": ce_price, "pe_price": pe_price,
        "lot_size": int(ce["lot_size"]),
        "strike_interval": strike_interval,
    }


def build_session(quantity: int = 1) -> ShadowSession:
    """Experimental defaults per spec -- collected in one place so
    they're easy to find and tune once shadow data accumulates
    (section 20's parameter-replay requirement)."""
    params_rotation = RotationParams()
    params_entry = EntryParams()
    params_exit = ExitParams()
    return ShadowSession(
        params_rotation, params_entry, params_exit,
        window_seconds=10.0, confirmation_required_count=3, quantity=quantity,
        mode=cfg.MODE, paper_slippage_pct=cfg.PAPER_SLIPPAGE_PCT,
    )


def run_shadow_session(underlying_name: str = "NIFTY", market_start_hour: int = 9, market_start_minute: int = 15):
    """
    Top-level SHADOW-mode loop. NEVER calls any order function -- this
    loop only reads ticks, evaluates, and logs. Structurally complete,
    UNVERIFIED against real data (see module docstring).
    """
    if cfg.MODE == "LIVE":
        raise RuntimeError("Premium Rotation LIVE execution is structurally disabled")
    kite = get_kite_client()
    today = datetime.now(IST).date()
    today_str = str(today)

    underlying_config = cfg.UNDERLYING_REGISTRY[underlying_name]
    selection = select_atm_pair(kite, underlying_config, today)
    logger.info(f"ATM selected: strike={selection['strike']} expiry={selection['expiry']} "
                f"ce={selection['ce_symbol']} pe={selection['pe_symbol']}")

    day_state = load_day_state(DAY_STATE_PATH, today_str)
    kill_params = KillSwitchParams()
    premium_per_lot = max(selection["ce_price"], selection["pe_price"]) * selection["lot_size"]
    lots = int(cfg.FNO_CAPITAL // premium_per_lot) if premium_per_lot > 0 else 0
    if cfg.MODE == "PAPER" and lots < 1:
        raise RuntimeError(
            f"insufficient PAPER capital for one lot: capital={cfg.FNO_CAPITAL}, "
            f"premium_per_lot={premium_per_lot:.2f}"
        )
    quantity = selection["lot_size"] * max(lots, 1)
    session = build_session(quantity=quantity)
    audit_log = RotationAuditLog(AUDIT_LOG_PATH_TEMPLATE.format(date=today_str))

    market_open = datetime.now(IST).replace(
        hour=market_start_hour, minute=market_start_minute, second=0, microsecond=0
    )

    # --- WebSocket wiring -------------------------------------------------
    # NOTE: least-tested part of the whole build. Follows the same
    # connect/subscribe shape used by fno_bot/market_data/ticker.py,
    # but uses raw KiteTicker directly rather than that module's
    # wrapper class (its exact interface wasn't available to verify
    # against here). Reusing the real wrapper on the VM, instead of
    # this raw usage, is a worthwhile follow-up once this file's first
    # real run confirms the rest of the pipeline works end to end.
    from kiteconnect import KiteTicker
    with open(cfg.ACCESS_TOKEN_FILE) as f:
        access_token = f.read().strip()
    kws = KiteTicker(cfg.API_KEY, access_token)

    latest_ticks = {}   # instrument_token -> last_price

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
        if now_hhmm >= "15:30":
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

        tick = TickSample(timestamp=elapsed_monotonic, ce_price=ce_price, pe_price=pe_price,
                           underlying_price=underlying_price)

        allowed, kill_reason = can_take_new_trade(day_state, kill_params)
        if now_hhmm >= session.params_exit.session_cutoff_hhmm and session.open_position is None:
            allowed, kill_reason = False, "session entry cutoff reached"
        protected = is_within_opening_protection(seconds_since_open, OPENING_PROTECTION_SECONDS)

        # Gates now flow all the way into evaluate_entry() -- a trade
        # structurally cannot open when either blocks it. No after-the-
        # fact check needed anymore; verified by
        # test_kill_switch_structurally_blocks_eligibility and
        # test_opening_protection_structurally_blocks_eligibility.
        record = session.on_tick(
            tick, now_hhmm=now_hhmm,
            kill_switch_allowed=allowed, kill_switch_reason=kill_reason,
            opening_protected=protected,
        )
        audit_log.log_observation(record)

        if record.trade_opened:
            logger.info(
                "PAPER TRADE OPENED direction=%s fill=%.2f quantity=%s",
                record.trade_opened["direction"], record.trade_opened["entry_price"], quantity,
            )

        if record.trade_closed:
            pnl = net_pnl_for_closed_trade(record.trade_closed)
            audit_log.log_trade_closed(record.trade_closed, pnl)
            day_state = record_trade_result(day_state, pnl["net_pnl_estimate"], kill_params)
            save_day_state(DAY_STATE_PATH, day_state)
            logger.info(f"TRADE CLOSED: {record.trade_closed.exit_reason} net_pnl={pnl['net_pnl_estimate']}")

        if session.open_position is None and should_reselect_atm(
            underlying_price, selection["strike"], selection["strike_interval"],
            ATM_RESELECT_THRESHOLD, position_open=False,
        ):
            logger.info("Re-ATM triggered -- reselect logic present, re-subscribe wiring not yet added on the VM")

        time.sleep(1)

    kws.close()


def main():
    run_shadow_session(underlying_name=cfg.UNDERLYING)


if __name__ == "__main__":
    main()
