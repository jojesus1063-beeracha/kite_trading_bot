#!/usr/bin/env bash
set -euo pipefail

echo "=================================================="
echo "Stage 5 verified FORCE_EXIT regression"
echo "=================================================="

echo
echo "[1/4] Python compilation"
python3 -m py_compile \
  executor.py \
  main.py \
  test_force_exit_integration.py \
  test_force_exit_recovery.py \
  test_candle_aligned_scheduler.py

echo
echo "[2/4] FORCE_EXIT integration"
python3 test_force_exit_integration.py

echo
echo "[3/4] FORCE_EXIT restart recovery"
python3 test_force_exit_recovery.py

echo
echo "[4/4] Stage 4 regression baseline"
bash run_regression_stage4.sh

echo
echo "=================================================="
echo "STAGE 5 REGRESSION PASSED"
echo "=================================================="
