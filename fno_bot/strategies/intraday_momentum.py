"""Completed-candle plus live-option confirmation for intraday PAPER trading.

The historical leg deliberately accepts only completed one-minute candles.
The caller is responsible for removing the currently-forming minute before
calling :func:`evaluate_intraday_momentum`.
"""
from dataclasses import dataclass
from typing import Optional, Sequence

from fno_bot.strategies.indicators import (
    adx_wilder,
    atr_wilder,
    di_plus_minus,
    ema,
    session_vwap,
    volume_ratio,
)
from fno_bot.strategies.signal_candidates import MarketSnapshot


@dataclass(frozen=True)
class MinuteCandle:
    timestamp: object
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class IntradayDecision:
    direction: Optional[str]
    confidence: Optional[float]
    reason: str
    metrics: dict


def _option_live_metrics(snapshot: MarketSnapshot, direction: str) -> tuple[dict, Optional[str]]:
    selected = snapshot.ce_history if direction == "CE" else snapshot.pe_history
    opposing = snapshot.pe_history if direction == "CE" else snapshot.ce_history
    if len(selected) < 20 or len(opposing) < 20:
        return {}, "collecting live option history"
    span = min(
        selected[-1].at_monotonic - selected[0].at_monotonic,
        opposing[-1].at_monotonic - opposing[0].at_monotonic,
    )
    if span < 19:
        return {"option_span_seconds": span}, "live option window shorter than 19 seconds"

    def roc(history):
        return None if history[0].price <= 0 else (history[-1].price / history[0].price - 1) * 100

    selected_roc = roc(selected)
    opposing_roc = roc(opposing)
    latest = selected[-1]
    first = selected[0]
    volume_delta = None if latest.volume is None or first.volume is None else latest.volume - first.volume
    total = (latest.total_buy_qty or 0) + (latest.total_sell_qty or 0)
    pressure = None if total <= 0 else ((latest.total_buy_qty or 0) - (latest.total_sell_qty or 0)) / total
    metrics = {
        "option_span_seconds": span,
        "selected_option_roc_pct": selected_roc,
        "opposing_option_roc_pct": opposing_roc,
        "option_relative_edge_pct": None if selected_roc is None or opposing_roc is None else selected_roc - opposing_roc,
        "selected_option_volume_delta": volume_delta,
        "selected_option_oi": latest.open_interest,
        "selected_option_book_pressure": pressure,
    }
    if selected_roc is None or opposing_roc is None:
        return metrics, "invalid option price history"
    if selected_roc < 0.50:
        return metrics, "selected option momentum below 0.50%"
    if selected_roc - opposing_roc < 0.75:
        return metrics, "option relative-strength edge below 0.75%"
    if volume_delta is None or volume_delta <= 0:
        return metrics, "selected option volume is not increasing"
    if latest.open_interest is None or latest.open_interest < 1000:
        return metrics, "selected option OI below 1000 or unavailable"
    if pressure is None or pressure <= 0:
        return metrics, "selected option total-book pressure is not positive"
    return metrics, None


def evaluate_intraday_momentum(
    candles: Sequence[MinuteCandle],
    snapshot: MarketSnapshot,
    *,
    adx_period: int = 14,
    volume_lookback: int = 20,
) -> IntradayDecision:
    """Return CE/PE only when completed candles and live option flow agree."""
    minimum = max(2 * adx_period, 21, volume_lookback + 1)
    if len(candles) < minimum:
        return IntradayDecision(None, None, f"need at least {minimum} completed one-minute candles", {"completed_candles": len(candles)})

    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    closes = [float(c.close) for c in candles]
    volumes = [float(c.volume) for c in candles]
    ema9 = ema(closes, 9)[-1]
    ema21 = ema(closes, 21)[-1]
    vwap = session_vwap(highs, lows, closes, volumes)[-1]
    adx = adx_wilder(highs, lows, closes, adx_period)[-1]
    di_plus, di_minus = di_plus_minus(highs, lows, closes, adx_period)
    atr = atr_wilder(highs, lows, closes, adx_period)[-1]
    rvol = volume_ratio(volumes, volume_lookback)[-1]
    ready = (ema9, ema21, vwap, adx, di_plus[-1], di_minus[-1], atr, rvol)
    if any(value is None for value in ready):
        return IntradayDecision(None, None, "historical indicators are still warming up", {"completed_candles": len(candles)})

    close = closes[-1]
    bullish = close > vwap and ema9 > ema21 and di_plus[-1] > di_minus[-1]
    bearish = close < vwap and ema9 < ema21 and di_minus[-1] > di_plus[-1]
    direction = "CE" if bullish else "PE" if bearish else None
    metrics = {
        "close": close, "ema9": ema9, "ema21": ema21, "vwap": vwap,
        "adx": adx, "di_plus": di_plus[-1], "di_minus": di_minus[-1],
        "atr": atr, "relative_volume": rvol,
        "distance_from_vwap_atr": abs(close - vwap) / atr if atr > 0 else None,
        "completed_candles": len(candles),
    }
    if direction is None:
        return IntradayDecision(None, 0.0, "EMA/VWAP/DI direction is not aligned", metrics)
    if adx < 20:
        return IntradayDecision(None, 0.0, "ADX below 20; market is not trending", metrics)
    if rvol < 0.80:
        return IntradayDecision(None, 0.0, "relative volume below 0.80", metrics)
    if atr <= 0 or abs(close - vwap) / atr > 1.50:
        return IntradayDecision(None, 0.0, "price is extended more than 1.50 ATR from VWAP", metrics)

    live_metrics, rejection = _option_live_metrics(snapshot, direction)
    metrics.update(live_metrics)
    if rejection:
        return IntradayDecision(None, 0.0, rejection, metrics)

    trend_separation = abs(ema9 - ema21) / close * 100
    confidence = min(
        100.0,
        adx * 0.8 + min(rvol, 3.0) * 10 + trend_separation * 80
        + max(live_metrics["selected_option_roc_pct"], 0) * 8
        + max(live_metrics["option_relative_edge_pct"], 0) * 6,
    )
    return IntradayDecision(direction, confidence, "completed-candle trend and live option flow agree", metrics)

