"""
Entry point: `python -m fno_bot.launcher`

Wires real KiteConnect/KiteTicker objects to the state machine
(state_machine.py) using the modules built across this and the prior
phase. This file is intentionally the ONLY place that constructs real
broker/WebSocket objects -- everything it calls into takes plain
data/dependency-injected callables, which is what keeps the rest of
the package unit-testable without a live connection.

CANNOT be exercised end-to-end outside real (or at minimum, paper)
market hours with real credentials -- treat this as the Phase 6
integration starting point, not validated production code. It does
NOT yet implement: PAPER-mode fill simulation, replay mode, or
resuming an in-flight MONITOR/EXIT_PENDING position after a restart
(crash_recovery.py detects that a recovery is needed and computes the
plan, but the actual reconstruction of local state from broker truth
into a live MONITOR loop is still a TODO -- see RECOVERY branch below).
"""
import sys
import time
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fno_bot import config as cfg
from fno_bot.state_machine import SessionContext, State, transition
from fno_bot.audit.event_log import log_event
from fno_bot.instruments.contract_master import load_contract_master, filter_for_underlying
from fno_bot.instruments.expiry import current_expiry
from fno_bot.instruments.strike_selector import select_atm_contracts
from fno_bot.market_data.tick_store import TickStore
from fno_bot.market_data.ticker import FnoTicker
from fno_bot.execution.position_store import load_positions
from fno_bot.execution import order_store
from fno_bot.monitoring.crash_recovery import compute_startup_recovery_plan, requires_recovery_state
from fno_bot.strategies.opening_scalper import build_snapshot, evaluate_signals
from fno_bot.strategies.signal_candidates import MarketSnapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fno.launcher")

IST = ZoneInfo("Asia/Kolkata")


def _get_kite_client():
    """Reuses the SAME daily access token as the equity bot (see
    architecture review Section B) -- never regenerates it itself."""
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=cfg.API_KEY)
    with open(cfg.ACCESS_TOKEN_FILE) as f:
        access_token = f.read().strip()
    kite.set_access_token(access_token)
    return kite


def run_boot_and_recovery(kite) -> SessionContext:
    """BOOT: always queries the broker before trusting local state
    (spec #27). Never assumes 'restarted = no position'."""
    ctx = SessionContext()
    log_event("BOOT_START")

    cfg.validate_mode()

    underlying_cfg = cfg.UNDERLYING_REGISTRY[cfg.UNDERLYING]
    tradingsymbol_prefix = cfg.UNDERLYING  # used only for the broker positions() lookup below

    local_positions = load_positions()
    local_position = local_positions.get(cfg.UNDERLYING)  # keyed by underlying for now (V1: one leg at a time)

    try:
        broker_positions = kite.positions().get("net", [])
    except Exception as e:
        logger.error(f"BOOT: could not query broker positions -- refusing to proceed blind: {e}")
        return transition(ctx, "RECOVERY_FAILED", reason=f"broker positions() query failed: {e}")

    broker_net_qty = sum(
        p.get("quantity", 0) for p in broker_positions
        if p.get("tradingsymbol", "").startswith(tradingsymbol_prefix)
    )

    plan = compute_startup_recovery_plan(local_position=local_position, broker_net_quantity=broker_net_qty)
    log_event("INSTRUMENT_MASTER_LOADED" if not requires_recovery_state(plan) else "ERROR",
              detail=plan.action_summary)

    if requires_recovery_state(plan):
        logger.warning(f"BOOT: recovery required -- {plan.action_summary}")
        # Reconstruction of live MONITOR state from broker truth is a
        # deliberate TODO (see module docstring) -- for now this halts
        # rather than guessing, which is the safe failure mode.
        return transition(ctx, "RECOVERY_REQUIRED", plan=plan)

    return transition(ctx, "CLEAN_STARTUP")


def run_prepare(kite, ctx: SessionContext) -> tuple[SessionContext, TickStore, FnoTicker, dict]:
    """PREPARE: auth already done (kite client passed in), load contract
    master, resolve expiry, connect + subscribe WebSocket BEFORE the
    market-opening decision (spec #3)."""
    log_event("AUTH_OK")
    underlying_cfg = cfg.UNDERLYING_REGISTRY[cfg.UNDERLYING]

    try:
        records = load_contract_master(kite, underlying_cfg["exchange"])
        records = filter_for_underlying(records, cfg.UNDERLYING)
        expiry = current_expiry(records, as_of=date.today())
        if expiry is None:
            return transition(ctx, "PREPARE_FAILED", reason="no valid expiry found"), None, None, {}
    except Exception as e:
        return transition(ctx, "PREPARE_FAILED", reason=f"contract master load failed: {e}"), None, None, {}

    tick_store = TickStore()
    with open(cfg.ACCESS_TOKEN_FILE) as f:
        access_token = f.read().strip()
    ticker = FnoTicker(cfg.API_KEY, access_token, tick_store=tick_store)
    ticker.connect(threaded=True)
    if not ticker.wait_connected(timeout_seconds=15):
        return transition(ctx, "PREPARE_FAILED", reason="WebSocket did not connect within 15s"), None, None, {}

    # Subscribe to the underlying index token first -- CE/PE tokens are
    # only known once the strike is selected from the first underlying tick.
    underlying_token = None
    try:
        underlying_instruments = kite.instruments(underlying_cfg["index_exchange"])
        underlying_token = next(
            i["instrument_token"] for i in underlying_instruments
            if i.get("tradingsymbol") == underlying_cfg["index_symbol"]
        )
    except (StopIteration, Exception) as e:
        return transition(ctx, "PREPARE_FAILED", reason=f"could not resolve underlying token: {e}"), None, None, {}

    ticker.subscribe([underlying_token], mode="full")
    log_event("WEBSOCKET_READY")

    return (
        transition(ctx, "PREPARE_OK"),
        tick_store,
        ticker,
        {"records": records, "expiry": expiry, "underlying_token": underlying_token, "underlying_cfg": underlying_cfg},
    )


