"""
Modular Price Action confirmation layer. ISOLATED from indicators.py
and strategy.py entirely -- an additional confirmation filter, never
a signal generator, never a replacement for the existing strategy.

No-look-ahead discipline: every function here only uses data at or
before the LAST row of whatever DataFrame slice it's given. Swing
points are identified using a confirmation lag (a swing high/low is
only "confirmed" once enough later bars exist to know it was actually
a local extreme) -- this is standard, unavoidable in real-time swing
detection, and is documented on every relevant function.
"""
import logging
import pandas as pd

from indicators import atr as _atr

logger = logging.getLogger("price_action")


def find_swing_points(df, lookback=3):
    """
    Identifies confirmed swing highs/lows: a bar at index i is a swing
    high if its high is the max within [i-lookback, i+lookback], and
    similarly for swing lows. Because this needs `lookback` bars AFTER
    a candidate point to confirm it, the last `lookback` rows of `df`
    can never produce a confirmed swing point yet -- that's correct,
    not a bug: a swing point isn't knowable as "the top" until price
    has moved away from it.

    Returns (swing_highs, swing_lows) as lists of (index, price) tuples,
    in chronological order, using only data already in `df`.
    """
    swing_highs, swing_lows = [], []
    n = len(df)
    if n < (2 * lookback + 1):
        return swing_highs, swing_lows

    highs = df["high"].values
    lows = df["low"].values

    for i in range(lookback, n - lookback):
        window_high = highs[i - lookback:i + lookback + 1]
        if highs[i] == window_high.max() and (window_high == highs[i]).sum() == 1:
            swing_highs.append((i, highs[i]))
        window_low = lows[i - lookback:i + lookback + 1]
        if lows[i] == window_low.min() and (window_low == lows[i]).sum() == 1:
            swing_lows.append((i, lows[i]))

    return swing_highs, swing_lows


def detect_market_structure(df, direction, lookback=3):
    """
    BUY: requires the two most recent CONFIRMED swing highs to be
    rising (Higher High) AND the two most recent confirmed swing lows
    to be rising (Higher Low).
    SELL: mirror -- Lower High and Lower Low.

    Returns (bool_confirms_structure, detail_dict). Fails safe to
    (False, {"reason": "insufficient data"}) if not enough confirmed
    swing points exist yet -- never raises, never blocks on missing
    history by crashing.
    """
    try:
        swing_highs, swing_lows = find_swing_points(df, lookback)
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return False, {"reason": "insufficient confirmed swing points", "swing_highs": len(swing_highs), "swing_lows": len(swing_lows)}

        last_two_highs = [p for _, p in swing_highs[-2:]]
        last_two_lows = [p for _, p in swing_lows[-2:]]

        higher_high = last_two_highs[1] > last_two_highs[0]
        higher_low = last_two_lows[1] > last_two_lows[0]
        lower_high = last_two_highs[1] < last_two_highs[0]
        lower_low = last_two_lows[1] < last_two_lows[0]

        detail = {
            "last_two_highs": last_two_highs, "last_two_lows": last_two_lows,
            "higher_high": higher_high, "higher_low": higher_low,
            "lower_high": lower_high, "lower_low": lower_low,
        }

        if direction == "BUY":
            return (higher_high and higher_low), detail
        else:
            return (lower_high and lower_low), detail
    except Exception as e:
        logger.warning(f"Market structure detection failed: {e}")
        return False, {"reason": f"error: {e}"}


def find_support_resistance(df, lookback=3):
    """
    Finds the nearest confirmed support (swing low below current
    close) and nearest confirmed resistance (swing high above current
    close), by price distance, not recency. Returns (support, resistance)
    -- either can be None if no confirmed swing exists on that side.
    """
    swing_highs, swing_lows = find_swing_points(df, lookback)
    current_price = df["close"].iloc[-1]

    resistances_above = [p for _, p in swing_highs if p > current_price]
    supports_below = [p for _, p in swing_lows if p < current_price]

    resistance = min(resistances_above) if resistances_above else None
    support = max(supports_below) if supports_below else None
    return support, resistance


