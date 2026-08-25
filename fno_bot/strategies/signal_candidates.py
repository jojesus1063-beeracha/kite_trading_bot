"""
Modular, independently shadow-testable CE/PE direction-selection
candidates (spec #6). NONE of these is assumed correct -- the one
observed trade (CE=307.30, PE=196.80, diff=110.50 -> bought PE) is one
data point and proves nothing about which of these, if any, is a real
edge. Every candidate here runs on every session in SHADOW mode; only
cfg.AUTHORIZED_SIGNAL (default: None, meaning "no candidate is
authorized yet") is ever allowed to produce a live/paper order --
see opening_scalper.py.

Each candidate takes a MarketSnapshot and returns a DirectionSignal.
`direction` is "CE", "PE", or None (no opinion / insufficient data).
Never raises for missing/insufficient data -- returns a None-direction
signal with a reason instead, so one candidate's data gap never breaks
the others or the shadow-logging pass.
"""
from dataclasses import dataclass, field
from typing import Optional, Callable


@dataclass(frozen=True)
class TickPoint:
    """One historical (price, monotonic_time) sample, used by candidates
    that need a short recent window rather than just the latest tick."""
    price: float
    at_monotonic: float
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    total_buy_qty: Optional[int] = None
    total_sell_qty: Optional[int] = None


@dataclass(frozen=True)
class MarketSnapshot:
    underlying_price: float
    underlying_prev_close: Optional[float]
    ce_price: float
    pe_price: float
    ce_best_bid: Optional[float]
    ce_best_ask: Optional[float]
    ce_best_bid_qty: Optional[int]
    ce_best_ask_qty: Optional[int]
    pe_best_bid: Optional[float]
    pe_best_ask: Optional[float]
    pe_best_bid_qty: Optional[int]
    pe_best_ask_qty: Optional[int]
    ce_history: tuple = field(default_factory=tuple)   # recent TickPoints, oldest first
    pe_history: tuple = field(default_factory=tuple)
    underlying_history: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class DirectionSignal:
    candidate: str
    direction: Optional[str]   # "CE" | "PE" | None
    confidence: Optional[float]  # 0-100, candidate-defined scale, None if no opinion
    reason: str
    raw_metrics: dict


def premium_imbalance(snapshot: MarketSnapshot) -> DirectionSignal:
    """
    The candidate closest to what the observed trade superficially
    looked like: CE and PE premiums differ, and the HIGHER-premium
    side is read as "more bought up" (more expensive) -- so this
    candidate selects the CHEAPER side, matching the one observed
    example (CE 307.30 > PE 196.80 -> selected PE). This is exactly
    the naive rule spec #6 warns against trusting; it exists here
    ONLY as one shadow-tested candidate among several, never as a
    default live rule.
    """
    diff = abs(snapshot.ce_price - snapshot.pe_price)
    if snapshot.ce_price <= 0 or snapshot.pe_price <= 0:
        return DirectionSignal("premium_imbalance", None, None, "non-positive premium", {})
    diff_pct = diff / min(snapshot.ce_price, snapshot.pe_price) * 100
    direction = "PE" if snapshot.ce_price > snapshot.pe_price else "CE"
    confidence = min(diff_pct, 100.0)
    return DirectionSignal(
        "premium_imbalance", direction, confidence,
        f"selected cheaper leg; ce={snapshot.ce_price:.2f} pe={snapshot.pe_price:.2f} diff_pct={diff_pct:.2f}",
        {"ce_price": snapshot.ce_price, "pe_price": snapshot.pe_price, "diff": diff, "diff_pct": diff_pct},
    )


