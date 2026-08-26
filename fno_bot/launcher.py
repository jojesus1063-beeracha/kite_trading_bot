"""
Entry point: `python -m fno_bot.launcher`

Wires real KiteConnect/KiteTicker objects (or, in PAPER mode, the
simulated-fill PaperBroker) to the state machine, using every module
built across this project. This file is intentionally the ONLY place
that constructs real broker/WebSocket objects.

HONESTY ABOUT SCOPE (read before running today):
- SHADOW and PAPER modes are wired end to end: BOOT -> PREPARE ->
  strike selection -> CE/PE subscription -> signal evaluation ->
  (PAPER only) sizing -> kill-switch check -> entry -> MONITOR -> exit
  -> trade recording -> REPORT.
- LIVE mode uses the exact same code path as PAPER except the broker
  object is the real kite client instead of PaperBroker -- but LIVE
  remains gated behind FNO_LIVE_ACK (config.validate_mode()) and is
  NOT recommended until PAPER has run cleanly for real sessions first,
  per the spec's own phased-rollout requirement.
- The MONITOR loop's `signal_still_valid_fn` is hardcoded to True.
  SIGNAL_INVALIDATION as an exit condition is fully implemented and
  tested in opening_scalper.py, but no specific, validated
  invalidation RULE exists yet (spec #6 is explicit that the
  direction-selection logic itself is unproven) -- wiring a real
  invalidation check is future work once a rule is chosen from shadow
  evidence, not a gap introduced today.
- Recovering an in-flight MONITOR/EXIT_PENDING position after a
  process restart is still a TODO (crash_recovery.py detects that
  recovery is needed and halts safely rather than guessing) -- this
  is unchanged from the prior phase.
- This has NEVER run against a real or simulated live market tick
  feed. Every module it calls is unit-tested in isolation with
  injected fakes; this file's own orchestration is not, and cannot
  meaningfully be, unit-tested without one. Watch the first PAPER
  session closely rather than treating this as pre-validated.
"""
import sys
import time
import logging
from dataclasses import dataclass
from dataclasses import asdict
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Optional

from fno_bot import config as cfg
from fno_bot.state_machine import SessionContext, State, transition
from fno_bot.audit.event_log import log_event
from fno_bot.audit.shadow_log import ShadowTracker, log_shadow_record
from fno_bot.instruments.contract_master import load_contract_master, filter_for_underlying
from fno_bot.instruments.expiry import current_expiry
from fno_bot.instruments.strike_selector import select_atm_contracts, StrikeSelection
from fno_bot.market_data.tick_store import TickStore
from fno_bot.market_data.ticker import FnoTicker
from fno_bot.execution.position_store import save_positions, load_positions, clear_positions
from fno_bot.execution.entry import execute_entry
from fno_bot.execution.exit import execute_exit
from fno_bot.monitoring.crash_recovery import compute_startup_recovery_plan, requires_recovery_state
from fno_bot.monitoring.disconnect_handler import decide_disconnect_action, DisconnectAction
from fno_bot.monitoring.position_monitor import init_excursion, monitor_step
from fno_bot.strategies.opening_scalper import build_snapshot, evaluate_signals
from fno_bot.risk.risk_manager import compute_quantity
from fno_bot.risk.kill_switches import FnoKillSwitch
from fno_bot.reporting.trade_log import record_trade, save_bot_status

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


def get_broker(real_kite, tick_store):
    """PAPER gets the simulated-fill adapter; SHADOW never places
    orders so this is never called for it; LIVE gets the real client."""
    if cfg.MODE == "PAPER":
        from fno_bot.broker.paper_broker import PaperBroker
        return PaperBroker(real_kite, tick_store, cfg)
    return real_kite


