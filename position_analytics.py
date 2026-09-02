"""
Analytics-only layer for live position monitoring. Never generates
signals, never touches entry/exit decisions, never writes to
open_positions.json's trading-critical fields (entry/stop/target/qty/
direction) -- only reads them. Adds two new, purely informational
fields to each position dict (mfe_pct, mae_pct) for continuous
excursion tracking; everything else is written to bot_status.json,
a reporting file, not a trading-state file.
"""
import time
import logging

logger = logging.getLogger("position_analytics")


def compute_gross_pnl(direction, entry, current, qty):
    if direction == "BUY":
        return (current - entry) * qty
    else:
        return (entry - current) * qty


def compute_profit_pct(direction, entry, current):
    if entry == 0:
        return 0.0
    if direction == "BUY":
        return (current - entry) / entry * 100
    else:
        return (entry - current) / entry * 100


def compute_distances(direction, current, stop, target):
    """Returns dict with distance to stop/target in both INR and %,
    always non-negative in the 'still open' sense (distance remaining)."""
    if direction == "BUY":
        dist_stop_inr = current - stop
        dist_target_inr = target - current
    else:
        dist_stop_inr = stop - current
        dist_target_inr = current - target

    dist_stop_pct = (dist_stop_inr / current * 100) if current else 0.0
    dist_target_pct = (dist_target_inr / current * 100) if current else 0.0

    return {
        "distance_to_stop_inr": dist_stop_inr, "distance_to_stop_pct": dist_stop_pct,
        "distance_to_target_inr": dist_target_inr, "distance_to_target_pct": dist_target_pct,
    }


def compute_reward_risk(direction, entry, current, stop, target):
    """Return both entry-plan and live remaining reward:risk.

    Keeping these values separate prevents the live monitor from presenting a
    ratio that rises as a losing position approaches its stop as though it were
    the trade's original reward:risk.
    """
    if direction == "BUY":
        entry_reward = target - entry
        entry_risk = entry - stop
        reward_remaining = target - current
        risk_remaining = current - stop
    else:
        entry_reward = entry - target
        entry_risk = stop - entry
        reward_remaining = current - target
        risk_remaining = stop - current

    entry_reward_risk = (
        entry_reward / entry_risk
        if entry_reward >= 0 and entry_risk > 0
        else None
    )
    remaining_reward_risk = (
        reward_remaining / risk_remaining
        if reward_remaining >= 0 and risk_remaining > 0
        else None
    )
    return {
        "entry_reward": entry_reward,
        "entry_risk": entry_risk,
        "entry_reward_risk": entry_reward_risk,
        "reward_remaining": reward_remaining, "risk_remaining": risk_remaining,
        "remaining_reward_risk": remaining_reward_risk,
        # Backward-compatible alias for downstream consumers that already use
        # reward_risk as the live remaining ratio.
        "reward_risk": remaining_reward_risk,
    }


def compute_spread(bid, ask):
    if bid is None or ask is None or bid <= 0:
        return {"spread": None, "spread_pct": None}
    spread = ask - bid
    spread_pct = (spread / bid * 100) if bid else None
    return {"spread": spread, "spread_pct": spread_pct}


def update_mfe_mae(position, current_profit_pct):
    """
    Mutates position IN PLACE, adding/updating mfe_pct and mae_pct --
    the best and worst profit % ever seen since entry. These are NEW
    fields, purely informational, never read by any exit-decision
    code. Returns the updated (mfe_pct, mae_pct).
    """
    # Excursion is measured relative to entry.  Before price has moved in a
    # favourable/adverse direction the corresponding excursion is zero, not a
    # negative MFE or positive MAE.
    # Clamp legacy persisted values as well, so a position first observed while
    # losing is repaired from a negative MFE on the next analytics update.
    mfe = max(float(position.get("mfe_pct", 0.0) or 0.0), 0.0)
    mae = min(float(position.get("mae_pct", 0.0) or 0.0), 0.0)
    mfe = max(mfe, current_profit_pct)
    mae = min(mae, current_profit_pct)
    position["mfe_pct"] = mfe
    position["mae_pct"] = mae
    return mfe, mae


