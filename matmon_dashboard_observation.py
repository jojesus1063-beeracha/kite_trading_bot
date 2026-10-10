#!/usr/bin/env python3

"""
Matmon dashboard telemetry.

OBSERVATION ONLY.

Converts Matmon's existing CLEAN MODE_FULL tick window into the
structured quote_evidence consumed by configure_app.py.

This module has no entry/exit/order authority.
"""

from __future__ import annotations

import math
import json
import logging
import threading
import time
from pathlib import Path

from matmon_microstructure import weighted_5_imbalance
from matmon_observation_log import append_observation

logger = logging.getLogger("matmon_dashboard_observation")
LIVE_SNAPSHOT = Path("runtime/matmon/observations/live_snapshot.json")


def _number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _tick_time(tick):
    return _number((tick or {}).get("received_at"))


def _ltp(tick):
    value = _number((tick or {}).get("last_price"))
    if value is None:
        value = _number((tick or {}).get("ltp"))
    return value


def _levels(tick):
    depth = (tick or {}).get("depth") or {}
    return depth.get("buy") or [], depth.get("sell") or []


def _best_prices(tick):
    buys, sells = _levels(tick)
    if not buys or not sells:
        return None, None

    try:
        bid = _number(buys[0].get("price"))
        ask = _number(sells[0].get("price"))
    except (AttributeError, IndexError):
        return None, None

    return bid, ask


def _l1_imbalance(tick):
    buys, sells = _levels(tick)
    if not buys or not sells:
        return None

    try:
        bid_qty = _number(buys[0].get("quantity"))
        ask_qty = _number(sells[0].get("quantity"))
    except (AttributeError, IndexError):
        return None

    if bid_qty is None or ask_qty is None:
        return None

    total = bid_qty + ask_qty
    if total <= 0:
        return None

    return (bid_qty - ask_qty) / total


def _microprice(tick):
    buys, sells = _levels(tick)
    bid, ask = _best_prices(tick)

    if bid is None or ask is None or not buys or not sells:
        return None

    try:
        bid_qty = _number(buys[0].get("quantity"))
        ask_qty = _number(sells[0].get("quantity"))
    except (AttributeError, IndexError):
        return None

    if bid_qty is None or ask_qty is None:
        return None

    total = bid_qty + ask_qty
    if total <= 0:
        return None

    # Standard opposite-side quantity weighted microprice.
    return (
        (ask * bid_qty) +
        (bid * ask_qty)
    ) / total


def _spread(tick):
    bid, ask = _best_prices(tick)

    if bid is None or ask is None:
        return None, None

    spread = ask - bid
    mid = (ask + bid) / 2.0

    spread_bps = (
        (spread / mid) * 10000.0
        if mid > 0
        else None
    )

    return spread, spread_bps


def _velocity(first_value, last_value, elapsed):
    if (
        first_value is None
        or last_value is None
        or elapsed is None
        or elapsed <= 0
    ):
        return None

    return (last_value - first_value) / elapsed


