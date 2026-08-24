"""
PREMIUM_ROTATION_SHADOW -- entry eligibility gate (section 9 threshold
logic + section 10 anti-chase, combined into one decision point).

CRITICAL DESIGN CONSTRAINT, learned the hard way on 2026-08-21: the
opening-scalper's run_entry() had two early-return paths that produced
NO audit event, making a real rejection look like total silence. This
module's evaluate_entry() NEVER returns without a populated `reason`
and `rejections` list -- there is no code path that can return
eligible=False with an empty explanation. If this function is ever
extended, that invariant must be preserved.

SHADOW-only: no broker, no order function, no import of anything in
fno_bot.broker or fno_bot.execution. This is a decision, not an action.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from fno_bot.strategies.premium_rotation import (
    TickSample, WindowFeatures, RotationParams, ConfirmationTracker,
    resolve_classification, compute_scores, check_not_extended,
    BULLISH_ROTATION, BEARISH_ROTATION,
)


@dataclass(frozen=True)
class EntryParams:
    """All experimental (section 9 explicitly forbids assuming these
    are optimal production values)."""
    score_threshold: float = 65.0        # winning score must be >= this
    dominance_margin: float = 20.0       # winning score - opposing score must be >= this
    anti_chase_lookback_seconds: float = 10.0
    anti_chase_max_extension_pct: float = 15.0


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    direction: Optional[str]   # "CE" | "PE" | None
    ce_score: float
    pe_score: float
    classification: str
    confirmed: bool
    reason: str                 # always populated, success or failure
    rejections: List[str] = field(default_factory=list)   # every reason checked and failed, not just the first


def evaluate_entry(
    history: List[TickSample],
    now_time: float,
    window_seconds: float,
    tracker: ConfirmationTracker,
    params_rotation: RotationParams,
    params_entry: EntryParams,
    kill_switch_allowed: bool = True,
    kill_switch_reason: str = "",
    opening_protected: bool = False,
) -> EligibilityResult:
    """Single entry point tying detection + scoring + confirmation +
    anti-chase together. Called once per synchronized tick; caller is
    responsible for logging the FULL EligibilityResult every time,
    eligible or not (section 16's mandatory logging requirement).

    kill_switch_allowed / opening_protected: THREADED IN, not checked
    after the fact -- this closes the exact gap flagged in
    premium_rotation_launcher.py's first draft, where those two checks
    ran alongside session.on_tick() instead of inside it, meaning a
    trade could open despite them. Now a blocked trade structurally
    cannot become eligible=True. Defaults preserve backward
    compatibility for callers/tests that don't pass these."""
    from fno_bot.strategies.premium_rotation import calculate_window_features

    gate_rejections: List[str] = []
    if opening_protected:
        gate_rejections.append("blocked: within opening-market protection window")
    if not kill_switch_allowed:
        gate_rejections.append(f"blocked by kill switch: {kill_switch_reason}")

    rejections: List[str] = list(gate_rejections)

    features = calculate_window_features(history, window_seconds)
    if features is None:
        return EligibilityResult(
            eligible=False, direction=None, ce_score=0.0, pe_score=0.0,
            classification="INSUFFICIENT_HISTORY", confirmed=False,
            reason="insufficient tick history for the configured window",
            rejections=["insufficient tick history for the configured window"],
        )

    classification = resolve_classification(features, params_rotation)
    confirmed = tracker.update(classification, now_time)
    scores = compute_scores(features, params_rotation)
    ce_score, pe_score = scores["ce_score"], scores["pe_score"]

    if classification not in (BULLISH_ROTATION, BEARISH_ROTATION):
        reason = f"classification is {classification}, not a directional rotation"
        rejections.append(reason)
        return EligibilityResult(False, None, ce_score, pe_score, classification, confirmed, reason, rejections)

    direction = "CE" if classification == BULLISH_ROTATION else "PE"
    winning_score = ce_score if direction == "CE" else pe_score
    opposing_score = pe_score if direction == "CE" else ce_score

    if not confirmed:
        rejections.append(f"classification {classification} not yet confirmed (persistence requirement not met)")

    if winning_score < params_entry.score_threshold:
        rejections.append(f"{direction}_SCORE {winning_score:.1f} below threshold {params_entry.score_threshold}")

    margin = winning_score - opposing_score
    if margin < params_entry.dominance_margin:
        rejections.append(f"dominance margin {margin:.1f} below required {params_entry.dominance_margin}")

    chase_reason = check_not_extended(
        history, direction, params_entry.anti_chase_lookback_seconds, params_entry.anti_chase_max_extension_pct
    )
    if chase_reason is not None:
        rejections.append(chase_reason)

    if rejections:
        return EligibilityResult(False, direction, ce_score, pe_score, classification, confirmed,
                                  "; ".join(rejections), rejections)

    if gate_rejections:
        # belt-and-suspenders: should be unreachable given the check above
        # (gate_rejections is always a subset of rejections), but kept
        # explicit so a future refactor can't accidentally reorder past it.
        return EligibilityResult(False, direction, ce_score, pe_score, classification, confirmed,
                                  "; ".join(gate_rejections), gate_rejections)

    return EligibilityResult(
        eligible=True, direction=direction, ce_score=ce_score, pe_score=pe_score,
        classification=classification, confirmed=confirmed,
        reason=f"{direction} eligible: score={winning_score:.1f} margin={margin:.1f} confirmed={confirmed}",
        rejections=[],
    )