def classify_status(direction, current, stop, target, near_pct=0.25,
                     quote_age_seconds=None, stale_threshold=60):
    """
    Returns one of: PRICE_STALE, TARGET_HIT (Pending Exit),
    STOP_HIT (Pending Exit), NEAR_TARGET, NEAR_STOP, ACTIVE.
    Stale check takes priority -- a stale quote makes every other
    classification unreliable.
    """
    if quote_age_seconds is not None and quote_age_seconds > stale_threshold:
        return "PRICE_STALE"

    if direction == "BUY":
        if current >= target:
            return "TARGET_HIT (Pending Exit)"
        if current <= stop:
            return "STOP_HIT (Pending Exit)"
        dist_to_target_pct = (target - current) / current * 100 if current else 0
        dist_to_stop_pct = (current - stop) / current * 100 if current else 0
    else:
        if current <= target:
            return "TARGET_HIT (Pending Exit)"
        if current >= stop:
            return "STOP_HIT (Pending Exit)"
        dist_to_target_pct = (current - target) / current * 100 if current else 0
        dist_to_stop_pct = (stop - current) / current * 100 if current else 0

    if dist_to_target_pct <= near_pct:
        return "NEAR TARGET"
    if dist_to_stop_pct <= near_pct:
        return "NEAR STOP"
    return "ACTIVE"


def fetch_batched_quotes(kite, open_positions):
    """
    ONE batched kite.quote() call for every open position's symbol --
    per the explicit requirement not to fetch one symbol at a time and
    not to create a second polling loop. Returns {} on any failure
    (fail-safe); callers must retain prior values rather than treat
    an empty result as 'price is zero'.
    """
    if not open_positions:
        return {}
    instrument_keys = [f"{pos.get('exchange', 'NSE')}:{symbol}" for symbol, pos in open_positions.items()]
    try:
        return kite.quote(instrument_keys)
    except Exception as e:
        logger.warning(f"Batched quote fetch failed for {len(instrument_keys)} instruments: {e}")
        return {}


def validate_position(symbol, position):
    """
    Returns a list of validation error strings (empty list = valid).
    Checks only the STATIC fields known before any quote fetch --
    current_price validity (a genuinely nonsensical live price, e.g.
    <= 0) is checked separately in build_position_analytics() once a
    quote is actually available, since a missing quote is PRICE_STALE
    territory, not INVALID_DATA.
    """
    errors = []
    if not symbol or not isinstance(symbol, str):
        errors.append("symbol is missing or invalid")
    if not position.get("exchange"):
        errors.append("exchange is missing")
    direction = position.get("direction")
    if direction not in ("BUY", "SELL"):
        errors.append("direction is missing or invalid (must be BUY or SELL)")
    qty = position.get("qty")
    if not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty <= 0:
        errors.append("quantity must be greater than zero")
    entry = position.get("entry")
    if not isinstance(entry, (int, float)) or isinstance(entry, bool) or entry <= 0:
        errors.append("entry_price must be greater than zero")
    stop = position.get("stop")
    if not isinstance(stop, (int, float)) or isinstance(stop, bool) or stop <= 0:
        errors.append("stop_price must be greater than zero")
    target = position.get("target")
    if not isinstance(target, (int, float)) or isinstance(target, bool) or target <= 0:
        errors.append("target_price must be greater than zero")
    return errors