def run_boot_and_recovery(kite) -> SessionContext:
    """BOOT: always queries the broker before trusting local state
    (spec #27). PAPER mode never has a real broker position, so its
    reconciliation check is against broker_net_quantity=0 by
    construction -- if local PAPER state shows an open position,
    that's trusted as-is (it's synthetic; the real broker never had
    it), not flagged as a mismatch requiring recovery."""
    ctx = SessionContext()
    log_event("BOOT_START", mode=cfg.MODE)
    cfg.validate_mode()

    local_positions = load_positions()
    local_position = local_positions.get(cfg.UNDERLYING)

    if cfg.MODE == "PAPER":
        # PAPER's local position store IS the source of truth -- there is
        # nothing on the real broker to reconcile against (paper_broker.py
        # never touches the real account). Treat local state as authoritative.
        log_event("INSTRUMENT_MASTER_LOADED", detail="PAPER mode -- skipping broker position reconciliation")
        return transition(ctx, "CLEAN_STARTUP")

    try:
        broker_positions = kite.positions().get("net", [])
    except Exception as e:
        logger.error(f"BOOT: could not query broker positions -- refusing to proceed blind: {e}")
        return transition(ctx, "RECOVERY_FAILED", reason=f"broker positions() query failed: {e}")

    broker_net_qty = sum(
        p.get("quantity", 0) for p in broker_positions
        if p.get("tradingsymbol", "").startswith(cfg.UNDERLYING)
    )

    plan = compute_startup_recovery_plan(local_position=local_position, broker_net_quantity=broker_net_qty)
    log_event("INSTRUMENT_MASTER_LOADED" if not requires_recovery_state(plan) else "ERROR",
              detail=plan.action_summary)

    if requires_recovery_state(plan):
        logger.warning(f"BOOT: recovery required -- {plan.action_summary}")
        return transition(ctx, "RECOVERY_REQUIRED", plan=plan)

    return transition(ctx, "CLEAN_STARTUP")


def run_prepare(kite, ctx: SessionContext):
    """PREPARE: load contract master, resolve expiry, connect + subscribe
    WebSocket BEFORE the market-opening decision (spec #3)."""
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
        transition(ctx, "PREPARE_OK"), tick_store, ticker,
        {"records": records, "expiry": expiry, "underlying_token": underlying_token, "underlying_cfg": underlying_cfg},
    )


