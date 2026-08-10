import config as cfg


assert cfg.TREND_EMA_FAST == 9, (
    f"expected effective TREND_EMA_FAST=9, got {cfg.TREND_EMA_FAST}"
)
assert cfg.TREND_EMA_SLOW == 21, (
    f"expected effective TREND_EMA_SLOW=21, got {cfg.TREND_EMA_SLOW}"
)
assert cfg.TREND_EMA_FAST < cfg.TREND_EMA_SLOW

print("PASS: effective 15-minute trend EMA pair is 9/21")