def evaluate_support_resistance(df, direction, min_distance_pct=0.5, lookback=3):
    """
    BUY: blocked if too close to resistance (would be buying directly
    into a level likely to reject price).
    SELL: blocked if too close to support (mirror).

    Returns (blocked: bool, detail: dict) with support/resistance
    levels and both distances, regardless of which one is relevant --
    useful for full logging per the spec's requirements.
    """
    try:
        current_price = df["close"].iloc[-1]
        support, resistance = find_support_resistance(df, lookback)

        support_distance_pct = ((current_price - support) / current_price * 100) if support else None
        resistance_distance_pct = ((resistance - current_price) / current_price * 100) if resistance else None

        detail = {
            "support": support, "resistance": resistance,
            "support_distance_pct": support_distance_pct,
            "resistance_distance_pct": resistance_distance_pct,
        }

        if direction == "BUY" and resistance_distance_pct is not None and resistance_distance_pct < min_distance_pct:
            return True, detail
        if direction == "SELL" and support_distance_pct is not None and support_distance_pct < min_distance_pct:
            return True, detail
        return False, detail
    except Exception as e:
        logger.warning(f"Support/resistance evaluation failed: {e}")
        return False, {"reason": f"error: {e}"}


def detect_breakout(df, direction, level, volume_multiplier=1.5, volume_period=20):
    """
    BUY: current candle closes ABOVE `level` (typically resistance)
    with volume confirmation. SELL: closes BELOW `level` (support).
    Requires both the close beyond the level AND above-average volume
    to avoid false/low-conviction breakouts.
    """
    try:
        if level is None:
            return False, {"reason": "no level to break"}
        if len(df) < 2:
            return False, {"reason": "insufficient data"}

        current = df.iloc[-1]
        history = df.iloc[:-1]
        avg_volume = history["volume"].tail(volume_period).mean() if len(history) > 0 else current["volume"]
        volume_ok = bool(current["volume"] > avg_volume * volume_multiplier) if avg_volume > 0 else False

        if direction == "BUY":
            level_broken = bool(current["close"] > level)
        else:
            level_broken = bool(current["close"] < level)

        detail = {
            "level": level, "close": current["close"], "volume_ok": volume_ok,
            "avg_volume": avg_volume, "current_volume": current["volume"], "level_broken": level_broken,
        }
        return (level_broken and volume_ok), detail
    except Exception as e:
        logger.warning(f"Breakout detection failed: {e}")
        return False, {"reason": f"error: {e}"}


def _candle_anatomy(row):
    """body, upper_wick, lower_wick, range_size for one candle."""
    body = abs(row["close"] - row["open"])
    upper_wick = row["high"] - max(row["close"], row["open"])
    lower_wick = min(row["close"], row["open"]) - row["low"]
    range_size = row["high"] - row["low"]
    return body, upper_wick, lower_wick, range_size


def is_hammer(row, wick_to_body_ratio=2.0):
    """Bullish rejection: small body near the top, long lower wick, small upper wick."""
    body, upper_wick, lower_wick, range_size = _candle_anatomy(row)
    if range_size <= 0:
        return False
    long_lower = lower_wick >= wick_to_body_ratio * body and lower_wick >= 0.4 * range_size
    small_upper = upper_wick <= 0.3 * range_size
    return bool(long_lower and small_upper)


def is_shooting_star(row, wick_to_body_ratio=2.0):
    """Bearish rejection: small body near the bottom, long upper wick, small lower wick."""
    body, upper_wick, lower_wick, range_size = _candle_anatomy(row)
    if range_size <= 0:
        return False
    long_upper = upper_wick >= wick_to_body_ratio * body and upper_wick >= 0.4 * range_size
    small_lower = lower_wick <= 0.3 * range_size
    return bool(long_upper and small_lower)


def is_pin_bar(row, wick_to_body_ratio=2.0):
    """Generic pin bar: either a bullish (hammer-shape) or bearish (shooting-star-shape)
    long-wick rejection. Returns (bool, direction) -- direction is 'bullish', 'bearish', or None."""
    if is_hammer(row, wick_to_body_ratio):
        return True, "bullish"
    if is_shooting_star(row, wick_to_body_ratio):
        return True, "bearish"
    return False, None


