import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import matmon_production_staging as staging
from matmon_live_candidate_launcher import CandidateResult


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


def test_01_rejected_candidate_cannot_create_execution_intent():
    result = CandidateResult(False, "BUY", "REJECTED")
    assert staging.build_execution_intent(symbol="TEST", result=result) is None


def test_02_accepted_candidate_creates_audit_intent_only():
    result = CandidateResult(True, "BUY", "MATMON_DRY_RUN_AUTHORIZED")
    intent = staging.build_execution_intent(symbol="TEST", result=result)
    assert intent is not None
    assert intent.symbol == "TEST"
    assert intent.direction == "BUY"
    assert intent.execution_boundary == "BROKER_EXECUTION_DISABLED"


def test_03_submission_boundary_always_fails_closed():
    result = CandidateResult(True, "SELL", "MATMON_DRY_RUN_AUTHORIZED")
    intent = staging.build_execution_intent(symbol="TEST", result=result)
    try:
        staging.submit_execution_intent(intent)
    except RuntimeError as exc:
        assert "BROKER_EXECUTION_DISABLED" in str(exc)
    else:
        raise AssertionError("staging submission boundary must fail closed")


def test_04_staging_has_no_broker_execution_calls_or_executor_import():
    source = inspect.getsource(staging)
    tree = ast.parse(source)
    forbidden_calls = {"place_order", "modify_order", "cancel_order", "place_entry_order"}
    invoked = set()
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                invoked.add(func.id)
            elif isinstance(func, ast.Attribute):
                invoked.add(func.attr)
        elif isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert forbidden_calls.isdisjoint(invoked)
    assert "executor" not in imported_modules
    assert "protective_stop" not in imported_modules


def test_05_staging_service_is_isolated_oneshot():
    project = Path(__file__).resolve().parent
    unit = (project / "systemd" / "kitebot-matmon-production-staging.service").read_text()
    assert "Type=oneshot" in unit
    assert "matmon_production_staging.py" in unit
    assert "combined_live_launcher.py" not in unit
    assert "paper_matmon_launcher.py" not in unit
