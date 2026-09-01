"""Socket-only equity direction and entry engine -- PAPER SHADOW ONLY.

The process subscribes to the frozen equity watchlist in KiteTicker FULL mode,
records every received tick with five-level depth, estimates aggressive flow
over 15/30/60-second windows, and simulates a fixed-risk hybrid 1R/2R trade.

Safety boundary: this module contains no broker order call. It refuses to run
unless the shared configuration is in PAPER mode. Its state and history live
under runtime/equity_socket_shadow and never touch open_positions.json,
trade_history.jsonl, RiskManager, or the existing paper strategy.
"""
from __future__ import annotations

import csv
import copy
import gzip
import json
import logging
import math
import queue
import shutil
import signal
import statistics
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import config as cfg

try:
    from costs import net_pnl_for_trade as _configured_net_pnl
except ImportError:  # isolated unit tests only; production has costs.py
    _configured_net_pnl = None


IST = ZoneInfo("Asia/Kolkata")
LOGGER = logging.getLogger("equity_socket_shadow")


def _now_ist() -> datetime:
    return datetime.now(IST)


def _json_value(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _depth_levels(tick: dict, side: str) -> list[dict]:
    levels = ((tick.get("depth") or {}).get(side) or [])[:5]
    clean = []
    for level in levels:
        clean.append({
            "price": float(level.get("price") or 0.0),
            "quantity": int(level.get("quantity") or 0),
            "orders": int(level.get("orders") or 0),
        })
    return clean


def _book_snapshot(tick: dict) -> Optional[dict]:
    bids = _depth_levels(tick, "buy")
    asks = _depth_levels(tick, "sell")
    if not bids or not asks:
        return None
    best_bid, best_ask = bids[0]["price"], asks[0]["price"]
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None
    bid_qty = sum(max(0, row["quantity"]) for row in bids)
    ask_qty = sum(max(0, row["quantity"]) for row in asks)
    total = bid_qty + ask_qty
    if total <= 0:
        return None
    mid = (best_bid + best_ask) / 2.0
    return {
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "book_imbalance": (bid_qty - ask_qty) / total,
        "spread_pct": ((best_ask - best_bid) / mid) * 100.0,
    }


class AsyncGzipJsonlRecorder:
    """Bounded asynchronous writer; queue loss permanently blocks entries."""

    def __init__(self, path: Path, max_queue: int = 20_000):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self.dropped = 0
        self.failed = False
        self._stop = object()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def healthy(self) -> bool:
        return not self.failed and self.dropped == 0

    def write(self, row: dict):
        try:
            self.queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1

    def _run(self):
        try:
            with gzip.open(self.path, "at", encoding="utf-8", compresslevel=3) as handle:
                pending = 0
                while True:
                    item = self.queue.get()
                    if item is self._stop:
                        break
                    handle.write(json.dumps(item, default=_json_value, separators=(",", ":")))
                    handle.write("\n")
                    pending += 1
                    if pending >= 500:
                        handle.flush()
                        pending = 0
                handle.flush()
        except Exception:
            self.failed = True
            LOGGER.exception("Raw tick recorder failed; new shadow entries are blocked")

    def close(self):
        while True:
            try:
                self.queue.put(self._stop, timeout=1.0)
                break
            except queue.Full:
                continue
        self._thread.join(timeout=30.0)


@dataclass
class FlowPoint:
    received_at: float
    price: float
    best_bid: float
    best_ask: float
    buy_volume: int
    sell_volume: int
    book_imbalance: float
    spread_pct: float
    aggressor: str
    volume_delta: int
    offer_depletion: bool
    bid_depletion: bool
    bid_replenishment: bool
    offer_replenishment: bool


@dataclass
class ShadowPosition:
    symbol: str
    exchange: str
    direction: str
    quantity: int
    entry: float
    stop: float
    scalp_target: float
    runner_target: float
    scalp_remaining: int
    runner_remaining: int
    entered_at: str
    signal_metrics: dict
    realized_net: float = 0.0


@dataclass
class SymbolState:
    points: deque = field(default_factory=lambda: deque(maxlen=3_000))
    previous_tick: Optional[dict] = None
    previous_volume: Optional[int] = None
    last_evaluation_at: float = 0.0
    trades_today: int = 0


def _fallback_net_pnl(direction: str, qty: int, entry: float, exit_price: float) -> dict:
    sign = 1.0 if direction == "BUY" else -1.0
    gross = (exit_price - entry) * qty * sign
    # Used only when the production cost module is unavailable.
    costs = qty * (entry + exit_price) * 0.0002517818
    return {"gross_pnl": gross, "costs": costs, "net_pnl": gross - costs}


def _net_pnl(direction: str, qty: int, entry: float, exit_price: float) -> dict:
    if _configured_net_pnl is not None:
        return _configured_net_pnl(direction, qty, entry, exit_price)
    return _fallback_net_pnl(direction, qty, entry, exit_price)


class SocketShadowEngine:
    def __init__(self, config=cfg, recorder=None, output_dir: Optional[Path] = None):
        if not bool(getattr(config, "PAPER_TRADING", False)):
            raise RuntimeError("SAFETY BLOCK: equity socket shadow requires PAPER_TRADING=True")
        self.cfg = config
        self.output_dir = Path(output_dir or getattr(
            config, "SOCKET_SHADOW_OUTPUT_DIR", "runtime/equity_socket_shadow"
        ))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.session_date = _now_ist().date().isoformat()
        raw_path = self.output_dir / f"ticks_{self.session_date}.jsonl.gz"
        self.recorder = recorder or AsyncGzipJsonlRecorder(raw_path)
        self.states: dict[str, SymbolState] = defaultdict(SymbolState)
        self.positions: dict[str, ShadowPosition] = {}
        self.dynamic_adverse_since: dict[str, float] = {}
        self.fallback_armed: dict[str, dict] = {}
        self.latest_fallback_quotes: dict[str, dict] = {}
        self.fallback_arm_count = 0
        self.stale_check_counts: dict[str, int] = defaultdict(int)
        self.events_path = self.output_dir / f"events_{self.session_date}.jsonl"
        self.legs_path = self.output_dir / f"trade_legs_{self.session_date}.csv"
        self.lock = threading.RLock()
        self.total_trades = 0
        self.realized_net = 0.0
        self.entry_halted = False
        self.entry_halt_reason = None
        self.tick_count = 0
        self.direction_counts = defaultdict(int)
        self.score_cohorts = defaultdict(int)
        self.spread_bands = defaultdict(int)
        self.depth_inference_counts = defaultdict(int)

    def _event(self, event: str, **detail):
        row = {"event": event, "logged_at": _now_ist().isoformat(), **detail}
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, default=_json_value, separators=(",", ":")) + "\n")

    def _record_raw(self, symbol: str, tick: dict, book: Optional[dict], aggressor: str,
                    volume_delta: int, depth_inference: str):
        if not bool(getattr(self.cfg, "SOCKET_SHADOW_RECORD_RAW_TICKS", True)):
            return
        self.recorder.write({
            "received_at": tick.get("received_at"),
            "exchange_timestamp": tick.get("exchange_timestamp"),
            "instrument_token": tick.get("instrument_token"),
            "symbol": symbol,
            "last_price": tick.get("last_price"),
            "last_quantity": tick.get("last_quantity"),
            "volume_traded": tick.get("volume_traded"),
            "aggressor": aggressor,
            "classified_volume": volume_delta,
            "depth_change_inference": depth_inference,
            "depth": None if book is None else {"buy": book["bids"], "sell": book["asks"]},
        })

    def _classify_aggressor(self, price: float, previous: Optional[dict], now: float) -> str:
        if previous is None:
            return "UNKNOWN"
        previous_received = float(previous.get("received_at") or 0.0)
        if now - previous_received > float(getattr(self.cfg, "SOCKET_SHADOW_QUOTE_MAX_AGE_SECONDS", 0.5)):
            return "UNKNOWN"
        previous_book = _book_snapshot(previous)
        previous_price = float(previous.get("last_price") or 0.0)
        if previous_book is not None:
            if price >= previous_book["best_ask"]:
                return "BUY"
            if price <= previous_book["best_bid"]:
                return "SELL"
        if price > previous_price:
            return "BUY"
        if price < previous_price:
            return "SELL"
        return "UNKNOWN"

    def on_tick(self, symbol: str, tick: dict):
        now = float(tick.get("received_at") or time.time())
        try:
            price = float(tick.get("last_price") or 0.0)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return
        book = _book_snapshot(tick)
        with self.lock:
            state = self.states[symbol]
            previous_book = _book_snapshot(state.previous_tick or {})
            aggressor = self._classify_aggressor(price, state.previous_tick, now)
            try:
                cumulative = int(tick.get("volume_traded") or 0)
            except (TypeError, ValueError):
                cumulative = 0
            volume_delta = 0
            if state.previous_volume is not None and cumulative >= state.previous_volume:
                volume_delta = cumulative - state.previous_volume
            if cumulative > 0:
                state.previous_volume = cumulative
            self.tick_count += 1
            depth_inference = "AMBIGUOUS"
            if book is not None:
                offer_depletion = bool(
                    previous_book and book["ask_qty"] < previous_book["ask_qty"]
                    and volume_delta > 0 and aggressor == "BUY"
                )
                bid_depletion = bool(
                    previous_book and book["bid_qty"] < previous_book["bid_qty"]
                    and volume_delta > 0 and aggressor == "SELL"
                )
                bid_replenishment = bool(
                    previous_book and book["bid_qty"] > previous_book["bid_qty"]
                    and book["best_bid"] >= previous_book["best_bid"]
                )
                offer_replenishment = bool(
                    previous_book and book["ask_qty"] > previous_book["ask_qty"]
                    and book["best_ask"] <= previous_book["best_ask"]
                )
                side_depleted = bool(
                    previous_book and (
                        book["ask_qty"] < previous_book["ask_qty"]
                        or book["bid_qty"] < previous_book["bid_qty"]
                    )
                )
                if side_depleted and volume_delta > 0 and aggressor in {"BUY", "SELL"}:
                    depth_inference = "LIKELY_EXECUTED"
                elif side_depleted and volume_delta == 0:
                    depth_inference = "LIKELY_CANCELLED"
                state.points.append(FlowPoint(
                    received_at=now,
                    price=price,
                    best_bid=book["best_bid"],
                    best_ask=book["best_ask"],
                    buy_volume=volume_delta if aggressor == "BUY" else 0,
                    sell_volume=volume_delta if aggressor == "SELL" else 0,
                    book_imbalance=book["book_imbalance"],
                    spread_pct=book["spread_pct"],
                    aggressor=aggressor,
                    volume_delta=volume_delta,
                    offer_depletion=offer_depletion,
                    bid_depletion=bid_depletion,
                    bid_replenishment=bid_replenishment,
                    offer_replenishment=offer_replenishment,
                ))
                cutoff = now - 35.0
                while state.points and state.points[0].received_at < cutoff:
                    state.points.popleft()
            self._record_raw(symbol, tick, book, aggressor, volume_delta, depth_inference)
            self.depth_inference_counts[depth_inference] += 1
            state.previous_tick = tick
            self._manage_authoritative_exit(symbol, price, book, now)
            self._maybe_evaluate_entry(symbol, book, now)

    def _window(self, state: SymbolState, now: float) -> Optional[dict]:
        seconds = float(getattr(self.cfg, "SOCKET_SHADOW_OBSERVATION_SECONDS", 30.0))
        points = [point for point in state.points if point.received_at >= now - seconds]
        if len(points) < int(getattr(self.cfg, "SOCKET_SHADOW_MIN_WINDOW_SAMPLES", 5)):
            return None
        coverage = points[-1].received_at - points[0].received_at
        if coverage < float(getattr(self.cfg, "SOCKET_SHADOW_MIN_COVERAGE_SECONDS", 29.0)):
            return None
        # Preserve raw ticks, but use the final update in each second for depth
        # median and persistence so bursty symbols do not dominate the book.
        per_second = {int(point.received_at): point for point in points}
        depth_points = [per_second[key] for key in sorted(per_second)]
        if len(depth_points) < int(getattr(self.cfg, "SOCKET_SHADOW_MIN_WINDOW_SAMPLES", 15)):
            return None
        imbalances = [p.book_imbalance for p in depth_points]
        threshold = float(getattr(self.cfg, "SOCKET_SHADOW_IMBALANCE_THRESHOLD", 0.25))
        buy_volume = sum(p.buy_volume for p in points)
        sell_volume = sum(p.sell_volume for p in points)
        unknown_volume = sum(p.volume_delta for p in points if p.aggressor == "UNKNOWN")
        changes = []
        return_pcts = []
        for previous, current in zip(points, points[1:]):
            if previous.price > 0:
                return_pcts.append((current.price - previous.price) / previous.price * 100.0)
            if current.price > previous.price:
                changes.append(1)
            elif current.price < previous.price:
                changes.append(-1)
        first, latest = points[0], points[-1]
        price_change = ((latest.price - first.price) / first.price) * 100.0
        fixed_minimum = float(getattr(self.cfg, "SOCKET_SHADOW_MIN_PRICE_MOVE_PCT", 0.05))
        tick_size = float(getattr(self.cfg, "SOCKET_SHADOW_DEFAULT_TICK_SIZE", 0.05))
        tick_minimum = (2.0 * tick_size / first.price) * 100.0
        fixed_extension = float(getattr(self.cfg, "SOCKET_SHADOW_MAX_EXTENSION_PCT", 0.20))
        realized_volatility = (
            statistics.pstdev(return_pcts) * math.sqrt(len(return_pcts))
            if len(return_pcts) >= 2 else 0.0
        )
        volatility_ceiling = max(
            2.0 * max(fixed_minimum, tick_minimum),
            3.0 * realized_volatility,
        )
        return {
            "seconds": seconds,
            "sample_count": len(points),
            "coverage_seconds": coverage,
            "buy_volume": buy_volume,
            "sell_volume": sell_volume,
            "unknown_volume": unknown_volume,
            "median_imbalance": statistics.median(imbalances),
            "buy_persistence": sum(x >= threshold for x in imbalances) / len(imbalances),
            "sell_persistence": sum(x <= -threshold for x in imbalances) / len(imbalances),
            "price_change_pct": price_change,
            "minimum_move_pct": max(fixed_minimum, tick_minimum),
            "fixed_extension_pct": fixed_extension,
            "realized_volatility_pct": realized_volatility,
            "volatility_ceiling_pct": volatility_ceiling,
            "up_tick_ratio": sum(x > 0 for x in changes) / len(changes) if changes else 0.0,
            "down_tick_ratio": sum(x < 0 for x in changes) / len(changes) if changes else 0.0,
            "offer_depletion_cycles": sum(p.offer_depletion for p in points),
            "bid_depletion_cycles": sum(p.bid_depletion for p in points),
            "bid_replenishment_cycles": sum(p.bid_replenishment for p in points),
            "offer_replenishment_cycles": sum(p.offer_replenishment for p in points),
            "latest_spread_pct": latest.spread_pct,
        }

    def direction_metrics(self, symbol: str, now: Optional[float] = None) -> Optional[dict]:
        now = time.time() if now is None else float(now)
        state = self.states[symbol]
        window = self._window(state, now)
        if window is None:
            return None
        imbalance = float(getattr(self.cfg, "SOCKET_SHADOW_IMBALANCE_THRESHOLD", 0.25))
        persistence = float(getattr(self.cfg, "SOCKET_SHADOW_PERSISTENCE_RATIO", 0.75))
        tick_ratio = float(getattr(self.cfg, "SOCKET_SHADOW_DIRECTIONAL_TICK_RATIO", 0.60))
        extension = min(
            float(getattr(self.cfg, "SOCKET_SHADOW_MAX_EXTENSION_PCT", 0.20)),
            window["volatility_ceiling_pct"],
        )
        depletion = int(getattr(self.cfg, "SOCKET_SHADOW_MIN_DEPLETION_CYCLES", 2))
        replenishment = int(getattr(self.cfg, "SOCKET_SHADOW_MIN_REPLENISHMENT_CYCLES", 2))
        buy_checks = {
            "depth_persistence": window["median_imbalance"] >= imbalance and window["buy_persistence"] >= persistence,
            "price_response": window["minimum_move_pct"] <= window["price_change_pct"] <= extension,
            "directional_ticks": window["up_tick_ratio"] >= tick_ratio,
            "aggressor_volume": window["buy_volume"] > window["sell_volume"] and window["buy_volume"] > 0,
            "depletion": window["offer_depletion_cycles"] >= depletion,
            "replenishment": window["bid_replenishment_cycles"] >= replenishment,
        }
        sell_checks = {
            "depth_persistence": window["median_imbalance"] <= -imbalance and window["sell_persistence"] >= persistence,
            "price_response": -extension <= window["price_change_pct"] <= -window["minimum_move_pct"],
            "directional_ticks": window["down_tick_ratio"] >= tick_ratio,
            "aggressor_volume": window["sell_volume"] > window["buy_volume"] and window["sell_volume"] > 0,
            "depletion": window["bid_depletion_cycles"] >= depletion,
            "replenishment": window["offer_replenishment_cycles"] >= replenishment,
        }
        weights = {"depth_persistence": 20, "price_response": 15, "directional_ticks": 15,
                   "aggressor_volume": 20, "depletion": 10, "replenishment": 10}
        buy_score = sum(weights[k] for k, passed in buy_checks.items() if passed)
        sell_score = sum(weights[k] for k, passed in sell_checks.items() if passed)
        if all(buy_checks.values()):
            direction = "BUY"
        elif all(sell_checks.values()):
            direction = "SELL"
        else:
            direction = "SKIP"
        return {
            "direction": direction,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "buy_checks": buy_checks,
            "sell_checks": sell_checks,
            "window_30": window,
        }

    def _within_entry_window(self) -> bool:
        current = _now_ist().time()
        start = datetime.strptime(str(getattr(self.cfg, "NO_ENTRY_BEFORE", "09:30")), "%H:%M").time()
        end = datetime.strptime(str(getattr(self.cfg, "NO_ENTRY_AFTER", "15:05")), "%H:%M").time()
        return start <= current <= end

    def _maybe_evaluate_entry(self, symbol: str, book: Optional[dict], now: float):
        state = self.states[symbol]
        interval = float(getattr(self.cfg, "SOCKET_SHADOW_EVALUATION_SECONDS", 15.0))
        if now - state.last_evaluation_at < interval:
            return
        state.last_evaluation_at = now
        metrics = self.direction_metrics(symbol, now)
        if metrics is None:
            return
        proposed = metrics["direction"]
        self.direction_counts[proposed] += 1
        accepted = proposed in {"BUY", "SELL"}
        reason = None
        if not accepted:
            reason = "AMBIGUOUS_FLOW"
        elif not self.recorder.healthy:
            reason = "RAW_RECORDER_UNHEALTHY"
        elif self.entry_halted:
            reason = self.entry_halt_reason or "ENTRY_HALTED"
        elif not self._within_entry_window():
            reason = "OUTSIDE_ENTRY_WINDOW"
        elif symbol in self.positions:
            reason = "POSITION_ALREADY_OPEN"
        elif state.trades_today >= int(getattr(self.cfg, "SOCKET_SHADOW_MAX_TRADES_PER_SYMBOL", 1)):
            reason = "MAX_TRADES_PER_SYMBOL"
        elif self.total_trades >= int(getattr(self.cfg, "SOCKET_SHADOW_MAX_TRADES_PER_DAY", 100)):
            reason = "MAX_TRADES_PER_DAY"
        elif len(self.positions) >= int(getattr(self.cfg, "SOCKET_SHADOW_MAX_OPEN_POSITIONS", 5)):
            reason = "MAX_OPEN_POSITIONS"
        elif book is None:
            reason = "DEPTH_UNAVAILABLE"
        else:
            latest_age = max(0.0, now - self.states[symbol].points[-1].received_at)
            if latest_age > float(getattr(self.cfg, "SOCKET_SHADOW_TICK_MAX_AGE_SECONDS", 2.0)):
                reason = "STALE_TICK"
            elif book["spread_pct"] > float(getattr(self.cfg, "SOCKET_SHADOW_MAX_SPREAD_PCT", 0.05)):
                reason = "SPREAD_TOO_WIDE"
        metrics["execution_quality_pass"] = reason is None
        metrics["strict_pass"] = accepted and reason is None
        metrics["order_flow_score"] = max(metrics["buy_score"], metrics["sell_score"]) + (10 if reason is None else 0)
        score = metrics["order_flow_score"]
        cohort = "BELOW_60" if score < 60 else ("90_100" if score >= 90 else f"{int(score // 10) * 10}_{int(score // 10) * 10 + 9}")
        self.score_cohorts[cohort] += 1
        if book is not None:
            spread = book["spread_pct"]
            self.spread_bands["LE_5_BPS" if spread <= .05 else ("5_10_BPS" if spread <= .10 else "GT_10_BPS")] += 1
        self._event("DIRECTION_EVALUATION", symbol=symbol, accepted=metrics["strict_pass"],
                    rejection_reason=reason, metrics=metrics)
        if accepted and reason is None:
            self._open_position(symbol, proposed, book, metrics, now)

    def _open_position(self, symbol: str, direction: str, book: dict, metrics: dict, now: float):
        entry = book["best_ask"] if direction == "BUY" else book["best_bid"]
        latest = self.states[symbol].points[-1]
        slippage = abs(entry - latest.price) / latest.price * 100.0
        if slippage > float(getattr(self.cfg, "SOCKET_SHADOW_MAX_SLIPPAGE_PCT", 0.15)):
            self._event("ENTRY_REJECTED", symbol=symbol, reason="SLIPPAGE_TOO_HIGH", slippage_pct=slippage)
            return
        stop_pct = float(getattr(self.cfg, "SOCKET_SHADOW_STOP_PERCENT", 0.45)) / 100.0
        risk_per_share = entry * stop_pct
        capital = float(getattr(self.cfg, "SOCKET_SHADOW_CAPITAL", 5000.0))
        risk_budget = capital * float(getattr(self.cfg, "RISK_PER_TRADE_PCT", 1.0)) / 100.0
        notional_cap = capital
        risk_qty = math.floor(risk_budget / risk_per_share) if risk_per_share > 0 else 0
        notional_qty = math.floor(notional_cap / entry) if entry > 0 else 0
        executable_qty = book["ask_qty"] if direction == "BUY" else book["bid_qty"]
        liquidity_qty = math.floor(executable_qty / float(
            getattr(self.cfg, "SOCKET_SHADOW_EXECUTABLE_DEPTH_MULTIPLE", 2.0)
        ))
        quantity = max(0, min(risk_qty, notional_qty, liquidity_qty))
        if quantity < 2:
            self._event("ENTRY_REJECTED", symbol=symbol, reason="QUANTITY_BELOW_TWO", quantity=quantity)
            return
        sign = 1.0 if direction == "BUY" else -1.0
        scalp_qty = math.floor(quantity * float(getattr(self.cfg, "SOCKET_SHADOW_SCALP_FRACTION", 0.50)))
        scalp_qty = max(1, min(quantity - 1, scalp_qty))
        runner_qty = quantity - scalp_qty
        position = ShadowPosition(
            symbol=symbol,
            exchange="NSE",
            direction=direction,
            quantity=quantity,
            entry=entry,
            stop=entry - sign * risk_per_share,
            scalp_target=entry + sign * risk_per_share * float(getattr(self.cfg, "SOCKET_SHADOW_SCALP_R", 1.0)),
            runner_target=entry + sign * risk_per_share * float(getattr(self.cfg, "SOCKET_SHADOW_RUNNER_R", 2.0)),
            scalp_remaining=scalp_qty,
            runner_remaining=runner_qty,
            entered_at=datetime.fromtimestamp(now, IST).isoformat(),
            signal_metrics=metrics,
        )
        self.positions[symbol] = position
        state = self.states[symbol]
        state.trades_today += 1
        self.total_trades += 1
        self._event("SHADOW_ENTRY", symbol=symbol, direction=direction, quantity=quantity,
                    entry=entry, stop=position.stop, scalp_target=position.scalp_target,
                    runner_target=position.runner_target, slippage_pct=slippage, metrics=metrics)

    def _exit_leg(self, position: ShadowPosition, qty: int, exit_price: float, reason: str, now: float):
        result = _net_pnl(position.direction, qty, position.entry, exit_price)
        position.realized_net += float(result["net_pnl"])
        self.realized_net += float(result["net_pnl"])
        write_header = not self.legs_path.exists()
        with self.legs_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "date", "time", "symbol", "direction", "qty", "entry", "exit",
                "gross_pnl", "costs", "net_pnl", "result",
            ])
            if write_header:
                writer.writeheader()
            writer.writerow({
                "date": self.session_date,
                "time": datetime.fromtimestamp(now, IST).isoformat(),
                "symbol": position.symbol,
                "direction": position.direction,
                "qty": qty,
                "entry": position.entry,
                "exit": exit_price,
                "gross_pnl": result["gross_pnl"],
                "costs": result["costs"],
                "net_pnl": result["net_pnl"],
                "result": reason,
            })
        self._event("SHADOW_EXIT_LEG", symbol=position.symbol, direction=position.direction,
                    quantity=qty, entry=position.entry, exit=exit_price, reason=reason, pnl=result)
        daily_loss_pct = getattr(self.cfg, "SOCKET_SHADOW_MAX_DAILY_LOSS_PCT", None)
        daily_limit = None if daily_loss_pct is None else (
            float(getattr(self.cfg, "SOCKET_SHADOW_CAPITAL", 5000.0)) * float(daily_loss_pct) / 100.0
        )
        if daily_limit is not None and daily_limit > 0 and self.realized_net <= -daily_limit:
            self.entry_halted = True
            self.entry_halt_reason = "MAX_DAILY_LOSS"

    def _manage_position(self, symbol: str, price: float, book: Optional[dict], now: float):
        position = self.positions.get(symbol)
        if position is None:
            return
        executable = price
        if book is not None:
            executable = book["best_bid"] if position.direction == "BUY" else book["best_ask"]
        stop_hit = executable <= position.stop if position.direction == "BUY" else executable >= position.stop
        if stop_hit:
            qty = position.scalp_remaining + position.runner_remaining
            reason = "break_even_stop" if math.isclose(position.stop, position.entry) else "stop"
            self._exit_leg(position, qty, executable, reason, now)
            del self.positions[symbol]
            return
        scalp_hit = executable >= position.scalp_target if position.direction == "BUY" else executable <= position.scalp_target
        if position.scalp_remaining and scalp_hit:
            qty = position.scalp_remaining
            position.scalp_remaining = 0
            self._exit_leg(position, qty, executable, "hybrid_scalp_1r", now)
            position.stop = position.entry
        runner_hit = executable >= position.runner_target if position.direction == "BUY" else executable <= position.runner_target
        if position.runner_remaining and runner_hit:
            qty = position.runner_remaining
            position.runner_remaining = 0
            self._exit_leg(position, qty, executable, "hybrid_runner_2r", now)
        if position.scalp_remaining + position.runner_remaining == 0:
            del self.positions[symbol]

    def _dynamic_adverse_confirmed(self, symbol: str, direction: str, now: float) -> tuple[bool, dict]:
        seconds = float(getattr(self.cfg, "SOCKET_SHADOW_DYNAMIC_EXIT_WINDOW_SECONDS", 10.0))
        points = [p for p in self.states[symbol].points if p.received_at >= now - seconds]
        if len(points) < 5 or points[-1].received_at - points[0].received_at < seconds * 0.8:
            return False, {"reason": "INSUFFICIENT_EXIT_WINDOW"}
        per_second = {int(p.received_at): p for p in points}
        snapshots = [per_second[key] for key in sorted(per_second)]
        median_imbalance = statistics.median(p.book_imbalance for p in snapshots)
        buy_volume = sum(p.buy_volume for p in points)
        sell_volume = sum(p.sell_volume for p in points)
        changes = []
        for previous, current in zip(points, points[1:]):
            if current.price > previous.price:
                changes.append(1)
            elif current.price < previous.price:
                changes.append(-1)
        up_ratio = sum(x > 0 for x in changes) / len(changes) if changes else 0.0
        down_ratio = sum(x < 0 for x in changes) / len(changes) if changes else 0.0
        midpoint = sum(p.price for p in points) / len(points)
        threshold = float(getattr(self.cfg, "SOCKET_SHADOW_DYNAMIC_EXIT_IMBALANCE", 0.15))
        tick_ratio = float(getattr(self.cfg, "SOCKET_SHADOW_DYNAMIC_EXIT_TICK_RATIO", 0.60))
        min_depletion = int(getattr(self.cfg, "SOCKET_SHADOW_DYNAMIC_EXIT_MIN_DEPLETION", 2))
        min_replenishment = int(getattr(self.cfg, "SOCKET_SHADOW_DYNAMIC_EXIT_MIN_REPLENISHMENT", 2))
        if direction == "BUY":
            checks = {
                "opposite_imbalance": median_imbalance <= -threshold,
                "opposite_volume": sell_volume > buy_volume and sell_volume > 0,
                "opposite_ticks": down_ratio >= tick_ratio,
                "bid_depletion": sum(p.bid_depletion for p in points) >= min_depletion,
                "offer_replenishment": sum(p.offer_replenishment for p in points) >= min_replenishment,
                "price_response": points[-1].price < midpoint,
            }
        else:
            checks = {
                "opposite_imbalance": median_imbalance >= threshold,
                "opposite_volume": buy_volume > sell_volume and buy_volume > 0,
                "opposite_ticks": up_ratio >= tick_ratio,
                "offer_depletion": sum(p.offer_depletion for p in points) >= min_depletion,
                "bid_replenishment": sum(p.bid_replenishment for p in points) >= min_replenishment,
                "price_response": points[-1].price > midpoint,
            }
        detail = {
            "checks": checks, "median_imbalance": median_imbalance,
            "buy_volume": buy_volume, "sell_volume": sell_volume,
            "up_tick_ratio": up_ratio, "down_tick_ratio": down_ratio,
            "price_midpoint": midpoint,
        }
        return all(checks.values()), detail

    def arm_stale_fallbacks(self, now: Optional[float] = None,
                            reason: Optional[str] = None) -> list[str]:
        """Permanently move stale/disconnected positions to the REST hybrid bracket."""
        now = time.time() if now is None else float(now)
        armed = []
        max_age = float(getattr(self.cfg, "SOCKET_SHADOW_TICK_MAX_AGE_SECONDS", 2.0))
        with self.lock:
            for symbol, position in self.positions.items():
                if symbol in self.fallback_armed:
                    armed.append(symbol)
                    continue
                state = self.states[symbol]
                latest_at = state.points[-1].received_at if state.points else 0.0
                age = max(0.0, now - latest_at) if latest_at else float("inf")
                if reason:
                    fallback_reason = reason
                elif age > max_age:
                    self.stale_check_counts[symbol] += 1
                    required = int(getattr(
                        self.cfg, "SOCKET_SHADOW_STALE_CONSECUTIVE_CHECKS", 2
                    ))
                    fallback_reason = (
                        "STALE_TICK" if self.stale_check_counts[symbol] >= required else None
                    )
                else:
                    self.stale_check_counts.pop(symbol, None)
                    fallback_reason = None
                if fallback_reason is None:
                    continue
                self.fallback_armed[symbol] = {
                    "armed_at": now,
                    "reason": fallback_reason,
                    "tick_age_seconds": age,
                    "stop": position.stop,
                    "scalp_target": position.scalp_target,
                    "runner_target": position.runner_target,
                }
                self.dynamic_adverse_since.pop(symbol, None)
                self.stale_check_counts.pop(symbol, None)
                self.fallback_arm_count += 1
                armed.append(symbol)
                self._event("STALE_HYBRID_FALLBACK_ARMED", symbol=symbol,
                            reason=fallback_reason, tick_age_seconds=age,
                            stop=position.stop, scalp_target=position.scalp_target,
                            runner_target=position.runner_target)
        return armed

    def _manage_fallback_hybrid(self, symbol: str, executable: float, now: float,
                                source: str):
        position = self.positions.get(symbol)
        if position is None:
            return
        stop_hit = executable <= position.stop if position.direction == "BUY" else executable >= position.stop
        if stop_hit:
            qty = position.scalp_remaining + position.runner_remaining
            reason = "stale_fallback_break_even_stop" if math.isclose(position.stop, position.entry) else "stale_fallback_stop"
            self._exit_leg(position, qty, executable, f"{reason}_{source}", now)
            del self.positions[symbol]
            self.fallback_armed.pop(symbol, None)
            self.stale_check_counts.pop(symbol, None)
            self.dynamic_adverse_since.pop(symbol, None)
            return
        scalp_hit = executable >= position.scalp_target if position.direction == "BUY" else executable <= position.scalp_target
        if position.scalp_remaining and scalp_hit:
            qty = position.scalp_remaining
            position.scalp_remaining = 0
            self._exit_leg(position, qty, executable, f"stale_fallback_scalp_1r_{source}", now)
            position.stop = position.entry
        runner_hit = executable >= position.runner_target if position.direction == "BUY" else executable <= position.runner_target
        if position.runner_remaining and runner_hit:
            qty = position.runner_remaining
            position.runner_remaining = 0
            self._exit_leg(position, qty, executable, f"stale_fallback_runner_2r_{source}", now)
        if position.scalp_remaining + position.runner_remaining == 0:
            del self.positions[symbol]
            self.fallback_armed.pop(symbol, None)
            self.stale_check_counts.pop(symbol, None)
            self.dynamic_adverse_since.pop(symbol, None)
        
    def on_fallback_quote(self, symbol: str, quote: dict, now: Optional[float] = None):
        """Manage an already-armed position from a read-only REST quote."""
        now = time.time() if now is None else float(now)
        with self.lock:
            position = self.positions.get(symbol)
            if position is None or symbol not in self.fallback_armed:
                return
            last_price = float(quote.get("last_price") or 0.0)
            depth = quote.get("depth") or {}
            buys = depth.get("buy") or []
            sells = depth.get("sell") or []
            best_bid = float((buys[0] if buys else {}).get("price") or last_price)
            best_ask = float((sells[0] if sells else {}).get("price") or last_price)
            executable = best_bid if position.direction == "BUY" else best_ask
            if executable <= 0:
                self._event("FALLBACK_QUOTE_REJECTED", symbol=symbol, reason="NO_EXECUTABLE_PRICE")
                return
            self.latest_fallback_quotes[symbol] = {
                "received_at": now, "last_price": last_price,
                "best_bid": best_bid, "best_ask": best_ask,
            }
            self._manage_fallback_hybrid(symbol, executable, now, "rest")

    def _manage_authoritative_exit(self, symbol: str, price: float,
                                   book: Optional[dict], now: float):
        position = self.positions.get(symbol)
        if position is None:
            return
        executable = price if book is None else (
            book["best_bid"] if position.direction == "BUY" else book["best_ask"]
        )
        if symbol in self.fallback_armed:
            self._manage_fallback_hybrid(symbol, executable, now, "websocket")
            return
        adverse, detail = self._dynamic_adverse_confirmed(symbol, position.direction, now)
        if not adverse:
            self.dynamic_adverse_since.pop(symbol, None)
            return
        since = self.dynamic_adverse_since.setdefault(symbol, now)
        persistence = float(getattr(self.cfg, "SOCKET_SHADOW_DYNAMIC_EXIT_PERSIST_SECONDS", 10.0))
        if now - since < persistence:
            return
        qty = position.scalp_remaining + position.runner_remaining
        self._exit_leg(position, qty, executable, "dynamic_adverse_flow", now)
        self._event("DYNAMIC_ADVERSE_CONFIRMED", symbol=symbol,
                    persistence_seconds=now - since, detail=detail)
        del self.positions[symbol]
        self.stale_check_counts.pop(symbol, None)
        self.dynamic_adverse_since.pop(symbol, None)

    def square_off(self):
        with self.lock:
            now = time.time()
            for symbol, position in list(self.positions.items()):
                fallback_quote = self.latest_fallback_quotes.get(symbol)
                state = self.states[symbol]
                if fallback_quote:
                    price = (fallback_quote["best_bid"] if position.direction == "BUY"
                             else fallback_quote["best_ask"])
                elif state.points:
                    latest = state.points[-1]
                    price = latest.best_bid if position.direction == "BUY" else latest.best_ask
                else:
                    self._event("SQUARE_OFF_BLOCKED", symbol=symbol, reason="NO_TICK")
                    continue
                qty = position.scalp_remaining + position.runner_remaining
                self._exit_leg(position, qty, price, "square_off", now)
                del self.positions[symbol]
                self.fallback_armed.pop(symbol, None)
                self.stale_check_counts.pop(symbol, None)
                self.dynamic_adverse_since.pop(symbol, None)

    def summary(self) -> dict:
        existing_net = 0.0
        existing_legs = 0
        path = Path("trade_history.jsonl")
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if str(row.get("date") or "").startswith(self.session_date):
                    existing_net += float(row.get("pnl") or 0.0)
                    existing_legs += 1
        return {
            "date": self.session_date,
            "paper_shadow_only": True,
            "ema_dependency": False,
            "vwap_dependency": False,
            "tick_count": self.tick_count,
            "raw_recorder_healthy": self.recorder.healthy,
            "raw_records_dropped": self.recorder.dropped,
            "shadow_entries": self.total_trades,
            "shadow_open_positions": len(self.positions),
            "shadow_realized_net_pnl": self.realized_net,
            "exit_policy": "DYNAMIC_FLOW_THEN_STALE_HYBRID_0_45_1R_2R",
            "dynamic_exit_authoritative": True,
            "fixed_targets_while_fresh": False,
            "stale_fallback_hybrid_enabled": True,
            "fallback_armed_open_positions": len(self.fallback_armed),
            "fallback_armed_total": self.fallback_arm_count,
            "existing_strategy_exit_legs": existing_legs,
            "existing_strategy_net_pnl": existing_net,
            "net_pnl_difference": self.realized_net - existing_net,
            "entry_halted": self.entry_halted,
            "entry_halt_reason": self.entry_halt_reason,
            "direction_evaluations": dict(self.direction_counts),
            "score_cohorts": dict(self.score_cohorts),
            "spread_bands": dict(self.spread_bands),
            "depth_inference_counts": dict(self.depth_inference_counts),
            "shadow_capital_inr": float(getattr(self.cfg, "SOCKET_SHADOW_CAPITAL", 5000.0)),
            "daily_loss_limit_enabled": getattr(self.cfg, "SOCKET_SHADOW_MAX_DAILY_LOSS_PCT", None) is not None,
            "reversal_logic_enabled": False,
        }

    def persist_summary(self):
        path = self.output_dir / f"summary_{self.session_date}.json"
        path.write_text(json.dumps(self.summary(), indent=2), encoding="utf-8")
        return path

    def close(self):
        self.persist_summary()
        self.recorder.close()


