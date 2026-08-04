#!/usr/bin/env python3
"""Self-contained regression tests for audit_sector_coverage.py."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from audit_sector_coverage import (
    build_report,
    diagnostic_summary,
    format_text,
    replay_hard_filter,
    watchlist_coverage,
)


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print("PASS:", name)


def digest_tree(root: Path):
    result = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


coverage = watchlist_coverage(
    [
        {"exchange": "NSE", "symbol": "BANK1"},
        {"exchange": "NSE", "symbol": "IT1"},
        {"exchange": "NSE", "symbol": "UNKNOWN1"},
    ],
    {"BANK1": "NIFTY BANK", "IT1": "NIFTY IT"},
    {"NIFTY BANK": 123},
)
check("Mapped symbols are counted", coverage["mapped"] == 2)
check("Unmapped symbols are counted", coverage["unmapped"] == 1)
check("Mapped sectors without tokens are visible", coverage["missing_token"] == 1)
check("Only fully configured mappings are token-ready", coverage["token_ready"] == 1)
check("Coverage percentage is exact", coverage["mapping_coverage_pct"] == 66.67)

records = [
    {
        "symbol": "BANK1",
        "market_alignment": "STRONG_ALIGNMENT",
        "market_trend_reason": "OK",
        "sector_trend_reason": "OK",
    },
    {
        "symbol": "UNKNOWN1",
        "signal": {
            "market_alignment": "UNKNOWN",
            "market_trend_reason": "OK",
            "sector_trend_reason": "UNMAPPED",
        },
    },
    {"symbol": "OLDROW"},
]
summary = diagnostic_summary(records)
check("Nested diagnostic fields are read", summary["sector_trend_reasons"]["UNMAPPED"] == 1)
check("Missing historic fields are not invented", summary["sector_trend_reasons"]["NOT_RECORDED"] == 1)
replay = replay_hard_filter(records)
check("Aligned candidates would remain allowed", replay["allowed"] == 1)
check("Unknown alignment would be blocked", replay["blocked"] == 1)
check("Missing alignment stays unreplayable", replay["unreplayable"] == 1)

with TemporaryDirectory() as directory:
    root = Path(directory)
    source_root = root / "source"
    bot_root = root / "bot"
    source_root.mkdir()
    (bot_root / "signal_logs").mkdir(parents=True)
    (bot_root / "validation_events").mkdir()

    (source_root / "market_trend.py").write_text(
        "SECTOR_MAP = {'BANK1': 'NIFTY BANK', 'IT1': 'NIFTY IT'}\n"
        "SECTOR_INDEX_TOKENS = {'NIFTY BANK': 123}\n",
        encoding="utf-8",
    )
    (bot_root / "user_config.json").write_text(
        json.dumps(
            {
                "watchlist": [
                    "BANK1",
                    {"symbol": "IT1", "exchange": "NSE"},
                    "UNKNOWN1",
                    "BANK1",
                ]
            }
        ),
        encoding="utf-8",
    )
    (bot_root / "trade_history.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-08-04T12:00:00+05:30",
                "symbol": "BANK1",
                "market_alignment": "ALIGNED",
                "market_trend_reason": "OK",
                "sector_trend_reason": "OK",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (bot_root / "signal_logs" / "signals_2026-08-04.jsonl").write_text(
        json.dumps({"symbol": "FALLBACK", "market_alignment": "MISALIGNED"}) + "\n",
        encoding="utf-8",
    )
    (bot_root / "validation_events" / "2026-08-04.jsonl").write_text(
        json.dumps(
            {
                "event_type": "candidate_collected",
                "session_date": "2026-08-04",
                "payload": {
                    "symbol": "BANK1",
                    "market_alignment": "STRONG_ALIGNMENT",
                    "market_trend_reason": "OK",
                    "sector_trend_reason": "OK",
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "event_type": "candidate_collected",
                "session_date": "2026-08-04",
                "payload": {
                    "symbol": "UNKNOWN1",
                    "market_alignment": "UNKNOWN",
                    "market_trend_reason": "OK",
                    "sector_trend_reason": "UNMAPPED",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    before = digest_tree(root)
    report = build_report(bot_root, source_root, "2026-08-04")
    after = digest_tree(root)

    check("Audit reads the configured watchlist and deduplicates it", report["coverage"]["watchlist_total"] == 3)
    check("Validation candidates take precedence over signal fallback", report["candidate_source"].startswith("validation_events"))
    check("Hard-filter replay uses candidate analytics", report["hard_filter_replay"]["blocked"] == 1)
    check("Closed-trade diagnostics are read separately", report["closed_trade_diagnostics"]["records"] == 1)
    check("Every audited input remains byte-for-byte unchanged", before == after)
    check("Internal read-only integrity check passes", report["read_only_integrity"] == "PASS")
    rendered = format_text(report)
    check("Text report declares no config change", "NO_CONFIG_CHANGES=True" in rendered)
    check("Text report declares no service action", "NO_SERVICE_ACTION=True" in rendered)

print("SECTOR COVERAGE AUDIT TESTS PASSED")