def is_long_wick_rejection(row, min_wick_ratio=1.5):
    """Looser check than pin bar -- either wick meaningfully longer than the body,
    without requiring the opposite wick to be small. Returns (bool, direction)."""
    body, upper_wick, lower_wick, range_size = _candle_anatomy(row)
    if range_size <= 0 or body <= 0:
        return False, None
    if lower_wick >= min_wick_ratio * body and lower_wick > upper_wick:
        return True, "bullish"
    if upper_wick >= min_wick_ratio * body and upper_wick > lower_wick:
        return True, "bearish"
    return False, None


def detect_rejection_candle(df, direction, wick_to_body_ratio=2.0):
    """
    Checks the LAST row of `df` for a rejection candle supporting
    `direction`: BUY accepts hammer / bullish pin bar / bullish
    long-wick; SELL accepts shooting star / bearish pin bar / bearish
    long-wick. Returns (bool, detail) with which specific pattern(s) matched.
    """
    try:
        row = df.iloc[-1]
        hammer = is_hammer(row, wick_to_body_ratio)
        shooting_star = is_shooting_star(row, wick_to_body_ratio)
        pin_bar, pin_direction = is_pin_bar(row, wick_to_body_ratio)
        long_wick, long_wick_direction = is_long_wick_rejection(row)

        wanted = "bullish" if direction == "BUY" else "bearish"
        matched_patterns = []
        if direction == "BUY" and hammer:
            matched_patterns.append("hammer")
        if direction == "SELL" and shooting_star:
            matched_patterns.append("shooting_star")
        if pin_bar and pin_direction == wanted:
            matched_patterns.append("pin_bar")
        if long_wick and long_wick_direction == wanted:
            matched_patterns.append("long_wick_rejection")

        detail = {"patterns": matched_patterns, "hammer": hammer, "shooting_star": shooting_star,
                   "pin_bar": pin_bar, "pin_bar_direction": pin_direction}
        return (len(matched_patterns) > 0), detail
    except Exception as e:
        logger.warning(f"Rejection candle detection failed: {e}")
        return False, {"reason": f"error: {e}"}


def detect_range(df, atr_period=14, atr_threshold_pct=1.0, flatness_lookback=10, flatness_threshold_pct=0.5):
    """
    Identifies a sideways/range-bound market using: (a) ATR relative
    to price below atr_threshold_pct (quiet, low-volatility), AND
    (b) flat recent highs OR flat recent lows (price bouncing within
    a tight band rather than trending). Both conditions required --
    a quiet ATR alone can happen briefly even in a real trend.
    """
    try:
        min_needed = max(atr_period, flatness_lookback) + 1
        if len(df) < min_needed:
            return False, {"reason": "insufficient data"}

        atr_series = _atr(df, atr_period)
        current_atr = atr_series.iloc[-1]
        current_price = df["close"].iloc[-1]
        if pd.isna(current_atr) or current_price <= 0:
            return False, {"reason": "ATR not available"}

        atr_pct = current_atr / current_price * 100
        recent = df.tail(flatness_lookback)
        high_range_pct = (recent["high"].max() - recent["high"].min()) / current_price * 100
        low_range_pct = (recent["low"].max() - recent["low"].min()) / current_price * 100

        is_range = bool(atr_pct < atr_threshold_pct and
                        (high_range_pct < flatness_threshold_pct or low_range_pct < flatness_threshold_pct))
        detail = {"atr_pct": atr_pct, "high_range_pct": high_range_pct, "low_range_pct": low_range_pct}
        return is_range, detail
    except Exception as e:
        logger.warning(f"Range detection failed: {e}")
        return False, {"reason": f"error: {e}"}