def premium_rate_of_change(snapshot: MarketSnapshot, min_points: int = 2) -> DirectionSignal:
    """
    Compares how fast CE vs PE premium has been RISING over the recent
    history window, rather than absolute levels -- the side rising
    faster is read as the side with real opening momentum behind it.
    """
    if len(snapshot.ce_history) < min_points or len(snapshot.pe_history) < min_points:
        return DirectionSignal("premium_rate_of_change", None, None,
                                "insufficient tick history for rate-of-change", {})
    ce_roc = snapshot.ce_history[-1].price - snapshot.ce_history[0].price
    pe_roc = snapshot.pe_history[-1].price - snapshot.pe_history[0].price
    if ce_roc == pe_roc:
        return DirectionSignal("premium_rate_of_change", None, 0.0, "no differential momentum",
                                {"ce_roc": ce_roc, "pe_roc": pe_roc})
    direction = "CE" if ce_roc > pe_roc else "PE"
    confidence = min(abs(ce_roc - pe_roc) / max(abs(ce_roc), abs(pe_roc), 0.01) * 100, 100.0)
    return DirectionSignal(
        "premium_rate_of_change", direction, confidence,
        f"faster-rising leg selected; ce_roc={ce_roc:.2f} pe_roc={pe_roc:.2f}",
        {"ce_roc": ce_roc, "pe_roc": pe_roc},
    )


def confirmed_momentum(snapshot: MarketSnapshot, min_points: int = 3) -> DirectionSignal:
    """Direction only when spot, option momentum, and book pressure agree.

    This is deliberately an abstaining entry signal.  Absolute premium level
    is never used to choose CE or PE: a cheap, falling option must not become
    attractive merely because it is cheap.
    """
    if snapshot.underlying_prev_close is None or snapshot.underlying_prev_close <= 0:
        return DirectionSignal("confirmed_momentum", None, None, "previous close unavailable", {})
    if len(snapshot.ce_history) < min_points or len(snapshot.pe_history) < min_points:
        return DirectionSignal("confirmed_momentum", None, None, "collecting option momentum history", {})

    span = min(
        snapshot.ce_history[-1].at_monotonic - snapshot.ce_history[0].at_monotonic,
        snapshot.pe_history[-1].at_monotonic - snapshot.pe_history[0].at_monotonic,
    )
    if span < 2.0:
        return DirectionSignal("confirmed_momentum", None, None, "momentum window shorter than 2 seconds", {"span_seconds": span})

    gap_pct = (
        (snapshot.underlying_price - snapshot.underlying_prev_close)
        / snapshot.underlying_prev_close * 100
    )
    ce_start = snapshot.ce_history[0].price
    pe_start = snapshot.pe_history[0].price
    if ce_start <= 0 or pe_start <= 0:
        return DirectionSignal("confirmed_momentum", None, None, "non-positive history price", {})
    ce_roc_pct = (snapshot.ce_history[-1].price - ce_start) / ce_start * 100
    pe_roc_pct = (snapshot.pe_history[-1].price - pe_start) / pe_start * 100

    def pressure(bid_qty, ask_qty):
        total = (bid_qty or 0) + (ask_qty or 0)
        return None if total <= 0 else ((bid_qty or 0) - (ask_qty or 0)) / total

    ce_pressure = pressure(snapshot.ce_best_bid_qty, snapshot.ce_best_ask_qty)
    pe_pressure = pressure(snapshot.pe_best_bid_qty, snapshot.pe_best_ask_qty)
    metrics = {
        "gap_pct": gap_pct, "ce_roc_pct": ce_roc_pct, "pe_roc_pct": pe_roc_pct,
        "ce_pressure": ce_pressure, "pe_pressure": pe_pressure, "span_seconds": span,
    }
    if ce_pressure is None or pe_pressure is None:
        return DirectionSignal("confirmed_momentum", None, None, "book pressure unavailable", metrics)

    min_gap_pct = 0.10
    min_leg_roc_pct = 0.25
    min_relative_edge_pct = 0.25
    if abs(gap_pct) < min_gap_pct:
        return DirectionSignal("confirmed_momentum", None, 0.0, "underlying direction too weak", metrics)

    direction = "CE" if gap_pct > 0 else "PE"
    selected_roc = ce_roc_pct if direction == "CE" else pe_roc_pct
    opposing_roc = pe_roc_pct if direction == "CE" else ce_roc_pct
    selected_pressure = ce_pressure if direction == "CE" else pe_pressure
    opposing_pressure = pe_pressure if direction == "CE" else ce_pressure

    if selected_roc < min_leg_roc_pct:
        return DirectionSignal("confirmed_momentum", None, 0.0, "directional option is not rising", metrics)
    if selected_roc - opposing_roc < min_relative_edge_pct:
        return DirectionSignal("confirmed_momentum", None, 0.0, "insufficient CE/PE relative-strength edge", metrics)
    if selected_pressure <= opposing_pressure or selected_pressure < 0:
        return DirectionSignal("confirmed_momentum", None, 0.0, "order book does not confirm direction", metrics)

    confidence = min(
        100.0,
        abs(gap_pct) * 100
        + selected_roc * 10
        + (selected_roc - opposing_roc) * 10
        + max(selected_pressure - opposing_pressure, 0) * 25,
    )
    return DirectionSignal(
        "confirmed_momentum", direction, confidence,
        f"spot/{direction} momentum/book agree; gap={gap_pct:+.3f}% "
        f"ce_roc={ce_roc_pct:+.3f}% pe_roc={pe_roc_pct:+.3f}%",
        metrics,
    )


