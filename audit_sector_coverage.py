#!/usr/bin/env python3
"""Read-only sector-map coverage and market-alignment diagnostics audit.

The script reads an existing bot checkout/runtime directory and writes only to
stdout.  It never imports config.py, changes user_config.json, toggles the
market-alignment filter, or interacts with systemd/the broker.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping


ALLOWED_BY_HARD_FILTER = {"ALIGNED", "STRONG_ALIGNMENT"}
KNOWN_REASON_CODES = {
    "OK",
    "UNMAPPED",
    "MISSING_TOKEN",
    "EMPTY_DATA",
    "FETCH_ERROR",
    "INDICATOR_ERROR",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit current watchlist sector coverage and replay stored market "
            "alignment decisions without changing the bot."
        )
    )
    parser.add_argument(
        "--bot-root",
        type=Path,
        default=Path.cwd(),
        help="Production bot directory containing user_config.json and logs.",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Checkout containing market_trend.py (defaults to this script's checkout).",
    )
    parser.add_argument(
        "--session-date",
        default=date.today().isoformat(),
        help="Session date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of the text report.",
    )
    return parser.parse_args(argv)


def load_literal_assignments(
    source_path: Path,
    names: Iterable[str],
) -> dict[str, Any]:
    """Load literal constants without importing or executing project code."""
    wanted = set(names)
    tree = ast.parse(source_path.read_text(encoding="utf-8"), str(source_path))
    found: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value_node = node.value
        for target in targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                found[target.id] = ast.literal_eval(value_node)
    missing = wanted.difference(found)
    if missing:
        raise ValueError(
            f"Missing literal assignment(s) in {source_path}: {sorted(missing)}"
        )
    return found


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
            if isinstance(row, dict):
                rows.append(row)
    return rows


def normalize_watchlist(raw_watchlist: Any) -> list[dict[str, str]]:
    if not isinstance(raw_watchlist, list):
        raise ValueError("user_config.json watchlist must be a list")
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_watchlist:
        if isinstance(item, str):
            symbol = item.strip().upper()
            exchange = "NSE"
        elif isinstance(item, dict):
            symbol = str(
                item.get("symbol")
                or item.get("tradingsymbol")
                or item.get("name")
                or ""
            ).strip().upper()
            exchange = str(item.get("exchange") or "NSE").strip().upper()
        else:
            continue
        if not symbol:
            continue
        key = (exchange, symbol)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"exchange": exchange, "symbol": symbol})
    return normalized


def watchlist_coverage(
    watchlist: list[dict[str, str]],
    sector_map: Mapping[str, str],
    sector_tokens: Mapping[str, Any],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    by_sector: dict[str, list[str]] = defaultdict(list)
    for item in watchlist:
        symbol = item["symbol"]
        sector = sector_map.get(symbol)
        if sector is None:
            status = "UNMAPPED"
            group = "UNMAPPED"
            has_token = False
        elif sector not in sector_tokens:
            status = "MISSING_TOKEN"
            group = sector
            has_token = False
        else:
            status = "READY"
            group = sector
            has_token = True
        by_sector[group].append(symbol)
        rows.append(
            {
                "exchange": item["exchange"],
                "symbol": symbol,
                "sector": sector,
                "sector_token_present": has_token,
                "coverage_status": status,
            }
        )

    total = len(rows)
    mapped = sum(row["sector"] is not None for row in rows)
    token_ready = sum(row["coverage_status"] == "READY" for row in rows)
    missing_token = sum(row["coverage_status"] == "MISSING_TOKEN" for row in rows)
    return {
        "watchlist_total": total,
        "mapped": mapped,
        "unmapped": total - mapped,
        "mapping_coverage_pct": round(100.0 * mapped / total, 2) if total else 0.0,
        "token_ready": token_ready,
        "token_ready_pct": round(100.0 * token_ready / total, 2) if total else 0.0,
        "missing_token": missing_token,
        "rows": rows,
        "by_sector": {
            sector: sorted(symbols)
            for sector, symbols in sorted(by_sector.items())
        },
    }


def value_from_record(record: Mapping[str, Any], field: str) -> Any:
    """Read a diagnostic field from supported flat/nested analytics shapes."""
    if record.get(field) is not None:
        return record.get(field)
    for container_name in ("signal", "signal_snapshot", "analytics", "entry_analytics"):
        container = record.get(container_name)
        if isinstance(container, Mapping) and container.get(field) is not None:
            return container.get(field)
    return None


def record_session_date(record: Mapping[str, Any]) -> str | None:
    for key in (
        "session_date",
        "trade_date",
        "date",
        "timestamp",
        "recorded_at",
        "exit_time",
        "entry_time",
    ):
        value = record.get(key)
        if value is None:
            continue
        text = str(value)
        if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
            return text[:10]
    return None


def filter_session(rows: Iterable[dict[str, Any]], session_date: str) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        row_date = record_session_date(row)
        if row_date is None or row_date == session_date:
            result.append(row)
    return result


def validation_candidates(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for event in rows:
        if event.get("event_type") != "candidate_collected":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            candidates.append(payload)
    return candidates


def diagnostic_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    market_reasons: Counter[str] = Counter()
    sector_reasons: Counter[str] = Counter()
    alignments: Counter[str] = Counter()
    affected_symbols: set[str] = set()
    diagnostics_present = 0

    for record in records:
        market_reason = value_from_record(record, "market_trend_reason")
        sector_reason = value_from_record(record, "sector_trend_reason")
        alignment = value_from_record(record, "market_alignment")
        market_label = str(market_reason) if market_reason is not None else "NOT_RECORDED"
        sector_label = str(sector_reason) if sector_reason is not None else "NOT_RECORDED"
        alignment_label = str(alignment) if alignment is not None else "NOT_RECORDED"
        market_reasons[market_label] += 1
        sector_reasons[sector_label] += 1
        alignments[alignment_label] += 1
        if market_reason is not None or sector_reason is not None:
            diagnostics_present += 1
        if (
            market_label not in {"OK", "NOT_RECORDED"}
            or sector_label not in {"OK", "NOT_RECORDED"}
            or alignment_label == "UNKNOWN"
        ):
            symbol = value_from_record(record, "symbol") or record.get("tradingsymbol")
            if symbol:
                affected_symbols.add(str(symbol))

    return {
        "records": len(records),
        "records_with_diagnostics": diagnostics_present,
        "market_trend_reasons": dict(sorted(market_reasons.items())),
        "sector_trend_reasons": dict(sorted(sector_reasons.items())),
        "market_alignments": dict(sorted(alignments.items())),
        "affected_symbols": sorted(affected_symbols),
    }


def replay_hard_filter(records: list[dict[str, Any]]) -> dict[str, Any]:
    allowed = 0
    blocked = 0
    unreplayable = 0
    by_alignment: Counter[str] = Counter()
    blocked_symbols: set[str] = set()
    for record in records:
        alignment = value_from_record(record, "market_alignment")
        if alignment is None:
            unreplayable += 1
            by_alignment["NOT_RECORDED"] += 1
            continue
        label = str(alignment)
        by_alignment[label] += 1
        if label in ALLOWED_BY_HARD_FILTER:
            allowed += 1
        else:
            blocked += 1
            symbol = value_from_record(record, "symbol") or record.get("tradingsymbol")
            if symbol:
                blocked_symbols.add(str(symbol))
    replayable = allowed + blocked
    return {
        "allowed": allowed,
        "blocked": blocked,
        "unreplayable": unreplayable,
        "blocked_pct_of_replayable": (
            round(100.0 * blocked / replayable, 2) if replayable else None
        ),
        "by_alignment": dict(sorted(by_alignment.items())),
        "blocked_symbols": sorted(blocked_symbols),
        "rule": "ALLOW only ALIGNED or STRONG_ALIGNMENT",
    }


def file_fingerprint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def snapshot(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    return {str(path): file_fingerprint(path) for path in paths}


def candidate_source(
    validation_rows: list[dict[str, Any]],
    signal_rows: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    candidates = validation_candidates(validation_rows)
    if candidates:
        return "validation_events:candidate_collected", candidates
    return "signal_logs:fallback", signal_rows


def build_report(
    bot_root: Path,
    source_root: Path,
    session_date: str,
) -> dict[str, Any]:
    # Validate the date early; the parsed value is intentionally not otherwise used.
    date.fromisoformat(session_date)
    bot_root = bot_root.resolve()
    source_root = source_root.resolve()
    market_trend_path = source_root / "market_trend.py"
    config_path = bot_root / "user_config.json"
    trade_path = bot_root / "trade_history.jsonl"
    signal_path = bot_root / "signal_logs" / f"signals_{session_date}.jsonl"
    validation_path = bot_root / "validation_events" / f"{session_date}.jsonl"
    input_paths = [
        market_trend_path,
        config_path,
        trade_path,
        signal_path,
        validation_path,
    ]
    before = snapshot(input_paths)

    constants = load_literal_assignments(
        market_trend_path,
        ("SECTOR_MAP", "SECTOR_INDEX_TOKENS"),
    )
    user_config = read_json(config_path, {})
    raw_watchlist = user_config.get("watchlist", user_config.get("WATCHLIST"))
    if raw_watchlist is None:
        raise ValueError(f"No watchlist found in {config_path}")
    watchlist = normalize_watchlist(raw_watchlist)
    coverage = watchlist_coverage(
        watchlist,
        constants["SECTOR_MAP"],
        constants["SECTOR_INDEX_TOKENS"],
    )

    trades = filter_session(read_jsonl(trade_path), session_date)
    signals = filter_session(read_jsonl(signal_path), session_date)
    validation_events = filter_session(read_jsonl(validation_path), session_date)
    source_name, candidates = candidate_source(validation_events, signals)

    after = snapshot(input_paths)
    integrity_passed = before == after
    report = {
        "audit": "sector_coverage_and_alignment_diagnostics",
        "session_date": session_date,
        "bot_root": str(bot_root),
        "source_root": str(source_root),
        "read_only_integrity": "PASS" if integrity_passed else "FAIL",
        "inputs": {
            "user_config": str(config_path),
            "market_trend_source": str(market_trend_path),
            "trade_history": str(trade_path),
            "signal_log": str(signal_path),
            "validation_events": str(validation_path),
        },
        "coverage": coverage,
        "candidate_source": source_name,
        "candidate_diagnostics": diagnostic_summary(candidates),
        "closed_trade_diagnostics": diagnostic_summary(trades),
        "hard_filter_replay": replay_hard_filter(candidates),
        "limitations": [
            (
                "UNMAPPED symbols cannot be grouped into their true economic sectors "
                "from local bot data; no external sector classification was inferred."
            ),
            (
                "NOT_RECORDED means the stored row predates diagnostic persistence or "
                "did not carry the field; it is not treated as OK or as an error."
            ),
            (
                "Hard-filter replay measures candidate-count impact only; it does not "
                "estimate counterfactual fills, P&L, or ranking changes."
            ),
        ],
    }
    if not integrity_passed:
        raise RuntimeError("An audited input changed while it was being read")
    return report


def format_counter(values: Mapping[str, int]) -> str:
    if not values:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in values.items())


def format_text(report: Mapping[str, Any]) -> str:
    coverage = report["coverage"]
    candidates = report["candidate_diagnostics"]
    trades = report["closed_trade_diagnostics"]
    replay = report["hard_filter_replay"]
    lines = [
        "===== SECTOR COVERAGE AUDIT =====",
        f"SESSION_DATE={report['session_date']}",
        f"BOT_ROOT={report['bot_root']}",
        f"SOURCE_ROOT={report['source_root']}",
        f"READ_ONLY_INTEGRITY={report['read_only_integrity']}",
        "",
        "===== WATCHLIST COVERAGE =====",
        f"WATCHLIST_TOTAL={coverage['watchlist_total']}",
        f"MAPPED={coverage['mapped']}",
        f"UNMAPPED={coverage['unmapped']}",
        f"MAPPING_COVERAGE_PCT={coverage['mapping_coverage_pct']:.2f}",
        f"TOKEN_READY={coverage['token_ready']}",
        f"TOKEN_READY_PCT={coverage['token_ready_pct']:.2f}",
        f"MISSING_TOKEN={coverage['missing_token']}",
        "",
        "===== SYMBOL COVERAGE =====",
    ]
    for row in coverage["rows"]:
        sector = row["sector"] or "UNMAPPED"
        lines.append(
            f"{row['exchange']}:{row['symbol']} | sector={sector} | "
            f"status={row['coverage_status']}"
        )
    lines.extend(["", "===== COVERAGE GROUPED BY MAPPED SECTOR ====="])
    for sector, symbols in coverage["by_sector"].items():
        lines.append(f"{sector}: {len(symbols)} | {', '.join(symbols)}")

    lines.extend(
        [
            "",
            "===== RANKED CANDIDATE DIAGNOSTICS =====",
            f"SOURCE={report['candidate_source']}",
            f"RECORDS={candidates['records']}",
            f"RECORDS_WITH_DIAGNOSTICS={candidates['records_with_diagnostics']}",
            "MARKET_TREND_REASONS=" + format_counter(candidates["market_trend_reasons"]),
            "SECTOR_TREND_REASONS=" + format_counter(candidates["sector_trend_reasons"]),
            "MARKET_ALIGNMENTS=" + format_counter(candidates["market_alignments"]),
            "AFFECTED_SYMBOLS=" + (", ".join(candidates["affected_symbols"]) or "none"),
            "",
            "===== CLOSED TRADE DIAGNOSTICS =====",
            f"RECORDS={trades['records']}",
            f"RECORDS_WITH_DIAGNOSTICS={trades['records_with_diagnostics']}",
            "MARKET_TREND_REASONS=" + format_counter(trades["market_trend_reasons"]),
            "SECTOR_TREND_REASONS=" + format_counter(trades["sector_trend_reasons"]),
            "MARKET_ALIGNMENTS=" + format_counter(trades["market_alignments"]),
            "AFFECTED_SYMBOLS=" + (", ".join(trades["affected_symbols"]) or "none"),
            "",
            "===== OFFLINE HARD-FILTER REPLAY =====",
            f"RULE={replay['rule']}",
            f"WOULD_ALLOW={replay['allowed']}",
            f"WOULD_BLOCK={replay['blocked']}",
            f"UNREPLAYABLE={replay['unreplayable']}",
            "BLOCKED_PCT_OF_REPLAYABLE="
            + (
                f"{replay['blocked_pct_of_replayable']:.2f}"
                if replay["blocked_pct_of_replayable"] is not None
                else "N/A"
            ),
            "ALIGNMENT_COUNTS=" + format_counter(replay["by_alignment"]),
            "WOULD_BLOCK_SYMBOLS=" + (", ".join(replay["blocked_symbols"]) or "none"),
            "",
            "===== LIMITATIONS =====",
        ]
    )
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.extend(
        [
            "",
            "NO_SOURCE_CHANGES=True",
            "NO_CONFIG_CHANGES=True",
            "NO_FILTER_TOGGLE=True",
            "NO_SERVICE_ACTION=True",
            "SECTOR_COVERAGE_AUDIT=PASS",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = build_report(args.bot_root, args.source_root, args.session_date)
    except Exception as exc:
        print(f"SECTOR_COVERAGE_AUDIT=FAIL: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
