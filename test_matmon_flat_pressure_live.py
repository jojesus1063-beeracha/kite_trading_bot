from matmon_microstructure import evaluate_flat_with_pressure


def tick(ts, *, bid_qty, ask_qty, ltp=100.05, bid=100.0, ask=100.1):
    return {
        "received_at": ts,
        "last_price": ltp,
        "depth": {
            "buy": [{"price": bid - i * 0.05, "quantity": bid_qty} for i in range(5)],
            "sell": [{"price": ask + i * 0.05, "quantity": ask_qty} for i in range(5)],
        },
    }


def test_flat_buy_with_strong_strengthening_pressure_passes():
    result = evaluate_flat_with_pressure("BUY", [
        tick(1.0, bid_qty=140, ask_qty=80),
        tick(2.0, bid_qty=190, ask_qty=60),
        tick(3.0, bid_qty=260, ask_qty=40),
    ])
    assert result.accepted
    assert result.confirmation_path == "FLAT_WITH_PRESSURE"
    assert result.last_weighted_5_imbalance >= 0.30
    assert result.microprice_change > 0


def test_flat_sell_with_strong_strengthening_pressure_passes():
    result = evaluate_flat_with_pressure("SELL", [
        tick(1.0, bid_qty=80, ask_qty=140),
        tick(2.0, bid_qty=60, ask_qty=190),
        tick(3.0, bid_qty=40, ask_qty=260),
    ])
    assert result.accepted
    assert result.last_weighted_5_imbalance <= -0.30
    assert result.microprice_change < 0


def test_weak_pressure_adverse_ltp_and_nonflat_quotes_reject():
    weak = [tick(1.0, bid_qty=105, ask_qty=100), tick(2.0, bid_qty=108, ask_qty=100), tick(3.0, bid_qty=110, ask_qty=100)]
    assert not evaluate_flat_with_pressure("BUY", weak).accepted

    adverse = [tick(1.0, bid_qty=140, ask_qty=80), tick(2.0, bid_qty=190, ask_qty=60), tick(3.0, bid_qty=260, ask_qty=40, ltp=100.00)]
    assert not evaluate_flat_with_pressure("BUY", adverse).accepted

    nonflat = [tick(1.0, bid_qty=140, ask_qty=80), tick(2.0, bid_qty=190, ask_qty=60), tick(3.0, bid_qty=260, ask_qty=40, bid=100.05, ask=100.15)]
    assert not evaluate_flat_with_pressure("BUY", nonflat).accepted
