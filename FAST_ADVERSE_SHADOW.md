# Fast adverse shadow telemetry

Purpose: observe whether a WebSocket-LTP early adverse exit would have helped, without granting the rule any order authority.

Initial policy:
- arm at >= 0.60R adverse movement
- disarm after recovery below 0.50R
- require >= 3 seconds armed and at least 2 fresh observations
- max WS tick age 2 seconds
- log transitions to `runtime/fast_adverse_shadow/events.jsonl`
- NO order placement
- NO `fast_adverse_exit` result injected into `check_position_exit`
- broker protective stop remains authoritative
- `WS_CANDLE_MODE` remains `shadow`

VM rollout:
1. Copy `fast_adverse_shadow.py` and `vm_fast_adverse_shadow_patch.py` into `~/kite_trading_bot`.
2. Compile both.
3. Run `python3 vm_fast_adverse_shadow_patch.py --check`.
4. Confirm simulated state has observer wired and `live fast-exit trigger added: False`.
5. Run `python3 vm_fast_adverse_shadow_patch.py --apply`.
6. Compile `main.py` and the observer.
7. Restart after market hours and verify startup; during a trading session inspect `runtime/fast_adverse_shadow/events.jsonl` and `FAST ADVERSE SHADOW` journal lines.

The observer reads the already-running `ws_shadow_engine.ws_ticker.tick_buffer.latest(symbol)`. Missing or stale ticks never cause an exit; they simply skip shadow observation.