def build_position_analytics(symbol, position, quotes, previous_status_entry=None,
                               near_pct=0.25, stale_threshold_seconds=60):
    """
    Assembles the full analytics dict for one position, per the
    requested bot_status.json schema. Fails safe: if this symbol's
    quote is missing from `quotes` (batched fetch failed or omitted
    it), retains every field from `previous_status_entry` unchanged
    except marks status as PRICE_STALE and recomputes quote_age from
    the OLD price_updated_at -- never fabricates a zero price.
    """
    from costs import net_pnl_for_trade

    validation_errors = validate_position(symbol, position)
    if validation_errors:
        return {
            "symbol": symbol, "exchange": position.get("exchange"),
            "side": position.get("direction"), "quantity": position.get("qty"),
            "entry_price": position.get("entry"), "current_price": None,
            "status": "INVALID_DATA", "validation_errors": validation_errors,
            "price_updated_at": None, "quote_age_seconds": None,
        }

    exchange = position.get("exchange", "NSE")
    key = f"{exchange}:{symbol}"
    quote = quotes.get(key)

    now = time.time()
    direction = position["direction"]
    entry = position["entry"]
    qty = position["qty"]
    stop = position.get("stop")
    target = position.get("target")

    if quote is None:
        # No fresh quote this cycle -- retain everything from last time.
        if previous_status_entry:
            retained = dict(previous_status_entry)
            old_updated_at = retained.get("price_updated_at", now)
            retained["quote_age_seconds"] = now - old_updated_at
            retained["status"] = "PRICE_STALE"
            return retained
        # No quote AND no prior data -- genuinely nothing to report yet.
        return {
            "symbol": symbol, "exchange": exchange, "side": direction, "quantity": qty,
            "entry_price": entry, "current_price": None, "status": "PRICE_STALE",
            "price_updated_at": None, "quote_age_seconds": None,
        }

    current_price = quote.get("last_price")
    if not isinstance(current_price, (int, float)) or isinstance(current_price, bool) or current_price <= 0:
        return {
            "symbol": symbol, "exchange": exchange, "side": direction, "quantity": qty,
            "entry_price": entry, "current_price": current_price,
            "status": "INVALID_DATA",
            "price_updated_at": now, "quote_age_seconds": 0,
        }
    depth = quote.get("depth", {})
    bid = depth.get("buy", [{}])[0].get("price") if depth.get("buy") else None
    ask = depth.get("sell", [{}])[0].get("price") if depth.get("sell") else None
    ltq = quote.get("last_quantity")

    gross_pnl = compute_gross_pnl(direction, entry, current_price, qty)
    cost_result = net_pnl_for_trade(direction, qty, entry, current_price)
    net_pnl = cost_result["net_pnl"]
    profit_pct = compute_profit_pct(direction, entry, current_price)

    mfe_pct, mae_pct = update_mfe_mae(position, profit_pct)

    session_high = max(position.get("session_high_since_entry", current_price), current_price)
    session_low = min(position.get("session_low_since_entry", current_price), current_price)
    position["session_high_since_entry"] = session_high
    position["session_low_since_entry"] = session_low

    distances = compute_distances(direction, current_price, stop, target) if stop and target else {}
    rr = compute_reward_risk(direction, entry, current_price, stop, target) if stop and target else {}
    spread_info = compute_spread(bid, ask)

    entry_time_str = position.get("entry_time")
    time_in_trade_minutes = None
    if entry_time_str:
        try:
            import pandas as pd
            entry_dt = pd.to_datetime(entry_time_str)
            now_dt = pd.Timestamp.now(tz=entry_dt.tz) if entry_dt.tz is not None else pd.Timestamp.now()
            time_in_trade_minutes = (now_dt - entry_dt).total_seconds() / 60
        except Exception:
            pass

    status = classify_status(direction, current_price, stop, target, near_pct=near_pct,
                              quote_age_seconds=0, stale_threshold=stale_threshold_seconds) if stop and target else "ACTIVE"

    return {
        "symbol": symbol, "exchange": exchange, "side": direction, "quantity": qty,
        "entry_price": entry, "current_price": current_price,
        "gross_unrealized_pnl": gross_pnl, "net_unrealized_pnl": net_pnl, "profit_pct": profit_pct,
        "entry_time": entry_time_str, "time_in_trade_minutes": time_in_trade_minutes,
        "stop_price": stop, "target_price": target,
        "strategy_stop_price": position.get("paper_strategy_stop"),
        "stop_type": (
            "PAPER_EMERGENCY"
            if position.get("paper_emergency_stop_active")
            else "STRATEGY"
        ),
        "hybrid_exit_stage": position.get("hybrid_exit_stage"),
        "session_high_since_entry": session_high, "session_low_since_entry": session_low,
        "mfe_pct": mfe_pct, "mae_pct": mae_pct,
        **distances, **rr,
        "bid": bid, "ask": ask, **spread_info, "ltq": ltq,
        "price_updated_at": now, "quote_age_seconds": 0,
        "status": status,
        "raw_direction": position.get("raw_direction"),
        "final_direction": position.get("final_direction", direction),
        "policy_decision": position.get("policy_decision"),
        "policy_reason": position.get("policy_reason"),
        "policy_market_trend": position.get("policy_market_trend"),
    }