def _wait_for_ticks(tick_store: TickStore, tokens: list, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if all(tick_store.has_tick(t) for t in tokens):
            return True
        time.sleep(0.1)
    return False


def _fetch_prev_close(kite, underlying_cfg) -> Optional[float]:
    try:
        key = f"{underlying_cfg['index_exchange']}:{underlying_cfg['index_symbol']}"
        quote = kite.quote(key)
        return quote[key]["ohlc"]["close"]
    except Exception as e:
        logger.warning(f"could not fetch previous close (candidates needing it will abstain): {e}")
        return None


def _today_at(hhmmss: str) -> datetime:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    now = datetime.now(IST)
    return now.replace(hour=h, minute=m, second=s, microsecond=0)


def wait_for_market_open(ctx, clock_fn=None, sleep_fn=None, poll_seconds: float = 1.0):
    """WAIT_MARKET -> FIRST_TICK_CAPTURE (spec's own state diagram).

    This is the step that was silently MISSING from the first working
    version of this file: run_strike_and_signal() was being called
    directly after PREPARE, so the state machine never actually left
    WAIT_MARKET -- every later transition() call inside
    run_strike_and_signal (and, transitively, ENTRY_PENDING/etc. in
    main()) was a no-op against a state that didn't recognize the
    event, even though the underlying broker calls (subscribing,
    evaluating signals) still ran. Caught by actually running this
    file for the first time tonight (see PROGRESS notes) -- exactly
    the risk flagged in this file's own docstring about orchestration
    never having been exercised live.

    clock_fn/sleep_fn are injectable (default real time.sleep /
    datetime.now) purely so this is unit-testable without an actual
    wait -- same pattern as the rest of this package.
    """
    clock_fn = clock_fn or (lambda: datetime.now(IST))
    sleep_fn = sleep_fn or time.sleep
    now = clock_fn()
    h, m, s = (int(x) for x in cfg.ENTRY_START_TIME.split(":"))
    market_open_at = now.replace(hour=h, minute=m, second=s, microsecond=0)
    if now < market_open_at:
        logger.info(f"Waiting for market open at {market_open_at.isoformat()} (now {now.isoformat()})...")
    while clock_fn() < market_open_at:
        sleep_fn(poll_seconds)

    return transition(ctx, "MARKET_OPEN_REACHED")


def run_strike_and_signal(ctx, kite, tick_store, ticker, prepare_data):
    """FIRST_TICK_CAPTURE -> VALIDATE_MARKET -> SELECT_STRIKE -> GENERATE_SIGNAL.

    Stops at GENERATE_SIGNAL -- strike picked, CE/PE first ticks in --
    and hands off to the caller for signal evaluation. Originally this
    function ALSO evaluated the signal once, right here, off the very
    first CE/PE tick. Split out after the first real PAPER session
    (2026-08-20) showed that single-tick decision is fragile: a noisy
    opening print can burn the day's only opportunity even when the
    authorized signal is objectively firing. See run_entry_window()
    for the PAPER/LIVE opening-range polling replacement; SHADOW mode
    still evaluates once, directly in main(), since it never places an
    order regardless of how many times it looks.

    Returns (ctx, selection, prev_close).
    """
    underlying_token = prepare_data["underlying_token"]
    underlying_cfg = prepare_data["underlying_cfg"]

    if not _wait_for_ticks(tick_store, [underlying_token], cfg.MAX_ENTRY_WINDOW_SECONDS):
        return transition(ctx, "MARKET_INVALID", reason="no underlying tick within entry window"), None, None
    log_event("FIRST_UNDERLYING_TICK", price=tick_store.latest(underlying_token).last_price)
    ctx = transition(ctx, "FIRST_TICKS_CAPTURED")

    if not tick_store.is_fresh(underlying_token, cfg.MAX_TICK_AGE_MS):
        return transition(ctx, "MARKET_INVALID", reason="underlying tick already stale"), None, None
    ctx = transition(ctx, "MARKET_VALID")

    underlying_tick = tick_store.latest(underlying_token)
    selection = select_atm_contracts(
        prepare_data["records"], underlying_price=underlying_tick.last_price,
        expiry=prepare_data["expiry"], strike_interval=underlying_cfg["strike_interval"],
    )
    if selection is None:
        return transition(ctx, "STRIKE_SELECTION_FAILED", reason="ATM CE/PE not both available"), None, None
    log_event("STRIKE_SELECTED", strike=selection.atm_strike, expiry=str(selection.expiry))
    ctx = transition(ctx, "STRIKE_SELECTED", selection=selection)

    ce_token = selection.ce_contract.instrument_token
    pe_token = selection.pe_contract.instrument_token
    ticker.subscribe([ce_token, pe_token], mode="full")
    if not _wait_for_ticks(tick_store, [ce_token, pe_token], timeout_seconds=30):
        return transition(ctx, "NO_TRADABLE_SIGNAL", reason="CE/PE ticks did not arrive within 30s"), selection, None
    log_event("FIRST_CE_TICK", price=tick_store.latest(ce_token).last_price)
    log_event("FIRST_PE_TICK", price=tick_store.latest(pe_token).last_price)

    prev_close = _fetch_prev_close(kite, underlying_cfg)
    return ctx, selection, prev_close


def run_entry_window(ctx, broker, tick_store, selection, underlying_token, prev_close,
                      clock_fn=None, sleep_fn=None, poll_seconds: float = 1.0):
    """GENERATE_SIGNAL -> ENTRY_PENDING -> (POSITION_OPEN | ABORTED).

    Opening-range entry protocol (added 2026-08-20, replacing the
    original single-first-tick decision -- see run_strike_and_signal's
    docstring for why): re-evaluates every signal candidate and
    re-attempts entry against FRESH ticks on every poll cycle, for the
    whole cfg.ENTRY_START_TIME..cfg.ENTRY_END_TIME window, instead of
    deciding once off the very first tick. Stops the instant ONE
    attempt actually fills or partially fills -- still exactly one
    trade per day; run_entry()'s own kill-switch check and
    MAX_TRADES_PER_DAY guard the day-level rule, this loop's only job
    is giving that first trade more chances to find a clean entry.

    Each round calls run_entry(), which itself reads the CURRENT best
    ask/CE-or-PE price fresh at call time as the slippage-check anchor
    -- so a genuinely-moved market gets a fresh, fair chance each
    round, rather than perpetually failing against the very first
    tick's now-stale price the way a single anchor for the whole
    window would.

    clock_fn/sleep_fn injectable (default datetime.now(IST)/time.sleep)
    for testability, same DI pattern as wait_for_market_open().
    Returns (ctx, position_or_None, kill_switch_or_None, last_snapshot_or_None).
    """
    clock_fn = clock_fn or (lambda: datetime.now(IST))
    sleep_fn = sleep_fn or time.sleep
    now = clock_fn()
    h, m, s = (int(x) for x in cfg.ENTRY_END_TIME.split(":"))
    window_end = now.replace(hour=h, minute=m, second=s, microsecond=0)
    ce_token = selection.ce_contract.instrument_token
    pe_token = selection.pe_contract.instrument_token
    last_snapshot = None

    while clock_fn() < window_end:
        snapshot = build_snapshot(tick_store, underlying_token, ce_token, pe_token, prev_close)
        if snapshot is not None:
            last_snapshot = snapshot
            all_results, authorized = evaluate_signals(snapshot, cfg.AUTHORIZED_SIGNAL)
            log_event("SIGNAL_EVALUATED", results=[
                {"candidate": r.candidate, "direction": r.direction, "confidence": r.confidence} for r in all_results
            ])

            if authorized is not None:
                position, kill_switch, err = run_entry(broker, cfg, tick_store, selection, authorized, ticker=None)
                if position is not None:
                    ctx = transition(ctx, "SIGNAL_AUTHORIZED", signal=authorized)
                    ctx = transition(ctx, "ENTRY_FILLED", result=position)
                    return ctx, position, kill_switch, last_snapshot
                if err and "kill switch halted" in str(err):
                    # Day-level halt -- won't clear mid-window, stop polling now
                    # rather than keep re-checking a state that can't improve.
                    ctx = transition(ctx, "SIGNAL_AUTHORIZED", signal=authorized)
                    ctx = transition(ctx, "ENTRY_ABORTED", reason=err)
                    return ctx, None, kill_switch, last_snapshot
                # This round's attempt didn't fill (spread/slippage/stale-tick
                # -- already audit-logged inside execute_entry). Keep polling;
                # a later tick within the window may clear the same checks.

        sleep_fn(poll_seconds)

    log_event("WINDOW_EXPIRED_NO_TRADE", window_end=window_end.isoformat())
    ctx = transition(ctx, "NO_TRADABLE_SIGNAL", reason="WINDOW_EXPIRED_NO_TRADE: no fill within entry window")
    return ctx, None, None, last_snapshot


def run_shadow_observation(tick_store, underlying_token, ce_token, pe_token, snapshot, max_seconds, sleep_fn=None):
    """Runs when no trade was taken -- keeps recording counterfactual
    price data anyway (spec #22), for up to max_seconds or until every
    configured horizon is captured, whichever comes first."""
    sleep_fn = sleep_fn or time.sleep
    tracker = ShadowTracker(
        start_monotonic=time.monotonic(), horizons_seconds=tuple(cfg.COUNTERFACTUAL_HORIZONS_SECONDS),
        reference_ce_price=snapshot.ce_price, reference_pe_price=snapshot.pe_price,
    )
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline and not tracker.is_complete():
        ce_tick = tick_store.latest(ce_token)
        pe_tick = tick_store.latest(pe_token)
        if ce_tick and pe_tick:
            tracker.update(time.monotonic(), ce_tick.last_price, pe_tick.last_price)
        sleep_fn(1.0)
    log_shadow_record(tracker.to_record())


@dataclass
class OpenPosition:
    tradingsymbol: str
    exchange: str
    option_token: int
    strike: float
    option_type: str
    quantity: int
    entry_price: float
    entry_monotonic: float
    risk_plan: object = None


def run_entry(broker, cfg_ref, tick_store, selection: StrikeSelection, authorized_result, ticker=None,
              position_key=None, signal_still_valid_fn=None, recent_option_prices=None,
              entry_spread_pct=None):
    """GENERATE_SIGNAL -> ENTRY_PENDING. Sizes the position, checks the
    daily kill switch, then executes the entry through execution/entry.py
    -- IDENTICAL code path for PAPER and LIVE (only `broker` differs)."""
    direction = authorized_result.direction  # "CE" or "PE"
    contract = selection.ce_contract if direction == "CE" else selection.pe_contract
    reference_price = tick_store.latest(contract.instrument_token).last_price

    kill_switch = FnoKillSwitch(cfg_ref)
    if not kill_switch.can_take_new_trade():
        return None, kill_switch, f"kill switch halted: {kill_switch.day.halt_reason}"

    risk_plan = None
    sizing_stop_pct = cfg_ref.STOP_LOSS_PCT
    if getattr(cfg_ref, "DYNAMIC_EXITS_ENABLED", False) and recent_option_prices:
        from fno_bot.strategies.dynamic_exits import build_dynamic_exit_plan
        signal_metrics = (
            getattr(authorized_result, "raw_metrics", None)
            or getattr(authorized_result, "metrics", None)
            or {}
        )
        momentum = signal_metrics.get(
            "selected_option_roc_pct",
            signal_metrics.get("ce_roc_pct" if direction == "CE" else "pe_roc_pct", 0),
        )
        risk_plan = build_dynamic_exit_plan(
            recent_option_prices, momentum, entry_spread_pct,
            reference_price, contract.lot_size,
        )
        sizing_stop_pct = risk_plan.stop_pct

    sizing = compute_quantity(
        fno_capital=cfg_ref.FNO_CAPITAL, max_capital_per_trade_pct=cfg_ref.MAX_CAPITAL_PER_TRADE_PCT,
        max_risk_per_trade_pct=cfg_ref.MAX_RISK_PER_TRADE_PCT, stop_loss_pct=sizing_stop_pct,
        entry_reference_price=reference_price, lot_size=contract.lot_size,
    )
    if sizing.lots < 1:
        return None, kill_switch, sizing.reason

    if hasattr(broker, "register_instrument_token"):
        broker.register_instrument_token(contract.tradingsymbol, contract.instrument_token)

    def fetch_fresh_market():
        tick = tick_store.latest(contract.instrument_token)
        return {
            "best_ask": tick.best_ask if tick else None,
            "tick_age_ms": tick_store.tick_age_ms(contract.instrument_token),
            "spread_pct": tick_store.spread_pct(contract.instrument_token),
        }

    result = execute_entry(
        broker, symbol=contract.tradingsymbol, exchange=contract.exchange, quantity=sizing.quantity,
        original_reference_price=reference_price, cfg=cfg_ref, fetch_fresh_market=fetch_fresh_market,
        signal_still_valid_fn=signal_still_valid_fn or (lambda: True),
        audit_fn=lambda event, **data: log_event(event, **data),
    )

    if not result.success:
        # Callers interpret the first tuple item as an actual open
        # position. Returning a failed EntryResult here previously made
        # the opening-window loop treat an aborted order as a fill.
        return None, kill_switch, result.abort_reason

    if risk_plan is not None:
        risk_plan = build_dynamic_exit_plan(
            recent_option_prices, momentum, entry_spread_pct,
            result.average_price, result.filled_quantity,
        )
        log_event("DYNAMIC_EXIT_PLAN_CREATED", symbol=contract.tradingsymbol, **asdict(risk_plan))

    position = OpenPosition(
        tradingsymbol=contract.tradingsymbol, exchange=contract.exchange, option_token=contract.instrument_token,
        strike=selection.atm_strike, option_type=direction, quantity=result.filled_quantity,
        entry_price=result.average_price, entry_monotonic=time.monotonic(), risk_plan=risk_plan,
    )
    save_positions({(position_key or cfg_ref.UNDERLYING): {
        "tradingsymbol": position.tradingsymbol, "exchange": position.exchange, "quantity": position.quantity,
        "entry_price": position.entry_price, "option_type": position.option_type, "strike": position.strike,
        "risk_plan": asdict(risk_plan) if risk_plan else None,
    }})
    return position, kill_switch, None


def run_monitor_and_exit(broker, ticker, tick_store, position: OpenPosition, kill_switch, cfg_ref,
                         underlying_name=None, signal_still_valid_fn=None):
    """POSITION_OPEN -> MONITOR -> EXIT_PENDING -> CLOSED. Polls once
    per second; escalates to a FORCE_EXIT if disconnected beyond the
    configured recovery timeout while a position is open (spec #26)."""
    excursion = init_excursion(position.entry_price)
    disconnected_since = None
    just_reconnected_pending = False

    while True:
        is_connected = ticker.is_connected()
        if not is_connected and disconnected_since is None:
            disconnected_since = time.monotonic()
            just_reconnected_pending = True
        elif is_connected and disconnected_since is not None:
            disconnected_since = None  # reconnected -- one reconciliation pass consumed below

        disconnect_decision = decide_disconnect_action(
            is_connected=is_connected,
            just_reconnected=(is_connected and just_reconnected_pending),
            has_open_position=True,
            disconnected_seconds=(time.monotonic() - disconnected_since) if disconnected_since else None,
            recovery_timeout_seconds=cfg_ref.DISCONNECT_WHILE_OPEN_RECOVERY_TIMEOUT_SECONDS,
        )
        if disconnect_decision.action == DisconnectAction.RECONCILE_WITH_BROKER:
            just_reconnected_pending = False
            log_event("MONITOR_RECONNECTED", detail=disconnect_decision.reason)

        emergency = disconnect_decision.action == DisconnectAction.EMERGENCY_POSITION_HANDLING

        result, excursion, current_price = monitor_step(
            tick_store=tick_store, option_token=position.option_token, direction="BUY",
            entry_price=position.entry_price, excursion=excursion, entry_monotonic=position.entry_monotonic,
            signal_still_valid_fn=signal_still_valid_fn or (lambda: True), cfg=cfg_ref,
            audit_fn=lambda event, **data: log_event(event, **data),
            risk_plan=position.risk_plan,
        )

        if emergency or result.should_exit:
            reason = "DISCONNECTED_BEYOND_TIMEOUT" if emergency else result.reason
            action = "FORCE_EXIT" if emergency else "EXIT"

            def fetch_fresh_best_bid():
                tick = tick_store.latest(position.option_token)
                return tick.best_bid if tick else None

            exit_result = execute_exit(
                broker, symbol=position.tradingsymbol, exchange=position.exchange, quantity=position.quantity,
                direction="BUY", action=action, cfg=cfg_ref, fetch_fresh_best_bid=fetch_fresh_best_bid,
                audit_fn=lambda event, **data: log_event(event, **data),
            )

            exit_price = exit_result.average_price if exit_result.average_price is not None else position.entry_price
            record_trade(
                underlying=(underlying_name or cfg_ref.UNDERLYING), strike=position.strike, option_type=position.option_type,
                direction="BUY", quantity=exit_result.filled_quantity or position.quantity,
                entry_price=position.entry_price, exit_price=exit_price, mode=cfg_ref.MODE, exit_reason=reason,
                mfe_pct=excursion.mfe_pct, mae_pct=excursion.mae_pct,
            )
            net_pnl = (exit_price - position.entry_price) * (exit_result.filled_quantity or position.quantity)
            kill_switch.record_trade_result(net_pnl)
            clear_positions()
            return exit_result, reason

        time.sleep(1.0)


def main():
    if cfg.UNIVERSE_MODE == "ALL_STOCK_OPTIONS":
        from fno_bot.stock_options_launcher import run_all_stock_options
        run_all_stock_options()
        return
    log_event("BOT_START", underlying=cfg.UNDERLYING, mode=cfg.MODE)
    try:
        kite = _get_kite_client()
    except Exception as e:
        log_event("ERROR", reason=f"could not load broker client: {e}")
        logger.error(f"Cannot start: {e}")
        sys.exit(1)

    ctx = run_boot_and_recovery(kite)
    if ctx.state in (State.RECOVERY, State.STOPPED):
        reason = ctx.recovery_plan.action_summary if ctx.state == State.RECOVERY else ctx.abort_reason
        logger.error(f"Halting at {ctx.state.value}: {reason}")
        log_event("ERROR", reason=reason)
        sys.exit(1)

    ctx, tick_store, ticker, prepare_data = run_prepare(kite, ctx)
    if ctx.state == State.STOPPED:
        logger.error(f"PREPARE failed: {ctx.abort_reason}")
        log_event("ERROR", reason=ctx.abort_reason)
        sys.exit(1)

    broker = get_broker(kite, tick_store) if cfg.MODE in ("PAPER", "LIVE") else None

    ctx = wait_for_market_open(ctx)
    if ctx.state != State.FIRST_TICK_CAPTURE:
        # Should not happen -- wait_for_market_open only returns after
        # market-open time, and WAIT_MARKET only reacts to
        # MARKET_OPEN_REACHED (or a disconnect, handled elsewhere) --
        # but refuse to proceed blind rather than silently running
        # run_strike_and_signal() against a state it doesn't expect.
        logger.error(f"Unexpected state after wait_for_market_open: {ctx.state.value}")
        log_event("ERROR", reason=f"unexpected state after market-open wait: {ctx.state.value}")
        save_bot_status(state=ctx.state.value, mode=cfg.MODE, underlying=cfg.UNDERLYING, session_summary=None)
        if ticker is not None:
            ticker.close()
        sys.exit(1)

    ctx, selection, prev_close = run_strike_and_signal(ctx, kite, tick_store, ticker, prepare_data)
    underlying_token = prepare_data["underlying_token"]

    trade_summary = None
    last_snapshot = None
    position = None
    kill_switch = None

    if ctx.state == State.GENERATE_SIGNAL and cfg.MODE in ("PAPER", "LIVE"):
        # Opening-range entry protocol: keep evaluating fresh ticks and
        # attempting entry for the whole ENTRY_START_TIME..ENTRY_END_TIME
        # window, instead of deciding once off the very first tick alone
        # (see run_entry_window()'s docstring for why -- added after the
        # first real PAPER session showed the single-shot version can
        # burn the day's only opportunity on one noisy opening print).
        ctx, position, kill_switch, last_snapshot = run_entry_window(
            ctx, broker, tick_store, selection, underlying_token, prev_close,
        )
    elif ctx.state == State.GENERATE_SIGNAL:
        # SHADOW mode: never places an order regardless of how many
        # times it looks, so a single evaluation (for logging/shadow
        # evidence) is enough -- unchanged from the original behavior.
        last_snapshot = build_snapshot(
            tick_store, underlying_token, selection.ce_contract.instrument_token,
            selection.pe_contract.instrument_token, prev_close,
        )
        if last_snapshot is not None:
            all_results, authorized = evaluate_signals(last_snapshot, cfg.AUTHORIZED_SIGNAL)
            log_event("SIGNAL_EVALUATED", results=[
                {"candidate": r.candidate, "direction": r.direction, "confidence": r.confidence} for r in all_results
            ])
            if authorized is not None:
                ctx = transition(ctx, "SIGNAL_AUTHORIZED", signal=authorized)
            else:
                log_event("SIGNAL_REJECTED", reason="no authorized candidate produced a direction this tick")
                ctx = transition(ctx, "NO_TRADABLE_SIGNAL", reason="no authorized signal")

    if ctx.state == State.ABORTED and last_snapshot is not None:
        run_shadow_observation(
            tick_store, underlying_token, selection.ce_contract.instrument_token,
            selection.pe_contract.instrument_token, last_snapshot, max(cfg.COUNTERFACTUAL_HORIZONS_SECONDS),
        )

    if ctx.state == State.POSITION_OPEN and position is not None:
        ctx = transition(ctx, "MONITORING_STARTED")
        exit_result, exit_reason = run_monitor_and_exit(broker, ticker, tick_store, position, kill_switch, cfg)
        ctx = transition(ctx, "EXIT_CONDITION_FIRED")
        ctx = transition(ctx, "EXIT_FILLED", result=exit_result)
        ctx = transition(ctx, "REPORTED")
        trade_summary = {"exit_reason": exit_reason, "filled_quantity": exit_result.filled_quantity,
                          "average_price": exit_result.average_price}

    save_bot_status(state=ctx.state.value, mode=cfg.MODE, underlying=cfg.UNDERLYING, session_summary=trade_summary)
    logger.info(f"Session ended in state={ctx.state.value} abort_reason={ctx.abort_reason} summary={trade_summary}")
    log_event("BOT_STOP", final_state=ctx.state.value, abort_reason=ctx.abort_reason, summary=trade_summary)

    if ticker is not None:
        ticker.close()


if __name__ == "__main__":
    main()
