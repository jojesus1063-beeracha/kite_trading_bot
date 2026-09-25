# EL-BETHEL V2.1 shadow validation

This branch adds observation-only telemetry for the historical
"failure-to-launch" hypothesis.

Policy under observation:

- evaluate an open position after 9 minutes;
- MFE > 0.10% => `HEALTHY_LAUNCH`;
- MFE <= 0.10% and the trade is no longer directionally profitable =>
  `FAILURE_TO_LAUNCH_SHADOW`;
- if entry ADX >= 30 and directional DI gap >= 20, classify the same
  low-MFE situation as `STRONG_ENTRY_EXCEPTION` and grant more time;
- low MFE with a still-positive directional return remains `WAITING`.

Nothing in this branch requests a broker exit or changes an order.

## VM validation

```bash
cd ~/kite_trading_bot
source venv/bin/activate
python3 vm_v21_shadow_patch.py --check
pytest -q tests/test_failure_to_launch_shadow.py
```

Only after the check and tests pass:

```bash
python3 vm_v21_shadow_patch.py --apply
python3 -m py_compile main.py failure_to_launch_shadow.py
```

The patcher backs up `main.py` and does not restart any service.
