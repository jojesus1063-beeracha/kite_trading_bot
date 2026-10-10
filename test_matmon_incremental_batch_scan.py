from types import SimpleNamespace

import main


def test_incremental_scan_confirms_after_each_bounded_batch(monkeypatch):
    calls = []
    monkeypatch.setattr(main.cfg, "MATMON_IMMEDIATE_BATCH_CONFIRMATION", True, raising=False)
    monkeypatch.setattr(main.cfg, "MATMON_SCAN_BATCH_SIZE", 2, raising=False)

    def fake_batch(kite, symbols, tokens, exchange_map, open_positions, risk, **kwargs):
        calls.append(list(symbols))
        return [{"symbol": symbol, "status": "checked"} for symbol in symbols]

    monkeypatch.setattr(main, "run_full_scan", fake_batch)
    result = main.run_incremental_scan(
        object(), ["A", "B", "C", "D", "E"], {}, {}, {}, SimpleNamespace()
    )

    assert calls == [["A", "B"], ["C", "D"], ["E"]]
    assert [row["symbol"] for row in result] == ["A", "B", "C", "D", "E"]


def test_incremental_scan_deduplicates_symbols_within_cycle(monkeypatch):
    calls = []
    monkeypatch.setattr(main.cfg, "MATMON_IMMEDIATE_BATCH_CONFIRMATION", True, raising=False)
    monkeypatch.setattr(main.cfg, "MATMON_SCAN_BATCH_SIZE", 2, raising=False)
    monkeypatch.setattr(
        main,
        "run_full_scan",
        lambda kite, symbols, *args, **kwargs: calls.append(list(symbols)) or [],
    )

    main.run_incremental_scan(object(), ["A", "A", "B", "B"], {}, {}, {}, SimpleNamespace())

    assert calls == [["A"], ["B"]]


def test_disabled_mode_preserves_single_full_scan(monkeypatch):
    calls = []
    monkeypatch.setattr(main.cfg, "MATMON_IMMEDIATE_BATCH_CONFIRMATION", False, raising=False)
    monkeypatch.setattr(main.cfg, "MATMON_SCAN_BATCH_SIZE", 2, raising=False)
    monkeypatch.setattr(
        main,
        "run_full_scan",
        lambda kite, symbols, *args, **kwargs: calls.append(list(symbols)) or [],
    )

    main.run_incremental_scan(object(), ["A", "B", "C"], {}, {}, {}, SimpleNamespace())

    assert calls == [["A", "B", "C"]]


def test_live_launcher_enables_safe_incremental_batches():
    source = open("matmon_live_launcher.py", encoding="utf-8").read()
    assert "cfg.MATMON_IMMEDIATE_BATCH_CONFIRMATION = True" in source
    assert "cfg.MATMON_SCAN_BATCH_SIZE = 12" in source
    assert "cfg.MATMON_PREFETCH_WORKERS = 3" in source