def compute_portfolio_summary(position_analytics_list, margins=None):
    """
    Aggregates across all open positions' analytics dicts (the output
    of build_position_analytics, one per position). `margins` is the
    raw dict from kite.margins(), optional -- fields needing it are
    None if not provided.
    """
    invalid_positions = [p for p in position_analytics_list if p.get("status") == "INVALID_DATA"]
    valid_positions = [p for p in position_analytics_list if p.get("status") != "INVALID_DATA"]
    position_analytics_list = valid_positions  # every line below this point sees ONLY valid rows

    if not position_analytics_list:
        summary = {
            "total_open_positions": 0, "buy_positions": 0, "sell_positions": 0,
            "total_exposure": 0.0, "gross_unrealized_pnl": 0.0, "net_unrealized_pnl": 0.0,
            "portfolio_profit_pct": None, "largest_winning_position": None,
            "largest_losing_position": None, "total_portfolio_risk": 0.0,
            "total_portfolio_reward": 0.0, "portfolio_reward_risk": None,
            "largest_position_size": 0.0, "largest_position_risk": 0.0,
            "average_holding_time_minutes": None,
            "invalid_position_count": len(invalid_positions),
        }
    else:
        buy_count = sum(1 for p in position_analytics_list if p.get("side") == "BUY")
        sell_count = sum(1 for p in position_analytics_list if p.get("side") == "SELL")
        total_exposure = sum((p.get("current_price") or 0) * p.get("quantity", 0) for p in position_analytics_list)
        gross_pnl = sum(p.get("gross_unrealized_pnl") or 0 for p in position_analytics_list)
        net_pnl = sum(p.get("net_unrealized_pnl") or 0 for p in position_analytics_list)
        portfolio_profit_pct = (net_pnl / total_exposure * 100) if total_exposure else None

        by_pnl = sorted(position_analytics_list, key=lambda p: p.get("gross_unrealized_pnl") or 0)
        largest_losing = by_pnl[0] if by_pnl and (by_pnl[0].get("gross_unrealized_pnl") or 0) < 0 else None
        largest_winning = by_pnl[-1] if by_pnl and (by_pnl[-1].get("gross_unrealized_pnl") or 0) > 0 else None

        total_risk = sum(p.get("risk_remaining") or 0 for p in position_analytics_list)
        total_reward = sum(p.get("reward_remaining") or 0 for p in position_analytics_list)
        portfolio_rr = (total_reward / total_risk) if total_risk > 0 else None

        position_sizes = [(p.get("current_price") or 0) * p.get("quantity", 0) for p in position_analytics_list]
        position_risks = [(p.get("risk_remaining") or 0) * p.get("quantity", 0) for p in position_analytics_list]
        holding_times = [p.get("time_in_trade_minutes") for p in position_analytics_list if p.get("time_in_trade_minutes") is not None]

        summary = {
            "total_open_positions": len(position_analytics_list), "buy_positions": buy_count, "sell_positions": sell_count,
            "total_exposure": total_exposure, "gross_unrealized_pnl": gross_pnl, "net_unrealized_pnl": net_pnl,
            "portfolio_profit_pct": portfolio_profit_pct,
            "largest_winning_position": largest_winning.get("symbol") if largest_winning else None,
            "largest_losing_position": largest_losing.get("symbol") if largest_losing else None,
            "total_portfolio_risk": total_risk, "total_portfolio_reward": total_reward,
            "portfolio_reward_risk": portfolio_rr,
            "largest_position_size": max(position_sizes) if position_sizes else 0.0,
            "largest_position_risk": max(position_risks) if position_risks else 0.0,
            "average_holding_time_minutes": (sum(holding_times) / len(holding_times)) if holding_times else None,
            "invalid_position_count": len(invalid_positions),
        }

    if margins:
        try:
            equity = margins.get("equity", {})
            available_cash = equity.get("available", {}).get("live_balance")
            used_margin = equity.get("utilised", {}).get("debits")
            net_margin = equity.get("net")
            margin_util_pct = (used_margin / net_margin * 100) if used_margin and net_margin else None
            summary.update({
                "available_cash": available_cash, "used_margin": used_margin,
                "remaining_margin": net_margin, "margin_utilization_pct": margin_util_pct,
            })
        except Exception as e:
            logger.warning(f"Failed to extract margin data for portfolio summary: {e}")

    return summary


