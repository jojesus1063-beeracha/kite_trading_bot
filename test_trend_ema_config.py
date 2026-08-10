from pathlib import Path

import config as cfg


assert cfg.TREND_EMA_FAST == 9, (
    f"expected effective TREND_EMA_FAST=9, got {cfg.TREND_EMA_FAST}"
)
assert cfg.TREND_EMA_SLOW == 21, (
    f"expected effective TREND_EMA_SLOW=21, got {cfg.TREND_EMA_SLOW}"
)
assert cfg.TREND_EMA_FAST < cfg.TREND_EMA_SLOW

indicators_source = Path("indicators.py").read_text()
incremental_source = Path("indicators_incremental.py").read_text()
ws_source = Path("ws_integration.py").read_text()

assert "ema(df_15m, cfg.TREND_EMA_FAST)" in indicators_source
assert "ema(df_15m, cfg.TREND_EMA_SLOW)" in indicators_source
assert "ema(df, cfg.TREND_EMA_FAST)" in incremental_source
assert "ema(df, cfg.TREND_EMA_SLOW)" in incremental_source
assert "update_ema(cfg.TREND_EMA_FAST" in ws_source
assert "update_ema(cfg.TREND_EMA_SLOW" in ws_source

print("PASS: effective 15-minute trend EMA pair is 9/21")
print("PASS: REST, incremental, and WebSocket trend EMAs are config-driven")