def detect_bos(df, direction, lookback=3):
    """
    Break of Structure: BUY confirms if current close breaks ABOVE the
    most recent confirmed swing high (price making real-time progress
    beyond the last known structural level). SELL confirms if current
    close breaks BELOW the most recent confirmed swing low.
    """
    try:
        swing_highs, swing_lows = find_swing_points(df, lookback)
        current_price = df["close"].iloc[-1]

        if direction == "BUY":
            if not swing_highs:
                return False, {"reason": "no confirmed swing high yet"}
            last_swing_high = swing_highs[-1][1]
            broke = bool(current_price > last_swing_high)
            return broke, {"last_swing_high": last_swing_high, "current_price": current_price}
        else:
            if not swing_lows:
                return False, {"reason": "no confirmed swing low yet"}
            last_swing_low = swing_lows[-1][1]
            broke = bool(current_price < last_swing_low)
            return broke, {"last_swing_low": last_swing_low, "current_price": current_price}
    except Exception as e:
        logger.warning(f"BOS detection failed: {e}")
        return False, {"reason": f"error: {e}"}


def detect_choch(df, direction, lookback=3):
    """
    Change of Character: warns that structure may be reversing AGAINST
    the given direction's continuation. For a BUY (uptrend) context,
    warns if the two most recent confirmed swings show a Lower High +
    Lower Low forming (bearish character emerging). For SELL, mirror:
    warns if Higher High + Higher Low is forming.

    Deliberate simplification: this checks the same two-swing
    comparison as detect_market_structure(), just for the OPPOSITE
    direction -- a full multi-leg CHoCH analysis (tracking an entire
    prior trend's structure before flagging the exact break) is
    considerably more involved; this two-swing version is a reasonable,
    defensible first approximation, used only to REDUCE confidence,
    never to reject a trade outright.
    """
    try:
        swing_highs, swing_lows = find_swing_points(df, lookback)
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return False, {"reason": "insufficient confirmed swings"}

        last_two_highs = [p for _, p in swing_highs[-2:]]
        last_two_lows = [p for _, p in swing_lows[-2:]]

        if direction == "BUY":
            choch = bool(last_two_highs[1] < last_two_highs[0] and last_two_lows[1] < last_two_lows[0])
        else:
            choch = bool(last_two_highs[1] > last_two_highs[0] and last_two_lows[1] > last_two_lows[0])

        detail = {"last_two_highs": last_two_highs, "last_two_lows": last_two_lows, "choch_detected": choch}
        return choch, detail
    except Exception as e:
        logger.warning(f"CHoCH detection failed: {e}")
        return False, {"reason": f"error: {e}"}


def detect_pullback_entry(df, direction, lookback=10, min_pullback_pct=0.2, max_pullback_pct=2.0):
    """
    Multi-stage check, real-time only (no look-ahead): within the last
    `lookback` bars, price must have (1) broken a prior confirmed
    swing level, (2) pulled back toward it without fully reversing
    through it, and (3) the LAST candle must show a rejection pattern
    plus close in the continuation direction.
    """
    try:
        if len(df) < lookback + 5:
            return False, {"reason": "insufficient data"}

        pre_window = df.iloc[:-lookback]
        swing_highs, swing_lows = find_swing_points(pre_window, lookback=3)
        recent = df.tail(lookback)
        current = df.iloc[-1]

        if direction == "BUY":
            if not swing_highs:
                return False, {"reason": "no prior swing high to break"}
            broken_level = swing_highs[-1][1]
            broke_out = bool((recent["high"] > broken_level).any())
            if not broke_out:
                return False, {"reason": "no recent breakout above prior structure", "broken_level": broken_level}
            lowest_since = recent["low"].min()
            pullback_pct = (recent["high"].max() - lowest_since) / recent["high"].max() * 100
            pulled_back = min_pullback_pct <= pullback_pct <= max_pullback_pct
            held_level = bool(lowest_since > broken_level * 0.995)
            rejection_ok, _ = detect_rejection_candle(df, "BUY")
            continuation = bool(current["close"] > current["open"])
        else:
            if not swing_lows:
                return False, {"reason": "no prior swing low to break"}
            broken_level = swing_lows[-1][1]
            broke_out = bool((recent["low"] < broken_level).any())
            if not broke_out:
                return False, {"reason": "no recent breakout below prior structure", "broken_level": broken_level}
            highest_since = recent["high"].max()
            pullback_pct = (highest_since - recent["low"].min()) / highest_since * 100
            pulled_back = min_pullback_pct <= pullback_pct <= max_pullback_pct
            held_level = bool(highest_since < broken_level * 1.005)
            rejection_ok, _ = detect_rejection_candle(df, "SELL")
            continuation = bool(current["close"] < current["open"])

        confirmed = bool(broke_out and pulled_back and held_level and rejection_ok and continuation)
        detail = {"broken_level": broken_level, "pullback_pct": pullback_pct, "held_level": held_level,
                   "rejection_confirmed": rejection_ok, "continuation": continuation, "broke_out": broke_out}
        return confirmed, detail
    except Exception as e:
        logger.warning(f"Pullback entry detection failed: {e}")
        return False, {"reason": f"error: {e}"}