def compute_session_summary(todays_trades):
    """
    Aggregates today's CLOSED trades (from trade_history.jsonl records
    for today's date) into full session statistics.
    """
    if not todays_trades:
        return {
            "todays_trades": 0, "winning_trades": 0, "losing_trades": 0, "win_rate_pct": None,
            "gross_realized_profit": 0.0, "gross_realized_loss": 0.0, "net_realized_profit": 0.0,
            "brokerage_and_charges": 0.0, "average_win": None, "average_loss": None,
            "expectancy": None, "profit_factor": None, "largest_winner": None, "largest_loser": None,
            "current_consecutive_wins": 0, "current_consecutive_losses": 0,
            "max_drawdown_today": 0.0, "peak_equity_today": 0.0, "current_equity": 0.0,
            "average_holding_time_minutes": None,
        }

    pnls = [t["pnl"] for t in todays_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_wins = [t.get("gross_pnl", t["pnl"]) for t in todays_trades if t["pnl"] > 0]
    gross_losses = [t.get("gross_pnl", t["pnl"]) for t in todays_trades if t["pnl"] <= 0]
    total_costs = sum(t.get("costs", 0) for t in todays_trades)

    win_rate = len(wins) / len(pnls) * 100
    net_realized = sum(pnls)
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    expectancy = net_realized / len(pnls)
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else (float('inf') if wins else None)

    # Consecutive wins/losses -- current streak, counting from the END (most recent) backwards
    current_streak_wins, current_streak_losses = 0, 0
    for p in reversed(pnls):
        if p > 0:
            if current_streak_losses > 0:
                break
            current_streak_wins += 1
        else:
            if current_streak_wins > 0:
                break
            current_streak_losses += 1

    # Equity curve / max drawdown across today's trades in chronological order
    cumulative, peak, max_dd = 0, 0, 0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)

    return {
        "todays_trades": len(pnls), "winning_trades": len(wins), "losing_trades": len(losses),
        "win_rate_pct": win_rate,
        "gross_realized_profit": sum(gross_wins), "gross_realized_loss": sum(gross_losses),
        "net_realized_profit": net_realized, "brokerage_and_charges": total_costs,
        "average_win": avg_win, "average_loss": avg_loss, "expectancy": expectancy,
        "profit_factor": profit_factor,
        "largest_winner": max(pnls) if wins else None, "largest_loser": min(pnls) if losses else None,
        "current_consecutive_wins": current_streak_wins, "current_consecutive_losses": current_streak_losses,
        "max_drawdown_today": max_dd, "peak_equity_today": peak, "current_equity": cumulative,
        "average_holding_time_minutes": None,  # requires entry_time in trade_history, not yet tracked there
    }


