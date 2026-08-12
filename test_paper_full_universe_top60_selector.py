import json
from pathlib import Path

import pytest

import paper_full_universe_top60_selector as s


def instrument(symbol="RELIANCE", *, exchange="NSE", isin="INE002A01018", name="RELIANCE INDUSTRIES", lot_size=1, instrument_type="EQ", segment=None):
    return {
        "exchange": exchange,
        "segment": segment or exchange,
        "instrument_type": instrument_type,
        "tradingsymbol": symbol,
        "name": name,
        "isin": isin,
        "lot_size": lot_size,
        "instrument_token": 123,
        "tick_size": 0.05,
    }


def test_regular_ine_equity_is_allowed():
    assert s.ordinary_equity_rejection_reason(instrument(), "NSE") is None


def test_regular_equity_with_missing_isin_is_allowed():
    row = instrument(isin="")
    assert s.ordinary_equity_rejection_reason(row, "NSE") is None


def test_etf_isin_is_rejected():
    row = instrument("SILVERIETF", isin="INF109KC1Y56", name="ICICI PRUDENTIAL SILVER ETF")
    assert s.ordinary_equity_rejection_reason(row, "NSE") == "non_ordinary_isin"


def test_missing_isin_does_not_bypass_fund_symbol_filter():
    row = instrument("SILVERIETF", isin="", name="ICICI PRUDENTIAL SILVER ETF")
    assert s.ordinary_equity_rejection_reason(row, "NSE") == "fund_like_symbol"


def test_special_be_series_is_rejected():
    row = instrument("E2E-BE")
    assert s.ordinary_equity_rejection_reason(row, "NSE") == "special_series_suffix"


def test_sme_and_st_series_are_rejected():
    assert s.ordinary_equity_rejection_reason(instrument("ABC-SM"), "NSE") == "special_series_suffix"
    assert s.ordinary_equity_rejection_reason(instrument("ABC-ST"), "NSE") == "special_series_suffix"


def test_goldiam_is_not_false_positive():
    row = instrument("GOLDIAM", name="GOLDIAM INTERNATIONAL LIMITED", isin="INE025B01025")
    assert s.ordinary_equity_rejection_reason(row, "NSE") is None


def test_non_unit_lot_is_rejected():
    row = instrument(lot_size=10)
    assert s.ordinary_equity_rejection_reason(row, "NSE") == "lot_size_not_one"


def test_name_level_etf_defence_rejects_even_ine_fixture():
    row = instrument("ODDNAME", isin="INE000A00001", name="SAMPLE EXCHANGE TRADED FUND")
    assert s.ordinary_equity_rejection_reason(row, "NSE") == "fund_or_debt_like_name"


def test_write_preserves_settings_and_exchange(tmp_path: Path):
    config = tmp_path / "user_config.json"
    config.write_text(json.dumps({"paper_trading": True, "capital": 5000, "watchlist": []}))
    selected = [
        {"symbol": "AAA", "exchange": "NSE"},
        {"symbol": "BBB", "exchange": "BSE"},
    ]
    backup = s.write_paper_watchlist(
        config,
        selected,
        min_selected=2,
        runtime_dir=tmp_path / "runtime",
    )
    updated = json.loads(config.read_text())
    assert updated["paper_trading"] is True
    assert updated["capital"] == 5000
    assert updated["watchlist"] == selected
    assert backup.exists()


def test_write_refuses_live_mode(tmp_path: Path):
    config = tmp_path / "user_config.json"
    config.write_text(json.dumps({"paper_trading": False, "watchlist": []}))
    with pytest.raises(RuntimeError, match="PAPER mode"):
        s.write_paper_watchlist(
            config,
            [{"symbol": "AAA", "exchange": "NSE"}],
            min_selected=1,
            runtime_dir=tmp_path / "runtime",
        )


def test_write_refuses_cross_exchange_duplicate_symbol(tmp_path: Path):
    config = tmp_path / "user_config.json"
    config.write_text(json.dumps({"paper_trading": True, "watchlist": []}))
    with pytest.raises(RuntimeError, match="duplicate symbols"):
        s.write_paper_watchlist(
            config,
            [
                {"symbol": "AAA", "exchange": "NSE"},
                {"symbol": "AAA", "exchange": "BSE"},
            ],
            min_selected=2,
            runtime_dir=tmp_path / "runtime",
        )
