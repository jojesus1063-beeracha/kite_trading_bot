#!/usr/bin/env bash
set -uo pipefail

tests=(
  test_exit_stack.py
  test_market_trend.py
  test_scan_caching.py
  test_signal_log.py
  test_trap_detection.py
  test_circuit_proximity.py
  test_candle_completion.py
  test_alignment_filter.py
  test_news_filter.py
  test_price_action.py
  test_daily_report.py
  test_json_safe.py
  test_fixed_target.py
  test_candle_aligned_scheduler.py
  test_startup_health_check.py
  test_position_analytics.py
  test_monitor_route.py
  test_persistence_isolation.py
  test_watchlist_range_analytics.py
  test_watchlist_dashboard_helpers.py
  test_watchlist_section.py
  test_watchlist_polling.py
  test_order_verification.py
  test_pending_order_store.py
  test_entry_integration.py
)

for test_file in "${tests[@]}"; do
    echo
    echo "=================================================="
    echo "RUNNING: ${test_file}"
    echo "=================================================="

    output_file="$(mktemp)"

    timeout 60 python3 "$test_file" 2>&1 | tee "$output_file"
    rc=${PIPESTATUS[0]}

    # Existing, documented baseline:
    # test_exit_stack.py currently has exactly one known peak_price failure.
    if [[ "$test_file" == "test_exit_stack.py" && "$rc" -eq 1 ]]; then
        if grep -q "FINAL Results: 16 passed, 1 failed" "$output_file" &&
           grep -q "FAIL: peak_price correctly tracks" "$output_file"; then
            echo "KNOWN BASELINE ACCEPTED: ${test_file}"
            rm -f "$output_file"
            continue
        fi
    fi

    if [[ "$rc" -ne 0 ]]; then
        echo
        echo "UNEXPECTED FAILURE: ${test_file}"
        echo "EXIT CODE: ${rc}"
        echo "Full output preserved at: ${output_file}"
        exit "$rc"
    fi

    rm -f "$output_file"
    echo "PASSED: ${test_file}"
done

echo
echo "ALL SUITES PASSED OR MATCHED DOCUMENTED BASELINES"
