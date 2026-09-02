from copy import deepcopy

from breakout_diagnostics import enrich_breakout_diagnostics


def sample_event():
    return {
        "symbol": "KRONOX",
        "decision": "REJECT",
        "reasons": ["BREAKOUT_VALIDATION_FAILED"],
        "price_action_confirmation": {
            "breakout_validation": {
                "passed": False,
                "direction": "BUY",
                "reasons": ["N_PERIOD_EXTREMUM_NOT_BROKEN", "CLV_DIRECTION_NOT_CONFIRMED"],
                "metrics": {
                    "minimum_volume_ratio": 1.5,
                    "minimum_atr_multiplier": 1.2,
                    "clv_threshold": 0.6,
                    "volume_ratio": 2.13,
                    "atr_multiplier": 1.255,
                    "clv": 0.289,
                    "structure_confirmed": False,
                    "volume_confirmed": True,
                    "volatility_confirmed": True,
                    "clv_confirmed": False,
                },
            }
        },
    }


def test_enrichment_is_diagnostics_only():
    event = sample_event()
    before = deepcopy(event)
    result = enrich_breakout_diagnostics(event)

    assert result is event
    assert result["decision"] == before["decision"]
    assert result["reasons"] == before["reasons"]
    assert result["price_action_confirmation"] == before["price_action_confirmation"]

    diag = result["breakout_diagnostics"]
    assert diag["status"] == "FAIL"
    assert diag["failed_components"] == ["STRUCTURE_FAIL", "CLV_FAIL"]
    assert diag["structure_break"] is False
    assert diag["volume_ratio"] == 2.13
    assert diag["volume_pass"] is True
    assert diag["atr_multiple"] == 1.255
    assert diag["atr_pass"] is True
    assert diag["clv_value"] == 0.289
    assert diag["clv_pass"] is False
    assert diag["primary_rejection_reason"] == "MULTIPLE_BREAKOUT_COMPONENTS_FAILED"
    assert diag["secondary_rejection_reasons"] == ["STRUCTURE_FAIL", "CLV_FAIL"]


def test_non_breakout_event_unchanged():
    event = {"symbol": "ABC", "decision": "REJECT", "reasons": ["ADX_STRENGTH_BELOW_MINIMUM"]}
    before = deepcopy(event)
    assert enrich_breakout_diagnostics(event) == before
    assert "breakout_diagnostics" not in event