def wait_for_first_underlying_tick(tick_store: TickStore, underlying_token: int, timeout_seconds: float = 120) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if tick_store.has_tick(underlying_token):
            log_event("FIRST_UNDERLYING_TICK", price=tick_store.latest(underlying_token).last_price)
            return True
        time.sleep(0.1)
    return False


def run_strike_and_signal(ctx: SessionContext, kite, tick_store: TickStore, prepare_data: dict) -> SessionContext:
    """FIRST_TICK_CAPTURE -> VALIDATE_MARKET -> SELECT_STRIKE -> GENERATE_SIGNAL."""
    underlying_token = prepare_data["underlying_token"]
    underlying_cfg = prepare_data["underlying_cfg"]

    if not wait_for_first_underlying_tick(tick_store, underlying_token, timeout_seconds=cfg.MAX_ENTRY_WINDOW_SECONDS):
        return transition(ctx, "MARKET_INVALID", reason="no underlying tick received within entry window")
    ctx = transition(ctx, "FIRST_TICKS_CAPTURED")

    underlying_tick = tick_store.latest(underlying_token)
    if not tick_store.is_fresh(underlying_token, cfg.MAX_TICK_AGE_MS):
        return transition(ctx, "MARKET_INVALID", reason="underlying tick already stale")
    ctx = transition(ctx, "MARKET_VALID")

    selection = select_atm_contracts(
        prepare_data["records"], underlying_price=underlying_tick.last_price,
        expiry=prepare_data["expiry"], strike_interval=underlying_cfg["strike_interval"],
    )
    if selection is None:
        return transition(ctx, "STRIKE_SELECTION_FAILED", reason="ATM CE/PE contracts not both available")
    log_event("STRIKE_SELECTED", strike=selection.atm_strike, expiry=str(selection.expiry))
    ctx = transition(ctx, "STRIKE_SELECTED", selection=selection)

    # Signal generation needs CE+PE ticks too -- left as an explicit TODO
    # wiring point: subscribe to selection.ce_contract/pe_contract tokens,
    # wait for their first ticks (spec #3 steps 6-8), THEN build the
    # MarketSnapshot and call evaluate_signals(). Not completed in this
    # phase since it requires live market data to exercise meaningfully.
    log_event("SIGNAL_REJECTED", reason="CE/PE tick wiring not yet implemented in launcher (see TODO)")
    return transition(ctx, "NO_TRADABLE_SIGNAL", reason="CE/PE tick subscription not yet wired")


def main():
    log_event("BOT_START", underlying=cfg.UNDERLYING, mode=cfg.MODE)
    try:
        kite = _get_kite_client()
    except Exception as e:
        log_event("ERROR", reason=f"could not load broker client: {e}")
        logger.error(f"Cannot start: {e}")
        sys.exit(1)

    ctx = run_boot_and_recovery(kite)
    if ctx.state == State.RECOVERY:
        logger.error(f"Halting at RECOVERY -- manual reconciliation required: {ctx.recovery_plan.action_summary}")
        log_event("ERROR", reason="halted at RECOVERY, manual reconciliation required")
        sys.exit(1)
    if ctx.state == State.STOPPED:
        logger.error(f"Halting: {ctx.abort_reason}")
        log_event("ERROR", reason=ctx.abort_reason)
        sys.exit(1)

    ctx, tick_store, ticker, prepare_data = run_prepare(kite, ctx)
    if ctx.state == State.STOPPED:
        logger.error(f"PREPARE failed: {ctx.abort_reason}")
        log_event("ERROR", reason=ctx.abort_reason)
        sys.exit(1)

    ctx = run_strike_and_signal(ctx, kite, tick_store, prepare_data)
    logger.info(f"Session ended in state={ctx.state.value} abort_reason={ctx.abort_reason}")
    log_event("BOT_STOP", final_state=ctx.state.value, abort_reason=ctx.abort_reason)


if __name__ == "__main__":
    main()