def build_quote_evidence(ticks):
    rows = [
        tick for tick in (ticks or ())
        if _tick_time(tick) is not None
    ]

    rows.sort(key=_tick_time)

    if not rows:
        return {
            "available": False,
            "sample_count": 0,
            "microstructure_samples": 0,
        }

    first = rows[0]
    last = rows[-1]

    first_ts = _tick_time(first)
    last_ts = _tick_time(last)

    elapsed = (
        last_ts - first_ts
        if first_ts is not None and last_ts is not None
        else None
    )

    first_bid, _ = _best_prices(first)
    last_bid, _ = _best_prices(last)

    _, first_ask = _best_prices(first)
    _, last_ask = _best_prices(last)

    first_ltp = _ltp(first)
    last_ltp = _ltp(last)

    first_micro = _microprice(first)
    last_micro = _microprice(last)

    first_l1 = _l1_imbalance(first)
    last_l1 = _l1_imbalance(last)

    first_w5 = weighted_5_imbalance(first)
    last_w5 = weighted_5_imbalance(last)

    last_spread, last_spread_bps = _spread(last)

    return {
        "available": True,
        "sample_count": len(rows),
        "microstructure_samples": len(rows),

        "first_received_at": first_ts,
        "last_received_at": last_ts,

        "first_ltp": first_ltp,
        "last_ltp": last_ltp,
        "ltp_change": (
            last_ltp - first_ltp
            if first_ltp is not None and last_ltp is not None
            else None
        ),
        "ltp_velocity_per_sec":
            _velocity(first_ltp, last_ltp, elapsed),

        "first_microprice": first_micro,
        "last_microprice": last_micro,
        "microprice_change": (
            last_micro - first_micro
            if first_micro is not None and last_micro is not None
            else None
        ),

        "first_l1_imbalance": first_l1,
        "last_l1_imbalance": last_l1,

        "first_weighted_5_imbalance": first_w5,
        "last_weighted_5_imbalance": last_w5,
        "weighted_5_imbalance_change": (
            last_w5 - first_w5
            if first_w5 is not None and last_w5 is not None
            else None
        ),

        "last_spread": last_spread,
        "last_spread_bps": last_spread_bps,

        # Bid/ask PRICE velocity over the CLEAN window.
        "bid_velocity_per_sec":
            _velocity(first_bid, last_bid, elapsed),
        "ask_velocity_per_sec":
            _velocity(first_ask, last_ask, elapsed),
    }


def record_dashboard_observation(
    *,
    symbol,
    direction,
    ticks,
    confirmed,
    accepted,
    reason,
    entry_timeframe=None,
    ema_fast_period=None,
    ema_slow_period=None,
    di_period=None,
):
    """
    Best-effort dashboard/research telemetry.

    Return value has no trading significance.
    """

    try:
        evidence = build_quote_evidence(ticks)

        return append_observation({
            "observation_id":
                f"{symbol}|{direction or 'UNKNOWN'}|{time.time_ns()}",
            "symbol": symbol,
            "direction": direction,
            "entry_timeframe": entry_timeframe,
            "ema_fast_period": ema_fast_period,
            "ema_slow_period": ema_slow_period,
            "di_period": di_period,
            "quote_confirmed": bool(confirmed),
            "quote_accepted": bool(accepted),
            "quote_reason": reason,
            "quote_evidence": evidence,
            "observation_only": True,
        })

    except Exception:
        # Telemetry must never influence Matmon.
        return False


class DashboardObservationPublisher:
    """Publish a bounded latest snapshot for every subscribed symbol.

    This runs independently of signal confirmation and has no order authority.
    One atomic JSON file is replaced each interval, avoiding an unbounded
    per-tick journal and keeping the dashboard read inexpensive.
    """

    def __init__(self, tick_buffer, symbols, *, interval_seconds=5.0,
                 lookback_seconds=10.0, output_path=LIVE_SNAPSHOT):
        self.tick_buffer = tick_buffer
        self.symbols = tuple(dict.fromkeys(symbols))
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.lookback_seconds = max(self.interval_seconds, float(lookback_seconds))
        self.output_path = Path(output_path)
        self._stop = threading.Event()
        self._thread = None

    def snapshot(self, *, now=None):
        now = time.time() if now is None else float(now)
        stocks = []
        for symbol in self.symbols:
            ticks = self.tick_buffer.ticks_received_since(
                symbol, now - self.lookback_seconds
            ) or []
            evidence = build_quote_evidence(ticks[-20:])
            stocks.append({
                "symbol": symbol,
                "direction": None,
                "recorded_at_epoch": now,
                "quote_evidence": evidence,
                "observation_only": True,
            })
        return {
            "schema_version": 1,
            "generated_at_epoch": now,
            "observation_only": True,
            "stocks": stocks,
        }

    def publish_once(self, *, now=None):
        payload = self.snapshot(now=now)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.output_path)
        return payload

    def _run(self):
        while not self._stop.is_set():
            try:
                self.publish_once()
            except Exception:
                logger.exception("MATMON DASHBOARD SNAPSHOT FAILED | OBSERVATION_ONLY=True")
            self._stop.wait(self.interval_seconds)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="matmon-dashboard-observer",
        )
        self._thread.start()

    def stop(self, timeout=2.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
