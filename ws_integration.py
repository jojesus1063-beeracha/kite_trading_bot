"""
Wires ws_ticker + candle_engine + indicators_incremental into main.py.

SHADOW MODE (cfg.WS_CANDLE_MODE == "shadow", the default whenever
cfg.ENABLE_WS_CANDLES is True): this module NEVER touches
run_full_scan(), evaluate(), or executor.place_entry_order(). It runs
entirely independently, driven by WSTicker's own background thread via
an on_tick callback. Nothing here can change what the existing
REST-polling loop does, even if this module crashes outright (every
entry point is wrapped so a failure here only logs and disables the
shadow engine, never raises into main.py's while-loop).

LIVE MODE (cfg.WS_CANDLE_MODE == "live"): main.py calls
get_augmented_candles() to optionally append the freshest WS-built
candle onto an already REST-fetched DataFrame, BEFORE indicators are
computed and a signal is evaluated. This is the one place this module
is allowed to influence trading decisions. It is designed to fail
closed: any timestamp mismatch, staleness, timezone issue, or
exception inside get_augmented_candles() falls back to the REST
DataFrame completely unmodified -- augmentation only ever ADDS the one
freshest candle when it's confidently correct to do so, never replaces
or reorders existing REST data, never guesses across a gap.

Called from main.py's run(), before the while-loop, only when
cfg.ENABLE_WS_CANDLES is True. When False (the default), main.py never
imports this module at all -- see the integration snippet in
HOW_TO_APPLY.md.
"""

import logging
import time
from datetime import datetime
from typing import Optional

import config as cfg
from candle_engine import SymbolCandleBuilder, combine_5m_into_15m, ShadowComparator
from indicators_incremental import SymbolIndicatorState, IncrementalShadowComparator
from data_feed import fetch_candles

logger = logging.getLogger("ws_integration")


