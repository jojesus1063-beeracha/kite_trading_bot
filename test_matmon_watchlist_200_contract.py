import matmon_live_launcher as live
import matmon_preopen_top120 as preopen
import matmon_strategy_config as strategy
import matmon_policy as paper


def test_all_matmon_paths_share_top_120_contract():
    assert strategy.MATMON_WATCHLIST_SIZE == 120
    assert preopen.TOP_N == 120
    assert paper.MATMON_REQUIRED["ENTRY_SCAN_SHORTLIST_SIZE"] == 120
    assert live.strategy_def.MATMON_WATCHLIST_SIZE == 120


def test_only_universe_size_changed():
    assert strategy.MATMON_EMA_FAST == 3
    assert strategy.MATMON_EMA_SLOW == 15
    assert strategy.MATMON_DI_PERIOD == 14
    assert strategy.MATMON_ENTRY_TIMEFRAME == "3minute"
    assert strategy.MATMON_QUOTE_WINDOW_SECONDS == 3.0
    assert strategy.MATMON_QUOTE_MAX_AGE_SECONDS == 2.0
