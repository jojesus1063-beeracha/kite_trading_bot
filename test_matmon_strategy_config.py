import inspect

import matmon_live_candidate_launcher as candidate
import matmon_live_launcher as live
import matmon_preopen_top120 as preopen
import matmon_strategy_config as strategy_def
import paper_matmon_launcher as paper_matmon


def test_strategy_definition_resolves_to_intended_values():
    assert strategy_def.MATMON_WATCHLIST_SIZE == 60
    assert strategy_def.MATMON_EMA_FAST == 3
    assert strategy_def.MATMON_EMA_SLOW == 15
    assert strategy_def.MATMON_DI_PERIOD == 14
    assert strategy_def.MATMON_ENTRY_TIMEFRAME == "3minute"
    assert strategy_def.MATMON_QUOTE_WINDOW_SECONDS == 3.0
    assert strategy_def.MATMON_QUOTE_MAX_AGE_SECONDS == 2.0


def test_paper_matmon_required_contract_matches_central_definition():
    assert paper_matmon.MATMON_REQUIRED["MATMON_EMA_FAST"] == strategy_def.MATMON_EMA_FAST
    assert paper_matmon.MATMON_REQUIRED["MATMON_EMA_SLOW"] == strategy_def.MATMON_EMA_SLOW
    assert paper_matmon.MATMON_REQUIRED["MATMON_DI_PERIOD"] == strategy_def.MATMON_DI_PERIOD
    assert paper_matmon.MATMON_REQUIRED["ENTRY_TIMEFRAME"] == strategy_def.MATMON_ENTRY_TIMEFRAME
    assert (
        paper_matmon.MATMON_REQUIRED["MATMON_QUOTE_WINDOW_SECONDS"]
        == strategy_def.MATMON_QUOTE_WINDOW_SECONDS
    )
    assert (
        paper_matmon.MATMON_REQUIRED["MATMON_QUOTE_MAX_AGE_SECONDS"]
        == strategy_def.MATMON_QUOTE_MAX_AGE_SECONDS
    )
    # This is the Top-60 assertion: a 60-symbol Matmon watchlist must not be
    # silently expanded back to 120 by the contract layer.
    assert paper_matmon.MATMON_REQUIRED["ENTRY_SCAN_SHORTLIST_SIZE"] == 60
    assert paper_matmon.MATMON_REQUIRED["ENTRY_SCAN_SHORTLIST_SIZE"] == strategy_def.MATMON_WATCHLIST_SIZE


def test_paper_matmon_enforce_settings_does_not_hardcode_strategy_values():
    # Source-level guard: each centralized strategy field must be assigned
    # from strategy_def, not from an independent literal, inside
    # enforce_settings(). (Risk/execution literals like RISK_PER_TRADE_PCT
    # are a separate, legitimately-hardcoded category and are not checked
    # here.)
    source = inspect.getsource(paper_matmon.enforce_settings)
    strategy_lines = {
        "cfg.MATMON_EMA_FAST": "strategy_def.MATMON_EMA_FAST",
        "cfg.MATMON_EMA_SLOW": "strategy_def.MATMON_EMA_SLOW",
        "cfg.MATMON_DI_PERIOD": "strategy_def.MATMON_DI_PERIOD",
        "cfg.MATMON_QUOTE_WINDOW_SECONDS": "strategy_def.MATMON_QUOTE_WINDOW_SECONDS",
        "cfg.MATMON_QUOTE_MAX_AGE_SECONDS": "strategy_def.MATMON_QUOTE_MAX_AGE_SECONDS",
        "cfg.ENTRY_TIMEFRAME": "strategy_def.MATMON_ENTRY_TIMEFRAME",
        "cfg.ENTRY_SCAN_SHORTLIST_SIZE": "strategy_def.MATMON_WATCHLIST_SIZE",
    }
    for line in source.splitlines():
        stripped = line.strip()
        for lhs, expected_rhs in strategy_lines.items():
            if stripped.startswith(lhs + " ="):
                assert expected_rhs in stripped, f"{stripped!r} does not read from {expected_rhs}"


def test_live_launcher_strategy_values_match_paper(monkeypatch):
    import config as cfg
    from types import SimpleNamespace

    fake_cfg = SimpleNamespace(
        PAPER_TRADING=False,
        PRODUCT="MIS",
        CAPITAL=5000.0,
        MARKET_PROTECTION=-1,
        ENABLE_WS_CANDLES=True,
    )
    monkeypatch.setattr(live, "cfg", fake_cfg)
    monkeypatch.setenv(live.LIVE_ACK_ENV, live.LIVE_ACK_VALUE)
    live.enforce_live_limits()

    assert fake_cfg.MATMON_EMA_FAST == strategy_def.MATMON_EMA_FAST
    assert fake_cfg.MATMON_EMA_SLOW == strategy_def.MATMON_EMA_SLOW
    assert fake_cfg.MATMON_DI_PERIOD == strategy_def.MATMON_DI_PERIOD
    assert fake_cfg.ENTRY_TIMEFRAME == strategy_def.MATMON_ENTRY_TIMEFRAME
    assert fake_cfg.ENTRY_SCAN_SHORTLIST_SIZE == strategy_def.MATMON_WATCHLIST_SIZE
    assert fake_cfg.ENTRY_SCAN_SHORTLIST_SIZE == 60
    # Live risk caps remain a distinct, live-specific concern.
    assert fake_cfg.RISK_PER_TRADE_PCT == live.LIVE_RISK_PER_TRADE_PCT
    assert fake_cfg.DAILY_LOSS_KILL_SWITCH_ENABLED is True


def test_live_launcher_no_longer_duplicates_strategy_literals():
    source = inspect.getsource(live)
    assert "LIVE_MATMON_EMA_FAST" not in source
    assert "LIVE_ENTRY_SCAN_SHORTLIST_SIZE" not in source
    assert "strategy_def." in source


def test_candidate_launcher_fallback_defaults_match_central_definition():
    source = inspect.getsource(candidate.authorize_candidate)
    assert "strategy_def.MATMON_QUOTE_WINDOW_SECONDS" in source
    assert "strategy_def.MATMON_QUOTE_MAX_AGE_SECONDS" in source


def test_preopen_selector_uses_central_watchlist_size_not_120():
    assert preopen.TOP_N == strategy_def.MATMON_WATCHLIST_SIZE
    assert preopen.TOP_N == 60


def test_existing_dry_run_safety_contract_unchanged():
    # Confirms this refactor did not touch the live-candidate's execution
    # boundary: still PAPER_TRADING-only, still no broker-order tokens.
    from types import SimpleNamespace

    cfg_obj = SimpleNamespace(
        PAPER_TRADING=True,
        ENABLE_WS_CANDLES=True,
        WS_CANDLE_MODE="shadow",
        ENTRY_TIMEFRAME="3minute",
        MATMON_EMA_FAST=3,
        MATMON_EMA_SLOW=15,
        MATMON_DI_PERIOD=14,
        CHECK_MARGIN_BEFORE_ENTRY=True,
    )
    assert candidate.assert_dry_run_contract(cfg_obj) is True
    source = inspect.getsource(candidate)
    for token in ("place_order(", "modify_order(", "cancel_order(", "combined_live_launcher", "LIVE_ACK_VALUE"):
        assert token not in source
