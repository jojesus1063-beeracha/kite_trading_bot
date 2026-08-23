import os
import tempfile

import pytest

from fno_bot.risk import shared_capital as sc


@pytest.fixture
def tmp_ledger_path():
    with tempfile.TemporaryDirectory() as d:
        yield os.path.join(d, "shared_capital_ledger_test.json")


def test_reserve_and_release_roundtrip(tmp_ledger_path):
    rid = sc.reserve(tmp_ledger_path, bot="fno", amount=5000.0, note="test entry")
    assert sc.total_reserved(tmp_ledger_path) == 5000.0
    sc.release(tmp_ledger_path, rid)
    assert sc.total_reserved(tmp_ledger_path) == 0.0


def test_total_reserved_excludes_own_bot_when_asked(tmp_ledger_path):
    sc.reserve(tmp_ledger_path, bot="fno", amount=1000.0)
    sc.reserve(tmp_ledger_path, bot="equity", amount=2000.0)
    assert sc.total_reserved(tmp_ledger_path) == 3000.0
    assert sc.total_reserved(tmp_ledger_path, exclude_bot="fno") == 2000.0


def test_release_unknown_reservation_is_a_safe_noop(tmp_ledger_path):
    sc.reserve(tmp_ledger_path, bot="fno", amount=1000.0)
    sc.release(tmp_ledger_path, "does-not-exist")  # must not raise
    assert sc.total_reserved(tmp_ledger_path) == 1000.0
