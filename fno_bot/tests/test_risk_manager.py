from fno_bot.risk.risk_manager import compute_quantity


def test_compute_quantity_never_hardcodes_100():
    # Spec explicitly warns against `quantity = 100`. Prove the result
    # actually varies with inputs (i.e. it's genuinely computed).
    r1 = compute_quantity(50000, 20, 5, 5, entry_reference_price=196.80, lot_size=10)
    r2 = compute_quantity(100000, 20, 5, 5, entry_reference_price=196.80, lot_size=10)
    assert r1.quantity != r2.quantity


def test_compute_quantity_floors_to_lot_size():
    result = compute_quantity(50000, 20, 5, 5, entry_reference_price=196.80, lot_size=10)
    assert result.quantity % 10 == 0


def test_compute_quantity_no_trade_when_below_one_lot():
    # Tiny capital, expensive premium -> can't even afford 1 lot
    result = compute_quantity(500, 20, 5, 5, entry_reference_price=5000.0, lot_size=10)
    assert result.lots == 0
    assert result.quantity == 0
    assert "NO_TRADE" in result.reason


def test_compute_quantity_risk_limited_below_capital_limited():
    # Huge capital budget but a tight risk budget should bind first.
    result = compute_quantity(
        fno_capital=1_000_000, max_capital_per_trade_pct=100, max_risk_per_trade_pct=0.1,
        stop_loss_pct=5, entry_reference_price=200.0, lot_size=10,
    )
    assert result.lots >= 0
    assert "risk-limited" in result.reason


def test_compute_quantity_rejects_invalid_inputs():
    assert compute_quantity(50000, 20, 5, 5, entry_reference_price=200.0, lot_size=0).lots == 0
    assert compute_quantity(50000, 20, 5, 5, entry_reference_price=0.0, lot_size=10).lots == 0
    assert compute_quantity(50000, 20, 5, 0, entry_reference_price=200.0, lot_size=10).lots == 0
