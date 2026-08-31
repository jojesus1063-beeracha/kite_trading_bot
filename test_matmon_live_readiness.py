import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import matmon_live_candidate_launcher as candidate
import matmon_live_readiness as readiness


def _cfg(**overrides):
    values = dict(
        PAPER_TRADING=True,
        ENABLE_WS_CANDLES=True,
        WS_CANDLE_MODE="shadow",
        ENTRY_TIMEFRAME="3minute",
        MATMON_EMA_FAST=3,
        MATMON_EMA_SLOW=15,
        MATMON_DI_PERIOD=14,
        CHECK_MARGIN_BEFORE_ENTRY=True,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_01_live_candidate_contract_accepts_only_verified_dry_run_shape():
    assert candidate.assert_dry_run_contract(_cfg()) is True


def test_02_live_candidate_contract_blocks_nonpaper_and_ws_live():
    for bad in (_cfg(PAPER_TRADING=False), _cfg(WS_CANDLE_MODE="live")):
        try:
            candidate.assert_dry_run_contract(bad)
        except SystemExit:
            pass
        else:
            raise AssertionError("unsafe candidate configuration must fail closed")


def test_03_readiness_code_contract_contains_no_execution_boundary():
    """Reject actual execution calls, not harmless audit strings naming them."""
    source = inspect.getsource(readiness)
    tree = ast.parse(source)
    forbidden_calls = {
        "place_order",
        "modify_order",
        "cancel_order",
        "place_entry_order",
    }
    invoked = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            invoked.add(func.id)
        elif isinstance(func, ast.Attribute):
            invoked.add(func.attr)
    assert forbidden_calls.isdisjoint(invoked)
    assert "DRY_RUN_ONLY" in source


def test_04_readiness_service_is_oneshot_candidate_check_only():
    project = Path(__file__).resolve().parent
    unit = (project / "systemd" / "kitebot-matmon-live-candidate-readiness.service").read_text()
    assert "Type=oneshot" in unit
    assert "matmon_live_readiness.py" in unit
    assert "combined_live_launcher.py" not in unit
    assert "paper_matmon_launcher.py" not in unit