def get_price_action_score(df, direction, cfg):
    """
    Runs every enabled sub-feature (per USE_* config flags) and sums
    their point contributions -- a PURE confidence modifier, never an
    independent hard reject on its own (per "Price Action should
    increase trade quality, not increase trade frequency"). Points:
    Market Structure +15, Breakout +10, Pullback +10, Rejection
    Candle +5, BOS +10, Range Market -20, Resistance/Support Nearby
    -15, CHoCH -25.
    """
    score = 0
    detail = {}

    if getattr(cfg, "USE_MARKET_STRUCTURE", True):
        ms_confirmed, ms_detail = detect_market_structure(df, direction)
        if ms_confirmed:
            score += 15
        detail["market_structure"] = ms_confirmed

    support, resistance = None, None
    if getattr(cfg, "USE_SUPPORT_RESISTANCE", True):
        min_dist = getattr(cfg, "MIN_DISTANCE_TO_SR_PERCENT", 0.5)
        sr_lookback = getattr(cfg, "SUPPORT_RESISTANCE_LOOKBACK", 30)
        sr_window = df.tail(sr_lookback) if len(df) > sr_lookback else df
        blocked_sr, sr_detail = evaluate_support_resistance(sr_window, direction, min_dist, lookback=3)
        support, resistance = sr_detail.get("support"), sr_detail.get("resistance")
        if blocked_sr:
            score -= 15
        detail["support"] = support
        detail["resistance"] = resistance
        detail["sr_blocked"] = blocked_sr

    if getattr(cfg, "USE_BREAKOUT_CONFIRMATION", True):
        level = resistance if direction == "BUY" else support
        breakout_confirmed, _ = detect_breakout(df, direction, level)
        if breakout_confirmed:
            score += 10
        detail["breakout"] = breakout_confirmed

    if getattr(cfg, "USE_PULLBACK_ENTRY", True):
        pullback_confirmed, _ = detect_pullback_entry(df, direction)
        if pullback_confirmed:
            score += 10
        detail["pullback"] = pullback_confirmed

    if getattr(cfg, "USE_REJECTION_CANDLES", True):
        rejection_confirmed, _ = detect_rejection_candle(df, direction)
        if rejection_confirmed:
            score += 5
        detail["rejection_candle"] = rejection_confirmed

    if getattr(cfg, "USE_BOS", True):
        bos_confirmed, _ = detect_bos(df, direction)
        if bos_confirmed:
            score += 10
        detail["bos"] = bos_confirmed

    if getattr(cfg, "USE_RANGE_FILTER", True):
        range_detected, _ = detect_range(df)
        if range_detected:
            score -= 20
        detail["range_market"] = range_detected

    if getattr(cfg, "USE_CHOCH", True):
        choch_detected, _ = detect_choch(df, direction)
        if choch_detected:
            score -= 25
        detail["choch"] = choch_detected

    detail["total_price_action_score"] = score
    return score, detail


def evaluate_price_action(df, direction, cfg):
    """
    Top-level, fail-safe entry point -- returns (score, detail).
    Score defaults to 0 (no impact) if disabled or on any internal
    error; never raises, never blocks the existing strategy.
    """
    if not getattr(cfg, "ENABLE_PRICE_ACTION", False):
        return 0, {"enabled": False}
    try:
        return get_price_action_score(df, direction, cfg)
    except Exception as e:
        logger.warning(f"Price action evaluation failed entirely, using score=0: {e}")
        return 0, {"error": str(e)}