def professional_momentum(snapshot: MarketSnapshot, min_points: int = 30) -> DirectionSignal:
    """Conservative live-tick direction for professional PAPER evaluation.

    Requires a sustained underlying move, matching option relative strength,
    fresh cumulative-volume activity, minimum OI, and total-book pressure.
    It deliberately abstains when Kite full-mode fields are unavailable.
    """
    histories = (snapshot.underlying_history, snapshot.ce_history, snapshot.pe_history)
    if any(len(history) < min_points for history in histories):
        return DirectionSignal("professional_momentum", None, None, "collecting 30-second live history", {})
    span = min(history[-1].at_monotonic - history[0].at_monotonic for history in histories)
    if span < 29.0:
        return DirectionSignal("professional_momentum", None, None, "live observation window shorter than 29 seconds", {"span_seconds": span})

    def roc(history):
        start = history[0].price
        return None if start <= 0 else (history[-1].price - start) / start * 100

    underlying_roc = roc(snapshot.underlying_history)
    ce_roc = roc(snapshot.ce_history)
    pe_roc = roc(snapshot.pe_history)
    if None in (underlying_roc, ce_roc, pe_roc):
        return DirectionSignal("professional_momentum", None, None, "invalid live price history", {})

    direction = "CE" if underlying_roc > 0 else "PE"
    selected = snapshot.ce_history if direction == "CE" else snapshot.pe_history
    opposing = snapshot.pe_history if direction == "CE" else snapshot.ce_history
    selected_roc = ce_roc if direction == "CE" else pe_roc
    opposing_roc = pe_roc if direction == "CE" else ce_roc

    latest = selected[-1]
    opposite_latest = opposing[-1]
    volume_delta = None if latest.volume is None or selected[0].volume is None else latest.volume - selected[0].volume
    opposing_volume_delta = None if opposite_latest.volume is None or opposing[0].volume is None else opposite_latest.volume - opposing[0].volume

    def total_pressure(point):
        total = (point.total_buy_qty or 0) + (point.total_sell_qty or 0)
        return None if total <= 0 else ((point.total_buy_qty or 0) - (point.total_sell_qty or 0)) / total

    pressure = total_pressure(latest)
    opposing_pressure = total_pressure(opposite_latest)
    metrics = {
        "span_seconds": span, "underlying_roc_pct": underlying_roc,
        "ce_roc_pct": ce_roc, "pe_roc_pct": pe_roc,
        "selected_volume_delta": volume_delta,
        "opposing_volume_delta": opposing_volume_delta,
        "selected_oi": latest.open_interest,
        "selected_pressure": pressure, "opposing_pressure": opposing_pressure,
    }

    if abs(underlying_roc) < 0.08:
        return DirectionSignal("professional_momentum", None, 0.0, "underlying 30-second move below 0.08%", metrics)
    if selected_roc < 1.0:
        return DirectionSignal("professional_momentum", None, 0.0, "directional option 30-second momentum below 1%", metrics)
    if selected_roc - opposing_roc < 1.0:
        return DirectionSignal("professional_momentum", None, 0.0, "CE/PE relative-strength edge below 1%", metrics)
    if volume_delta is None or volume_delta <= 0:
        return DirectionSignal("professional_momentum", None, 0.0, "no confirmed live option-volume increase", metrics)
    if latest.open_interest is None or latest.open_interest < 1000:
        return DirectionSignal("professional_momentum", None, 0.0, "open interest below 1000 or unavailable", metrics)
    if pressure is None or opposing_pressure is None or pressure <= opposing_pressure or pressure < 0:
        return DirectionSignal("professional_momentum", None, 0.0, "total buy/sell pressure does not confirm direction", metrics)

    confidence = min(100.0, abs(underlying_roc) * 150 + selected_roc * 12 +
                     (selected_roc - opposing_roc) * 10 + (pressure - opposing_pressure) * 20)
    return DirectionSignal(
        "professional_momentum", direction, confidence,
        f"30-second live spot/{direction}/volume/OI/book confirmation",
        metrics,
    )