def get_git_commit_hash():
    """Short git commit hash of the running code, or 'unknown' on any failure."""
    try:
        import subprocess
        result = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                 capture_output=True, text=True, timeout=5,
                                 cwd=__file__.rsplit("/", 1)[0])
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        logger.warning(f"Failed to get git commit hash: {e}")
    return "unknown"


def compute_health_check(cfg, kite, open_positions, symbols, start_time):
    """
    Full health-check snapshot: mode, filters, scheduler status, auth,
    resource usage, uptime, version. Never raises -- every field
    fails safe to None/'unknown' individually so one bad check
    doesn't blank out the rest.
    """
    health = {
        "trading_mode": "PAPER" if getattr(cfg, "PAPER_TRADING", True) else "LIVE",
        "market_alignment": "Observational" if getattr(cfg, "PROPOSED_CLEAN_PIPELINE", False) else ("Enabled" if getattr(cfg, "ENABLE_MARKET_ALIGNMENT_FILTER", False) else "Disabled"),
        "adx_filter": "Observational" if getattr(cfg, "PROPOSED_CLEAN_PIPELINE", False) else ("Enabled" if getattr(cfg, "USE_ADX_FILTER", False) else "Disabled"),
        "entry_scheduler": f"{getattr(cfg, 'ENTRY_TIMEFRAME', '3minute')} Candle" if getattr(cfg, "ENABLE_CANDLE_ALIGNED_POLLING", False) else "Continuous",
        "pipeline": "MOMENTUM_RVOL_EMA_MARKET_POLICY" if getattr(cfg, "PROPOSED_CLEAN_PIPELINE", False) else "LEGACY",
        "raw_signal": "EMA9_EMA21_3MIN" if getattr(cfg, "PROPOSED_CLEAN_PIPELINE", False) else "LEGACY",
        "legacy_strategy_filters": "OBSERVATIONAL_ONLY" if getattr(cfg, "PROPOSED_CLEAN_PIPELINE", False) else "CONFIGURED",
        "position_monitor_interval_seconds": getattr(cfg, "POSITION_CHECK_SECONDS", None),
        "watchlist_size": len(getattr(cfg, "WATCHLIST", [])),
        "symbols_loaded": len(symbols),
        "open_positions": len(open_positions),
        "bot_uptime_seconds": time.time() - start_time,
        "version": get_git_commit_hash(),
        "git_commit_hash": get_git_commit_hash(),
    }

    try:
        margins = kite.margins()
        health["api_connection"] = "Authenticated" if margins else "Disconnected"
    except Exception as e:
        health["api_connection"] = "Disconnected"
        logger.warning(f"Health check: API connection check failed: {e}")

    try:
        import psutil
        process = psutil.Process()
        health["memory_usage_mb"] = process.memory_info().rss / (1024 * 1024)
        health["cpu_usage_pct"] = process.cpu_percent(interval=0.1)
    except Exception as e:
        health["memory_usage_mb"] = None
        health["cpu_usage_pct"] = None
        logger.warning(f"Health check: resource usage check failed: {e}")

    return health


