"""
Unified candle provider.

Single entry point for augmenting an already-fetched REST candle
DataFrame with a fresher WebSocket-built candle, when confidently
correct to do so. run_full_scan() fetches candles exactly as it
always has -- via LIVE_CANDLE_CACHE.get() on the candle-aligned-polling
path, or via data_feed.fetch_candles() directly otherwise -- and this
module is called ONCE, afterward, on the result. Neither existing
REST-fetching path is touched or wrapped; this module only ever adds
to what they already return.

The returned DataFrame always has the same shape
(date/open/high/low/close/volume) that both existing REST paths have
always returned -- callers downstream (indicators.py, strategy.py)
are completely unaffected by this module's existence, and were not
modified to accommodate it.

Decision logic:
- IF cfg.ENABLE_WS_CANDLES and cfg.WS_CANDLE_MODE == "live" AND a
  running WSShadowEngine was supplied: ask it whether it has a
  confidently-correct next candle to append (see
  ws_integration.get_augmented_candles() for the exact fail-safe
  conditions -- gap detection, staleness check, timezone
  normalization, wrapped in a broad except that guarantees this can
  never raise into the caller).
- Any doubt, absence, mismatch, or error -- the REST df passed in is
  returned completely unmodified. This function can never return LESS
  data than was passed in; it can only ever ADD the single freshest
  candle when confident.

This is the ONLY place in the codebase that needs to know both data
sources exist. strategy.py, indicators.py, executor.py, and both
existing candle-fetching code paths in main.py are unchanged and
unaware of any of this.
"""

import logging

logger = logging.getLogger("candle_provider")


def augment_with_ws(df, *, symbol, interval, ws_engine=None):
    """
    Given an already-fetched REST DataFrame `df`, returns it augmented
    with the freshest WS-built candle if confidently correct to do so,
    else returns `df` completely unmodified. Never raises.
    """
    if ws_engine is None or symbol is None or df is None:
        return df

    try:
        import config as cfg
    except ImportError:
        return df

    if not getattr(cfg, "ENABLE_WS_CANDLES", False):
        return df
    if getattr(cfg, "WS_CANDLE_MODE", "shadow") != "live":
        return df

    timeframe_label = _interval_to_timeframe_label(interval)
    if timeframe_label is None:
        return df

    try:
        augmented_df, _was_augmented = ws_engine.get_augmented_candles(symbol, timeframe_label, df)
        return augmented_df
    except Exception:
        logger.exception(f"candle_provider: WS augmentation attempt failed for {symbol}/{interval} -- "
                          f"falling back to the unmodified REST result for this call")
        return df


def _interval_to_timeframe_label(interval: str):
    """Maps Kite's interval strings to the timeframe labels
    ws_integration.WSShadowEngine's internal dicts are keyed by."""
    if interval == "5minute":
        return "5minute"
    if interval == "15minute":
        return "15minute"
    return None