def underlying_open_vs_prev_close(snapshot: MarketSnapshot) -> DirectionSignal:
    """
    Simple gap-direction read: underlying opened above previous close
    -> bullish gap -> CE; below -> PE. The crudest, most classically
    "momentum" candidate, included because it's the most-cited naive
    opening strategy and worth having as a baseline comparison point.
    """
    if snapshot.underlying_prev_close is None or snapshot.underlying_prev_close <= 0:
        return DirectionSignal("underlying_open_vs_prev_close", None, None, "previous close unavailable", {})
    gap = snapshot.underlying_price - snapshot.underlying_prev_close
    gap_pct = gap / snapshot.underlying_prev_close * 100
    if gap == 0:
        return DirectionSignal("underlying_open_vs_prev_close", None, 0.0, "flat open, no gap", {"gap_pct": 0.0})
    direction = "CE" if gap > 0 else "PE"
    confidence = min(abs(gap_pct) * 20, 100.0)  # a 5% gap maxes out confidence; tune only after shadow evidence
    return DirectionSignal(
        "underlying_open_vs_prev_close", direction, confidence,
        f"gap={gap:.2f} ({gap_pct:+.3f}%) vs prev close {snapshot.underlying_prev_close:.2f}",
        {"gap": gap, "gap_pct": gap_pct},
    )


def bid_ask_imbalance(snapshot: MarketSnapshot) -> DirectionSignal:
    """
    Compares best-level bid/ask size on CE vs PE: heavier bid-side
    interest on one leg relative to its own ask is read as buying
    pressure on that leg. This only uses best-of-book (level 1) size,
    a real limitation flagged in raw_metrics -- true depth imbalance
    would need multiple levels, which the ticker's normalized tick
    currently only exposes as best bid/ask (see tick_store.py).
    """
    def leg_pressure(bid_qty, ask_qty):
        if not bid_qty and not ask_qty:
            return None
        total = (bid_qty or 0) + (ask_qty or 0)
        if total == 0:
            return None
        return ((bid_qty or 0) - (ask_qty or 0)) / total  # -1..+1, +ve = bid-heavy

    ce_pressure = leg_pressure(snapshot.ce_best_bid_qty, snapshot.ce_best_ask_qty)
    pe_pressure = leg_pressure(snapshot.pe_best_bid_qty, snapshot.pe_best_ask_qty)

    if ce_pressure is None or pe_pressure is None:
        return DirectionSignal("bid_ask_imbalance", None, None, "missing depth on one or both legs",
                                {"note": "level-1 depth only, not full market depth"})

    if ce_pressure == pe_pressure:
        return DirectionSignal("bid_ask_imbalance", None, 0.0, "equal bid/ask pressure on both legs",
                                {"ce_pressure": ce_pressure, "pe_pressure": pe_pressure})

    direction = "CE" if ce_pressure > pe_pressure else "PE"
    confidence = min(abs(ce_pressure - pe_pressure) * 100, 100.0)
    return DirectionSignal(
        "bid_ask_imbalance", direction, confidence,
        f"stronger bid-side pressure; ce_pressure={ce_pressure:+.2f} pe_pressure={pe_pressure:+.2f} "
        f"(level-1 depth only)",
        {"ce_pressure": ce_pressure, "pe_pressure": pe_pressure, "note": "level-1 depth only"},
    )


