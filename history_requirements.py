"""Pure helpers for deriving candle-history requirements from config."""

import math


FIFTEEN_MINUTE_BARS_PER_NSE_SESSION = 25
DEFAULT_TREND_LOOKBACK_DAYS = 5
DEFAULT_EMA200_HISTORY_LOOKBACK_DAYS = 20
DEFAULT_ENTRY_HISTORY_LOOKBACK_DAYS = 5
NSE_SESSION_MINUTES = 375


def _interval_minutes(timeframe: str) -> int:
    value = str(timeframe)
    if not value.endswith("minute"):
        raise ValueError(f"unsupported minute timeframe: {timeframe}")
    minutes = int(value.removesuffix("minute"))
    if minutes <= 0:
        raise ValueError("timeframe must be positive")
    return minutes


def entry_indicator_lookback_days(cfg) -> int:
    """Calendar days sufficient for all configured entry indicators."""

    interval = _interval_minutes(getattr(cfg, "ENTRY_TIMEFRAME", "3minute"))
    bars_per_session = max(1, NSE_SESSION_MINUTES // interval)
    required_bars = max(
        int(getattr(cfg, "ENTRY_EMA", 20)),
        int(getattr(cfg, "VOLUME_LOOKBACK", 20)) + 1,
        int(getattr(cfg, "RVOL_LOOKBACK", 20)) + 1,
        15,  # ATR(14) plus one prior close
    )
    trading_days = math.ceil(required_bars / bars_per_session)
    derived_calendar_days = math.ceil(trading_days * 7 / 5) + 2
    configured_floor = int(
        getattr(
            cfg,
            "ENTRY_HISTORY_LOOKBACK_DAYS",
            DEFAULT_ENTRY_HISTORY_LOOKBACK_DAYS,
        )
    )
    return max(DEFAULT_ENTRY_HISTORY_LOOKBACK_DAYS, configured_floor, derived_calendar_days)


def entry_trend_lookback_days(cfg) -> int:
    """Return calendar days needed by the enabled EMA200 entry gates.

    ``fetch_candles(..., lookback_days=...)`` accepts calendar days, while
    EMA configuration is expressed in candle counts. The buffer covers a
    weekend and one additional non-trading day.
    """
    if not (
        getattr(cfg, "ENABLE_200_EMA_FILTER", False)
        or getattr(cfg, "ENABLE_EMA200_WATCHLIST", False)
    ):
        return DEFAULT_TREND_LOOKBACK_DAYS

    period = max(1, int(getattr(cfg, "EMA200_PERIOD", 200)))
    slope = max(0, int(getattr(cfg, "EMA200_SLOPE_LOOKBACK", 5)))
    ema_lookback = max(1, int(getattr(cfg, "EMA200_LOOKBACK", 250)))
    required_bars = max(period + slope, ema_lookback)

    trading_days = math.ceil(
        required_bars / FIFTEEN_MINUTE_BARS_PER_NSE_SESSION
    )
    derived_calendar_days = math.ceil(trading_days * 7 / 5) + 3
    configured_floor = max(
        DEFAULT_TREND_LOOKBACK_DAYS,
        int(
            getattr(
                cfg,
                "EMA200_HISTORY_LOOKBACK_DAYS",
                DEFAULT_EMA200_HISTORY_LOOKBACK_DAYS,
            )
        ),
    )
    return max(derived_calendar_days, configured_floor)
