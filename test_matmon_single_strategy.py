from types import SimpleNamespace
import importlib.util
from pathlib import Path
import pytest
import matmon_live_launcher as live
import strategy


def test_old_entry_engine_is_not_available():
    import importlib
    fresh = importlib.reload(strategy)
    with pytest.raises(RuntimeError, match='MATMON hooks'):
        fresh.evaluate(None, None, None, None, None)


def test_launch_restores_observed_risk_and_exit_policy(monkeypatch):
    cfg = SimpleNamespace(PAPER_TRADING=False, PRODUCT='MIS', CAPITAL=5000,
                          MARKET_PROTECTION=-1, ENABLE_WS_CANDLES=True,
                          STOP_LOSS_PERCENT=0.45, ENABLE_HYBRID_EXIT=False,
                          ENABLE_TRAILING_STOP=True)
    monkeypatch.setattr(live, 'cfg', cfg)
    monkeypatch.setenv(live.LIVE_ACK_ENV, live.LIVE_ACK_VALUE)
    result = live.enforce_live_limits()
    assert result['strategy'] == 'MATMON_HAELOHIM'
    assert (cfg.CAPITAL, cfg.RISK_PER_TRADE_PCT, cfg.MAX_OPEN_POSITIONS) == (5000, 2, 3)
    assert cfg.STOP_LOSS_PERCENT == 1
    assert cfg.ENABLE_HYBRID_EXIT and not cfg.ENABLE_TRAILING_STOP
    assert (cfg.HYBRID_SCALP_FRACTION, cfg.HYBRID_SCALP_R, cfg.HYBRID_RUNNER_R) == (0.5, 1, 2)
    assert cfg.ENTRY_SCAN_SHORTLIST_SIZE == 120
    assert cfg.MATMON_IMMEDIATE_BATCH_CONFIRMATION
    assert cfg.MATMON_SCAN_BATCH_SIZE == 12


def installer():
    path = Path(__file__).parent / 'tools/install_matmon.py'
    spec = importlib.util.spec_from_file_location('installer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cleanup_preserves_credentials_state_logs_and_venv(tmp_path):
    for n in ['old_launcher.py', 'matmon_live_launcher.py', '.env', 'access_token.txt',
              'user_config.json', 'trade_history.jsonl', 'protective_stops.json']:
        (tmp_path/n).write_text('{}')
    for n in ['runtime', 'venv', '.git']:
        (tmp_path/n).mkdir();(tmp_path/n/'old.py').write_text('')
    (tmp_path/'old_link.py').symlink_to(tmp_path/'old_launcher.py')
    found=installer().cleanup_candidates(tmp_path, {'matmon_live_launcher.py': 'hash'})
    assert [p.name for p in found] == ['old_launcher.py']


def test_data_containing_backup_is_not_deleted_as_directory(tmp_path):
    p=tmp_path/'matmon_old_backup';p.mkdir()
    (p/'trades.jsonl').write_text('{}')
    assert p not in installer().cleanup_candidates(tmp_path,{})


def test_preflight_rejects_unreadable_broker_state():
    from matmon_safety import validate_broker_flat
    with pytest.raises(RuntimeError, match='positions response'):
        validate_broker_flat(SimpleNamespace(positions=lambda: None))
    with pytest.raises(RuntimeError, match='orders response'):
        validate_broker_flat(SimpleNamespace(positions=lambda: {'net': [], 'day': []}, orders=lambda: None))


def test_preflight_does_not_ignore_previous_day_positions(tmp_path):
    import json
    from matmon_safety import validate_local_flat
    (tmp_path/'open_positions.json').write_text(json.dumps({'date': '2020-01-01', 'positions': {'ABC': {'qty': 1}}}))
    with pytest.raises(RuntimeError, match='exposure'):
        validate_local_flat(tmp_path)