def depth_imbalance(snapshot: MarketSnapshot) -> DirectionSignal:
    """
    Distinct from bid_ask_imbalance: compares TOTAL visible liquidity
    (bid_qty + ask_qty) on CE vs PE, as a proxy for which leg the
    market is more actively quoting/interested in right now, rather
    than directional pressure within one leg's book.
    """
    ce_total = (snapshot.ce_best_bid_qty or 0) + (snapshot.ce_best_ask_qty or 0)
    pe_total = (snapshot.pe_best_bid_qty or 0) + (snapshot.pe_best_ask_qty or 0)

    if ce_total == 0 and pe_total == 0:
        return DirectionSignal("depth_imbalance", None, None, "no depth data on either leg", {})

    if ce_total == pe_total:
        return DirectionSignal("depth_imbalance", None, 0.0, "equal visible liquidity",
                                {"ce_total": ce_total, "pe_total": pe_total})

    # Interpretation is deliberately left as an open question for shadow
    # analysis: does more liquidity on a leg predict it's the one about
    # to move, or just the one everyone's already positioned in? Direction
    # here follows the MORE-liquid leg; this is a hypothesis to test, not
    # a conclusion.
    direction = "CE" if ce_total > pe_total else "PE"
    confidence = min(abs(ce_total - pe_total) / max(ce_total, pe_total) * 100, 100.0)
    return DirectionSignal(
        "depth_imbalance", direction, confidence,
        f"more-liquid leg selected (hypothesis, unvalidated); ce_total={ce_total} pe_total={pe_total}",
        {"ce_total": ce_total, "pe_total": pe_total},
    )


CANDIDATE_REGISTRY: dict[str, Callable[[MarketSnapshot], DirectionSignal]] = {
    "premium_imbalance": premium_imbalance,
    "premium_rate_of_change": premium_rate_of_change,
    "underlying_open_vs_prev_close": underlying_open_vs_prev_close,
    "bid_ask_imbalance": bid_ask_imbalance,
    "depth_imbalance": depth_imbalance,
    "confirmed_momentum": confirmed_momentum,
    "professional_momentum": professional_momentum,
}


def evaluate_all_candidates(snapshot: MarketSnapshot, candidate_names: list[str] = None) -> list[DirectionSignal]:
    """Runs every requested candidate (default: everything in the
    registry) against one snapshot. A single candidate raising is
    caught and converted into a None-direction signal with the
    exception as its reason -- one bad candidate must never prevent
    the others (or the authorized one) from being evaluated and logged."""
    names = candidate_names if candidate_names is not None else list(CANDIDATE_REGISTRY.keys())
    results = []
    for name in names:
        fn = CANDIDATE_REGISTRY.get(name)
        if fn is None:
            results.append(DirectionSignal(name, None, None, f"unknown candidate {name!r}", {}))
            continue
        try:
            results.append(fn(snapshot))
        except Exception as e:
            results.append(DirectionSignal(name, None, None, f"candidate raised: {e}", {}))
    return results
