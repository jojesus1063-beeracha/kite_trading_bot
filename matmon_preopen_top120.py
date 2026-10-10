#!/usr/bin/env python3

from __future__ import annotations

import json
import math
import shutil
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from auth import get_kite_client
from equity_universe import (
    cleaned_equity_instruments,
    fetch_selector_quotes,
)
import matmon_strategy_config as strategy_def
import matmon_watchlist_scoring as scoring
from matmon_value_saturation_shadow import add_preopen_value_shadow

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "user_config.json"
RUNTIME = ROOT / "runtime" / "matmon" / "preopen"
# STRATEGY definition -- single source of truth (was a bare literal here
# before centralization; the patch that fixed this repo-wide didn't apply
# cleanly to this file since it had already been hand-edited to 60).
TOP_N = strategy_def.MATMON_WATCHLIST_SIZE

# Broad safety/liquidity constraints only.
MIN_PRICE = 50.0
MAX_PRICE = 5000.0
MAX_SPREAD_PCT = 0.25
MIN_DEPTH_VALUE = 100_000.0


def num(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def depth_side_value(levels):
    total = 0.0
    for x in levels or []:
        if not isinstance(x, dict):
            continue
        total += num(x.get("price")) * num(x.get("quantity"))
    return total


def evaluate(row, quote):
    """Hard eligibility only -- delegates to matmon_watchlist_scoring so
    eligibility logic has one source of truth shared with the offline
    replay/comparison tooling. Identical pass/fail behavior to before this
    delegation; the returned dict gains a few extra observational fields
    (signed imbalance, suspicious-quote flag, CAS passthrough) that no
    ranking here depends on.
    """
    candidate, _reason = scoring.hard_eligibility(
        row,
        quote,
        min_price=MIN_PRICE,
        max_price=MAX_PRICE,
        max_spread_pct=MAX_SPREAD_PCT,
        min_depth_value=MIN_DEPTH_VALUE,
    )
    return candidate


def main():
    RUNTIME.mkdir(parents=True, exist_ok=True)

    cfg = json.loads(CONFIG.read_text())

    paper = cfg.get("paper_trading") is True
    mode = "PAPER" if paper else "LIVE"
    print(f"MATMON_PREOPEN_MODE={mode}")
    # Watchlist selection only — no orders. Allowed in both PAPER and LIVE.

    kite = get_kite_client()

    rows, cleaning = cleaned_equity_instruments(kite)

    if len(rows) < TOP_N:
        raise SystemExit(
            f"ABORT: cleaned universe only {len(rows)}"
        )

    keys = [
        f'{x["exchange"]}:{x["symbol"]}'
        for x in rows
    ]

    quotes = fetch_selector_quotes(kite, keys)

    candidates = []

    for row in rows:
        key = f'{row["exchange"]}:{row["symbol"]}'
        candidate = evaluate(row, quotes.get(key))

        if candidate is not None:
            candidates.append(candidate)

    if len(candidates) < TOP_N:
        raise SystemExit(
            f"ABORT: only {len(candidates)} valid pre-open candidates"
        )

    # PRE-OPEN DISCOVERY ONLY. Direction is deliberately NOT used to select
    # or gate entries here -- BUY/SELL remains exclusively Matmon's
    # EMA3/15 -> DI14 -> 3s CLEAN quote confirmation.
    #
    # Model A (current production formula) drives the actual selection
    # below -- unchanged behavior. Model B is computed alongside it purely
    # for research/observational persistence; nothing here selects on it.
    scoring.score_model_a(candidates)
    scoring.score_model_b(candidates)
    # Observation only: adds fields but never changes model_a_score or sorting.
    add_preopen_value_shadow(candidates)
    for c in candidates:
        c["preopen_score"] = c["model_a_score"]

    # Deduplicate the same symbol if available on both exchanges.
    candidates.sort(
        key=lambda x: (
            -x["preopen_score"],
            -x["depth_value"],
            x["spread_pct"],
            x["symbol"],
        )
    )

    selected = []
    seen = set()

    for c in candidates:
        if c["symbol"] in seen:
            continue

        seen.add(c["symbol"])
        selected.append(c)

        if len(selected) == TOP_N:
            break

    if len(selected) != TOP_N:
        raise SystemExit(
            f"ABORT: unique selected count={len(selected)}"
        )

    watchlist = [
        {
            "symbol": x["symbol"],
            "exchange": x["exchange"],
        }
        for x in selected
    ]

    if len({x["symbol"] for x in watchlist}) != TOP_N:
        raise SystemExit("ABORT: duplicate symbols")

    # Auto-approve: write Top-N into user_config.json and keep a report.
    now_ist = datetime.now(IST)
    stamp = now_ist.strftime("%Y%m%d_%H%M%S")
    selection_date = now_ist.date().isoformat()

    # Preserve previous config
    backup_dir = RUNTIME / "config_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"user_config_before_preopen_{stamp}.json"
    shutil.copy2(CONFIG, backup)

    updated = dict(cfg)
    updated["watchlist"] = watchlist
    updated["entry_scan_shortlist_size"] = TOP_N
    # Do not force paper_trading here — leave LIVE/PAPER as already configured

    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(updated, indent=2) + "\n")
    tmp.replace(CONFIG)

    check = json.loads(CONFIG.read_text())
    if check.get("watchlist") != watchlist:
        raise RuntimeError("POST-WRITE WATCHLIST CHECK FAILED")

    report = {
        "generated_at": now_ist.isoformat(),
        "selection_date": selection_date,
        "status": "AUTO_APPROVED",
        "target_count": TOP_N,
        "config_backup": str(backup),
        "strategy": f"MATMON_PREOPEN_TOP{TOP_N}",
        "cleaning": cleaning,
        "valid_candidates": len(candidates),
        "selected_count": len(selected),
        "watchlist": watchlist,
        "selected_details": [
            {
                k: v
                for k, v in x.items()
                if not k.startswith("_")
            }
            for x in selected
        ],
        "value_shadow_note": (
            "rupee_turnover_shadow/percentile and low_value_shadow are "
            "OBSERVATION-ONLY; production ranking remains model_a_score"
        ),
        "model_b_note": (
            "model_b_score/component scores are RESEARCH-ONLY and do not "
            "affect selection above -- see matmon_watchlist_scoring.py"
        ),
        "full_ranked_universe_file": f"{stamp}_full_ranked.json",
    }

    report_path = RUNTIME / f"top{TOP_N}_{stamp}.json"

    report_path.write_text(
        json.dumps(report, indent=2) + "\n"
    )

    (RUNTIME / "latest.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )

    candidate_path = RUNTIME / f"pending_top{TOP_N}_candidate.json"
    candidate_path.write_text(
        json.dumps(report, indent=2) + "\n"
    )

    # Research-only: the COMPLETE eligible ranked universe (not just the
    # operational Top-N), with both Model A and Model B scores, for later
    # counterfactual analysis (Would Top-30 have been better? Top-80?)
    # without needing to change the live trading universe to find out.
    full_ranked = {
        "generated_at": report["generated_at"],
        "note": (
            "Full eligible universe, ranked by model_a_score (current "
            "production ordering) with model_b_score/components attached "
            "for offline comparison. Not the operational watchlist."
        ),
        "candidates": [
            {k: v for k, v in x.items() if not k.startswith("_")}
            for x in candidates
        ],
    }
    full_ranked_path = RUNTIME / f"{stamp}_full_ranked.json"
    full_ranked_path.write_text(json.dumps(full_ranked, indent=2) + "\n")


    print(f"MATMON PREOPEN TOP-{TOP_N}")
    print("Clean universe :", len(rows))
    print("Valid candidates:", len(candidates))
    print("Selected       :", len(selected))
    print("Unique         :", len(seen))
    print("Candidate      :", candidate_path)
    print("Report         :", report_path)

    print()
    print("TOP 20")

    for i, x in enumerate(selected[:20], 1):
        print(
            f'{i:3d}. '
            f'{x["exchange"]}:{x["symbol"]:<16} '
            f'score={x["preopen_score"]:6.2f} '
            f'spread={x["spread_pct"]:.3f}% '
            f'depth={x["depth_value"]:,.0f} '
            f'imb={x["imbalance"]:.3f}'
        )

    print()
    print(f"MATMON_PREOPEN_TOP{TOP_N}=PASS")
    print("AUTO_APPROVED=TRUE")
    print("WATCHLIST_INSTALLED=TRUE")
    print("NO_ORDERS_PLACED")


if __name__ == "__main__":
    main()