def main():
    # Keep broker/websocket dependencies runtime-only so the signal and exit
    # engine can be tested without a configured Kite installation.
    from auth import get_kite_client
    from ws_ticker import WSTicker, build_token_map

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not bool(getattr(cfg, "ENABLE_EQUITY_SOCKET_SHADOW", False)):
        raise SystemExit("Equity socket shadow is disabled")
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: PAPER_TRADING must be True")
    watchlist = list(getattr(cfg, "WATCHLIST", []) or [])
    if len(watchlist) != 30:
        raise SystemExit(f"SAFETY BLOCK: expected frozen top-30 watchlist, found {len(watchlist)}")
    kite = get_kite_client()
    token_map = build_token_map(kite, watchlist)
    if len(token_map) != 30:
        raise SystemExit(f"SAFETY BLOCK: resolved {len(token_map)}/30 watchlist tokens")
    engine = SocketShadowEngine(cfg)
    ticker = WSTicker(cfg.API_KEY, kite.access_token, token_map, on_tick=engine.on_tick)
    stopping = threading.Event()

    def request_stop(signum, frame):
        stopping.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    ticker.start(threaded=True)
    if not ticker.wait_until_connected(timeout=15.0):
        engine._event("STARTUP_BLOCKED", reason="WEBSOCKET_NOT_CONNECTED")
        engine.close()
        ticker.stop()
        raise SystemExit("SAFETY BLOCK: WebSocket failed to connect")
    engine._event("SHADOW_STARTED", watchlist_count=120, token_count=len(token_map))
    LOGGER.warning("EQUITY SOCKET SHADOW ACTIVE: PAPER simulation only; broker orders impossible")
    squared_off = False
    last_disk_check = 0.0
    last_fallback_poll = 0.0
    try:
        while not stopping.wait(1.0):
            current = _now_ist()
            monotonic_now = time.monotonic()
            wall_now = time.time()
            if monotonic_now - last_disk_check >= 60.0:
                last_disk_check = monotonic_now
                minimum_free = float(getattr(cfg, "SOCKET_SHADOW_MIN_FREE_DISK_GB", 2.0)) * 1024 ** 3
                free_bytes = shutil.disk_usage(engine.output_dir).free
                if free_bytes < minimum_free:
                    engine.entry_halted = True
                    engine.entry_halt_reason = "LOW_DISK_SPACE"
                    engine._event("SAFETY_SHUTDOWN", reason="LOW_DISK_SPACE", free_bytes=free_bytes)
                    break
            if current.strftime("%H:%M") >= str(getattr(cfg, "FORCE_SQUARE_OFF_TIME", "15:08")) and not squared_off:
                engine.square_off()
                squared_off = True
                engine.persist_summary()
            if current.strftime("%H:%M") >= "15:10":
                break
            connected = ticker.is_connected()
            if not connected:
                engine.entry_halted = True
                engine.entry_halt_reason = "WEBSOCKET_DISCONNECTED"
                engine.arm_stale_fallbacks(wall_now, "WEBSOCKET_DISCONNECTED")
            elif engine.entry_halt_reason == "WEBSOCKET_DISCONNECTED":
                engine.entry_halted = False
                engine.entry_halt_reason = None
            if connected:
                engine.arm_stale_fallbacks(wall_now)
            poll_seconds = float(getattr(cfg, "SOCKET_SHADOW_STALE_REST_POLL_SECONDS", 2.0))
            if engine.fallback_armed and monotonic_now - last_fallback_poll >= poll_seconds:
                last_fallback_poll = monotonic_now
                instruments = [f"NSE:{symbol}" for symbol in sorted(engine.fallback_armed)]
                try:
                    quotes = kite.quote(instruments)
                    for instrument, quote in quotes.items():
                        engine.on_fallback_quote(instrument.split(":", 1)[-1], quote, wall_now)
                except Exception as exc:
                    engine._event("FALLBACK_REST_QUOTE_ERROR", error=repr(exc),
                                  instruments=instruments)
    finally:
        if not squared_off:
            engine.square_off()
        engine.persist_summary()
        ticker.stop()
        engine.close()
        LOGGER.info("Final socket-shadow summary: %s", engine.summary())


if __name__ == "__main__":
    main()