class WSShadowEngine:
    """
    Owns one SymbolCandleBuilder (5-min) + one SymbolIndicatorState per
    timeframe per symbol, fed by WSTicker's on_tick callback. Never
    exposes anything that main.py's existing scan loop reads from --
    it only writes to ws_shadow_logs/*.jsonl for offline review.
    """

    def __init__(self, kite, symbols: list[str], tokens: dict, exchange_map: dict):
        self.kite = kite
        self.symbols = symbols
        self.tokens = tokens
        self.exchange_map = exchange_map

        self.candle_builders_5m: dict[str, SymbolCandleBuilder] = {
            s: SymbolCandleBuilder(s, interval_minutes=5) for s in symbols
        }
        self.finalized_15m: dict[str, list] = {s: [] for s in symbols}
        self.indicator_state_15m: dict[str, SymbolIndicatorState] = {
            s: SymbolIndicatorState(symbol=s) for s in symbols
        }
        self.indicator_state_5m: dict[str, SymbolIndicatorState] = {
            s: SymbolIndicatorState(symbol=s) for s in symbols
        }

        self.candle_shadow = ShadowComparator(kite, cfg)
        self.indicator_shadow = IncrementalShadowComparator()

        self._last_indicator_check: dict[str, float] = {}
        self._indicator_check_interval_sec = getattr(cfg, "WS_INDICATOR_SHADOW_INTERVAL_MINUTES", 30) * 60
        self._emitted_15m: dict[str, set] = {}
        self._last_finalized_15m: dict[str, dict] = {}
        self._augmentation_count: dict[str, int] = {}
        self._augmentation_skip_count: dict[str, int] = {}

        self.ws_ticker = None

    def seed_from_history(self):
        """Seeds every symbol's indicator state from REST history before any tick is processed."""
        for symbol in self.symbols:
            token = self.tokens.get(symbol)
            if token is None:
                continue
            try:
                df_15m = fetch_candles(self.kite, token, cfg.TREND_TIMEFRAME, lookback_days=5)
                df_5m = fetch_candles(self.kite, token, cfg.ENTRY_TIMEFRAME, lookback_days=5)
                if not df_15m.empty:
                    self.indicator_state_15m[symbol].seed_from_history(df_15m, cfg, "15minute")
                if not df_5m.empty:
                    self.indicator_state_5m[symbol].seed_from_history(df_5m, cfg, "5minute")
                time.sleep(0.3)  # same courtesy delay pattern as run_full_scan's REST calls
            except Exception:
                logger.exception(f"ws_integration: seed_from_history failed for {symbol} -- "
                                  f"that symbol's shadow indicators start cold instead of seeded")

    def handle_tick(self, symbol: str, tick: dict):
        """
        Called from WSTicker's background thread for every tick.
        Wrapped entirely in try/except at the call site (see start())
        so an exception here can never propagate into KiteTicker's
        thread or, transitively, disrupt the main REST-based loop.
        """
        builder = self.candle_builders_5m.get(symbol)
        if builder is None:
            return
        finalized_5m = builder.add_tick(tick)
        if finalized_5m is None:
            return

        exchange = self.exchange_map.get(symbol, "NSE")
        token = self.tokens.get(symbol)

        # -- 5-min candle shadow comparison -----------------------------
        if token is not None:
            try:
                self.candle_shadow.compare_5m_candle(symbol, exchange, finalized_5m, token)
            except Exception:
                logger.exception(f"ws_integration: 5min shadow comparison failed for {symbol}")

        # -- 5-min incremental indicators --------------------------------
        state_5m = self.indicator_state_5m[symbol]
        state_5m.update_ema(cfg.ENTRY_EMA, finalized_5m["close"])
        state_5m.update_volume_avg(finalized_5m["volume"])
        state_5m.update_atr(finalized_5m["high"], finalized_5m["low"], finalized_5m["close"])

        # -- 15-min candles, only from complete 5-min triplets -----------
        self.finalized_15m[symbol].append(finalized_5m)
        combined = combine_5m_into_15m(self.finalized_15m[symbol])
        if combined and combined[-1]["date"] not in self._already_emitted_15m(symbol):
            candle_15m = combined[-1]
            self._mark_emitted_15m(symbol, candle_15m["date"])
            self._last_finalized_15m[symbol] = candle_15m
            state_15m = self.indicator_state_15m[symbol]
            day = candle_15m["date"].date()
            state_15m.update_ema(cfg.TREND_EMA_FAST, candle_15m["close"])
            state_15m.update_ema(cfg.TREND_EMA_SLOW, candle_15m["close"])
            state_15m.update_vwap(day, candle_15m["high"], candle_15m["low"], candle_15m["close"], candle_15m["volume"])
            state_15m.update_adx(candle_15m["high"], candle_15m["low"], candle_15m["close"])

        # -- periodic full-indicator shadow comparison --------------------
        now = time.time()
        last_check = self._last_indicator_check.get(symbol, 0)
        if now - last_check >= self._indicator_check_interval_sec and token is not None:
            self._last_indicator_check[symbol] = now
            self._run_indicator_shadow_comparison(symbol, token)

    def get_augmented_candles(self, symbol: str, timeframe_label: str, df):
        """
        LIVE-MODE ONLY (cfg.WS_CANDLE_MODE == "live"). Given a REST-fetched
        DataFrame `df` (already what main.py would have used unmodified),
        appends the WS-built candle for the NEXT interval after df's last
        row, IF AND ONLY IF all of the following hold:
          - a WS-finalized candle exists for this symbol/timeframe
          - its date is EXACTLY the next expected interval after df's last
            row (never skips ahead, never guesses -- a gap means "don't
            augment", not "fill the gap")
          - it is not stale (within 2x the interval duration of now)
          - all timestamp/timezone normalization succeeds

        On ANY failure, mismatch, or ambiguity -- including an exception
        anywhere in this method -- returns `df` completely unmodified.
        This is the single fail-safe point for the entire live-mode
        feature: a bug here degrades gracefully to today's REST-only
        behavior, it can never produce corrupted or duplicated candle
        data for the caller.

        Returns (df, augmented: bool) so the caller can log/count how
        often augmentation actually happened, for the same kind of
        after-the-fact review review_ws_shadow_logs.py already supports.
        """
        try:
            if df is None or df.empty:
                return df, False

            if timeframe_label == "15minute":
                latest = self._last_finalized_15m.get(symbol)
                interval_minutes = 15
            elif timeframe_label == "5minute":
                builder = self.candle_builders_5m.get(symbol)
                latest = builder.finalized[-1] if builder and builder.finalized else None
                interval_minutes = 5
            else:
                return df, False

            if latest is None:
                return df, False

            import pandas as pd
            from datetime import timedelta

            last_rest_date = df["date"].iloc[-1]
            latest_date = latest["date"]

            # Normalize timezone: match latest_date's tz-awareness to
            # last_rest_date's exactly. If this can't be done cleanly,
            # bail out rather than risk comparing naive to aware (which
            # raises) or silently comparing wrong instants.
            rest_tz = getattr(last_rest_date, "tzinfo", None)
            latest_ts = pd.Timestamp(latest_date)
            if rest_tz is not None and latest_ts.tzinfo is None:
                latest_ts = latest_ts.tz_localize(rest_tz)
            elif rest_tz is None and latest_ts.tzinfo is not None:
                latest_ts = latest_ts.tz_localize(None)
            elif rest_tz is not None and latest_ts.tzinfo is not None:
                latest_ts = latest_ts.tz_convert(rest_tz)

            expected_next = pd.Timestamp(last_rest_date) + timedelta(minutes=interval_minutes)
            if latest_ts != expected_next:
                # Not the immediate next candle (already present, or a
                # gap). Never skip-ahead or backfill here -- too risky
                # to guess; just don't augment this cycle.
                self._augmentation_skip_count[symbol] = self._augmentation_skip_count.get(symbol, 0) + 1
                return df, False

            now_ts = pd.Timestamp.now(tz=rest_tz) if rest_tz is not None else pd.Timestamp.now()
            if (now_ts - latest_ts) > timedelta(minutes=interval_minutes * 2):
                self._augmentation_skip_count[symbol] = self._augmentation_skip_count.get(symbol, 0) + 1
                return df, False

            new_row = pd.DataFrame([{
                "date": latest_ts,
                "open": float(latest["open"]),
                "high": float(latest["high"]),
                "low": float(latest["low"]),
                "close": float(latest["close"]),
                "volume": float(latest["volume"]),
            }])
            augmented_df = pd.concat([df, new_row], ignore_index=True)
            self._augmentation_count[symbol] = self._augmentation_count.get(symbol, 0) + 1
            return augmented_df, True

        except Exception:
            logger.exception(f"ws_integration: get_augmented_candles failed for {symbol}/{timeframe_label} "
                              f"-- falling back to REST-only data for this cycle")
            return df, False

    def _already_emitted_15m(self, symbol: str) -> set:
        return self._emitted_15m.setdefault(symbol, set())

    def _mark_emitted_15m(self, symbol: str, date_val):
        self._already_emitted_15m(symbol).add(date_val)

    def _run_indicator_shadow_comparison(self, symbol: str, token: int):
        from indicators import ema, vwap, atr, adx, average_volume
        try:
            df_15m = fetch_candles(self.kite, token, cfg.TREND_TIMEFRAME, lookback_days=5)
            df_5m = fetch_candles(self.kite, token, cfg.ENTRY_TIMEFRAME, lookback_days=5)
            if not df_15m.empty:
                batch_15m = {
                    f"ema_{cfg.TREND_EMA_FAST}": float(ema(df_15m, cfg.TREND_EMA_FAST).iloc[-1]),
                    f"ema_{cfg.TREND_EMA_SLOW}": float(ema(df_15m, cfg.TREND_EMA_SLOW).iloc[-1]),
                    "vwap": float(vwap(df_15m).iloc[-1]) if not vwap(df_15m).empty else None,
                    "adx": float(adx(df_15m, getattr(cfg, "ADX_PERIOD", 14)).iloc[-1]),
                }
                inc_state = self.indicator_state_15m[symbol]
                inc_15m = {
                    f"ema_{cfg.TREND_EMA_FAST}": inc_state.ema_periods.get(cfg.TREND_EMA_FAST),
                    f"ema_{cfg.TREND_EMA_SLOW}": inc_state.ema_periods.get(cfg.TREND_EMA_SLOW),
                    "vwap": inc_state.vwap_cum_tp_vol / inc_state.vwap_cum_vol if inc_state.vwap_cum_vol else None,
                    "adx": inc_state.adx_state.adx,
                }
                self.indicator_shadow.compare(symbol, "15minute", inc_15m, batch_15m)

            if not df_5m.empty:
                batch_5m = {
                    f"ema_{cfg.ENTRY_EMA}": float(ema(df_5m, cfg.ENTRY_EMA).iloc[-1]),
                    "atr": float(atr(df_5m, 14).iloc[-1]),
                    "avg_volume": float(average_volume(df_5m, cfg.VOLUME_LOOKBACK).iloc[-1]),
                }
                inc_state_5m = self.indicator_state_5m[symbol]
                inc_5m = {
                    f"ema_{cfg.ENTRY_EMA}": inc_state_5m.ema_periods.get(cfg.ENTRY_EMA),
                    "atr": inc_state_5m.atr_value,
                    "avg_volume": (sum(inc_state_5m.volume_window) / len(inc_state_5m.volume_window)
                                   if len(inc_state_5m.volume_window) == inc_state_5m.volume_window_size else None),
                }
                self.indicator_shadow.compare(symbol, "5minute", inc_5m, batch_5m)
        except Exception:
            logger.exception(f"ws_integration: periodic indicator shadow comparison failed for {symbol}")

    def start(self):
        from ws_ticker import WSTicker, build_token_map

        try:
            self.seed_from_history()

            token_to_symbol = {tok: sym for sym, tok in self.tokens.items() if sym in self.symbols}
            sector_indices = getattr(cfg, "WS_SECTOR_INDICES", [])
            if sector_indices:
                sector_tokens = build_token_map(self.kite, [], sector_indices)
                token_to_symbol.update(sector_tokens)

            def _safe_on_tick(symbol, tick):
                try:
                    self.handle_tick(symbol, tick)
                except Exception:
                    logger.exception(f"ws_integration: handle_tick crashed for {symbol} -- "
                                      f"shadow engine continues for other symbols")

            access_token = getattr(self.kite, "access_token", None)
            api_key = getattr(self.kite, "api_key", None) or getattr(cfg, "API_KEY", None)
            if not access_token or not api_key:
                logger.error("ws_integration: could not read api_key/access_token from kite client -- "
                             "shadow engine NOT started, REST-based trading loop is unaffected")
                return

            self.ws_ticker = WSTicker(api_key, access_token, token_to_symbol, on_tick=_safe_on_tick)
            self.ws_ticker.start(threaded=True)
            connected = self.ws_ticker.wait_until_connected(timeout=10.0)
            if connected:
                logger.info(f"ws_integration: WS shadow engine started, mode={cfg.WS_CANDLE_MODE}, "
                            f"{len(token_to_symbol)} instruments subscribed")
            else:
                logger.warning("ws_integration: WS did not confirm connection within 10s -- "
                                "it may still connect via its own reconnect logic; REST loop unaffected either way")
        except Exception:
            logger.exception("ws_integration: failed to start WS shadow engine -- "
                              "REST-based trading loop is completely unaffected by this failure")

    def stop(self):
        try:
            self._log_augmentation_summary()
        except Exception:
            logger.exception("ws_integration: failed to log augmentation summary")
        if self.ws_ticker is not None:
            try:
                self.ws_ticker.stop()
            except Exception:
                logger.exception("ws_integration: error stopping WS ticker")

    def _log_augmentation_summary(self):
        """
        Logs, per symbol, how many times a WS-built candle was actually
        appended (get_augmented_candles returned augmented=True) versus
        how many times it was skipped for a fail-safe reason (gap,
        staleness, missing data). Only meaningful when
        cfg.WS_CANDLE_MODE == "live" -- in shadow mode these stay at 0
        since get_augmented_candles is never called by main.py.
        This is the first thing to check after a live-mode session:
        zero augmentations across the board usually means the WS feed
        never caught up in time to matter, not that something is broken.
        """
        all_symbols = set(self._augmentation_count) | set(self._augmentation_skip_count)
        if not all_symbols:
            logger.info("ws_integration: session summary -- no augmentation attempts recorded "
                        "(WS_CANDLE_MODE was likely 'shadow', or no candles closed during this session)")
            return
        for symbol in sorted(all_symbols):
            used = self._augmentation_count.get(symbol, 0)
            skipped = self._augmentation_skip_count.get(symbol, 0)
            logger.info(f"ws_integration: session summary | {symbol} | "
                        f"WS candle used={used} | skipped_for_safety={skipped}")


def start_ws_shadow_engine(kite, symbols: list, tokens: dict, exchange_map: dict) -> Optional[WSShadowEngine]:
    """
    Entry point called from main.py's run(). Returns the engine (so
    main.py can stop() it on shutdown) or None if ENABLE_WS_CANDLES is
    off or startup failed. NEVER raises -- any failure here must leave
    the existing REST-based trading loop completely unaffected.
    """
    if not getattr(cfg, "ENABLE_WS_CANDLES", False):
        return None
    try:
        engine = WSShadowEngine(kite, symbols, tokens, exchange_map)
        engine.start()
        return engine
    except Exception:
        logger.exception("ws_integration: start_ws_shadow_engine failed entirely -- "
                          "continuing with REST-only trading loop")
        return None