def build_full_analytics_snapshot(kite, cfg, open_positions, symbols, start_time,
                                    previous_bot_status=None, todays_trades=None):
    """
    Top-level orchestration for the institutional dashboard: gathers
    one batched quote fetch, builds every position's analytics,
    aggregates the portfolio summary, computes today's session
    summary, and runs the health check. Returns (positions_list,
    portfolio_summary, session_summary, health) ready to pass into
    save_bot_status(). Never raises -- any single piece failing
    degrades gracefully rather than blocking the whole snapshot.
    """
    prev_positions_by_symbol = {}
    if previous_bot_status and previous_bot_status.get("positions"):
        prev_positions_by_symbol = {p["symbol"]: p for p in previous_bot_status["positions"]}

    try:
        quotes = fetch_batched_quotes(kite, open_positions)
    except Exception as e:
        logger.warning(f"Full analytics snapshot: batched quote fetch failed entirely: {e}")
        quotes = {}

    positions_list = []
    for symbol, position in open_positions.items():
        try:
            analytics = build_position_analytics(
                symbol, position, quotes,
                previous_status_entry=prev_positions_by_symbol.get(symbol),
                near_pct=getattr(cfg, "NEAR_TARGET_STOP_PCT", 0.25),
                stale_threshold_seconds=getattr(cfg, "PRICE_STALE_THRESHOLD_SECONDS", 60),
            )
            positions_list.append(analytics)
        except Exception as e:
            logger.warning(f"Analytics build failed for {symbol}, skipping this cycle: {e}")

    try:
        margins = kite.margins()
    except Exception:
        margins = None
    try:
        portfolio_summary = compute_portfolio_summary(positions_list, margins=margins)
    except Exception as e:
        logger.warning(f"Portfolio summary computation failed: {e}")
        portfolio_summary = {}

    try:
        session_summary = compute_session_summary(todays_trades or [])
    except Exception as e:
        logger.warning(f"Session summary computation failed: {e}")
        session_summary = {}

    try:
        health = compute_health_check(cfg, kite, open_positions, symbols, start_time)
    except Exception as e:
        logger.warning(f"Health check computation failed: {e}")
        health = {}

    return positions_list, portfolio_summary, session_summary, health


def classify_bot_freshness(generated_at, position_monitor_interval_seconds=25,
                            stale_threshold_seconds=60, offline_multiplier=2,
                            market_open="09:15", market_close="15:30", now=None):
    """
    Classifies overall bot status as MARKET_CLOSED, BOT_OFFLINE,
    STATUS_STALE, or LIVE, using Asia/Kolkata time. `generated_at` is
    the bot_status.json 'generated_at' ISO string (or a datetime).
    `now` can be injected for testing; defaults to the real current
    Asia/Kolkata time.

    MARKET_CLOSED takes priority over everything else -- weekends and
    outside market hours are never reported as BOT_OFFLINE, per the
    explicit requirement not to show an alarming offline warning
    merely because the trading day ended normally.
    """
    from datetime import datetime as dt, time as dtime
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    kolkata = ZoneInfo("Asia/Kolkata")
    if now is None:
        now = dt.now(kolkata)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=kolkata)

    if isinstance(generated_at, str):
        try:
            generated_dt = dt.fromisoformat(generated_at)
            if generated_dt.tzinfo is None:
                generated_dt = generated_dt.replace(tzinfo=kolkata)
        except ValueError:
            return {"status": "BOT_OFFLINE", "reason": "generated_at timestamp is unparseable",
                    "age_seconds": None}
    elif generated_at is None:
        return {"status": "BOT_OFFLINE", "reason": "no status has ever been recorded",
                "age_seconds": None}
    else:
        generated_dt = generated_at
        if generated_dt.tzinfo is None:
            generated_dt = generated_dt.replace(tzinfo=kolkata)

    is_weekend = now.weekday() >= 5  # 5=Saturday, 6=Sunday
    open_h, open_m = map(int, market_open.split(":"))
    close_h, close_m = map(int, market_close.split(":"))
    market_open_time = dtime(open_h, open_m)
    market_close_time = dtime(close_h, close_m)
    is_within_hours = market_open_time <= now.time() <= market_close_time

    if is_weekend or not is_within_hours:
        return {"status": "MARKET_CLOSED", "reason": "outside trading hours or weekend",
                "age_seconds": (now - generated_dt).total_seconds()}

    age_seconds = (now - generated_dt).total_seconds()
    offline_threshold = position_monitor_interval_seconds * offline_multiplier

    if age_seconds > offline_threshold:
        return {"status": "BOT_OFFLINE",
                "reason": f"no status update for {age_seconds:.0f}s (threshold {offline_threshold}s)",
                "age_seconds": age_seconds}
    if age_seconds > stale_threshold_seconds:
        return {"status": "STATUS_STALE",
                "reason": f"no status update for {age_seconds:.0f}s (threshold {stale_threshold_seconds}s)",
                "age_seconds": age_seconds}
    return {"status": "LIVE", "reason": "recently updated", "age_seconds": age_seconds}
