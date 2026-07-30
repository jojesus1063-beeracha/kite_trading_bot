"""
End-of-day report: per-trade win/loss reasons + filter effectiveness.
Pure analytics -- reads trade_history.jsonl and signal_logs/, never
alters trading behavior. Gracefully shows "N/A" for anything not yet
tracked (MFE/MAE, counterfactuals) rather than fabricating numbers.
"""
import json
import os
from collections import defaultdict


def load_trades(iso_date):
    trades = []
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_history.jsonl")
    if not os.path.exists(path):
        return trades
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                t = json.loads(line)
                if t.get("date") == iso_date:
                    trades.append(t)
    return trades


def load_signals(iso_date):
    signals = []
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_logs", f"signals_{iso_date}.jsonl")
    if not os.path.exists(path):
        return signals
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                signals.append(json.loads(line))
    return signals


def match_trade_to_signal(trade, signals):
    """Best-effort match by symbol + entry price -- signal logging predates
    a fully reliable join key, so this is approximate, not guaranteed exact.
    Defensively skips any signal whose entry_price isn'''t numeric (a real
    stale-data artifact existed briefly before the json_safe fix, where a
    numpy value got stringified instead of converted -- treat as no match
    rather than crash)."""
    candidates = []
    for s in signals:
        if s["symbol"] != trade["symbol"]:
            continue
        ep = s.get("entry_price", -1e9)
        if not isinstance(ep, (int, float)):
            continue
        if abs(ep - trade["entry"]) < 0.5:
            candidates.append(s)
    return candidates[0] if candidates else None


def build_trade_reasons(iso_date):
    """Per-trade: outcome + every known contributing factor, or 'N/A (pre-fix)'
    for trades that predate a given field's introduction."""
    trades = load_trades(iso_date)
    signals = load_signals(iso_date)
    rows = []
    for t in trades:
        sig = match_trade_to_signal(t, signals)
        rows.append({
            "time": t["time"], "symbol": t["symbol"], "direction": t["direction"],
            "result": t["result"], "gross_pnl": t.get("gross_pnl", t["pnl"]),
            "costs": t.get("costs", 0.0), "net_pnl": t["pnl"],
            "technical_confidence": sig.get("technical_confidence") if sig else "N/A (no matched signal)",
            "market_alignment": sig.get("market_alignment") if sig else "N/A",
            "news_sentiment": sig.get("news_sentiment", "N/A (pre-fix)") if sig else "N/A",
            "price_action_score": sig.get("price_action_score", "N/A (pre-fix)") if sig else "N/A",
            "raw_adx": sig.get("raw_adx", "N/A (pre-fix)") if sig else "N/A",
        })
    return rows


def _group_stats(rows, key_func, min_group_size=1):
    """Groups trade rows by key_func(row), returns win-rate/avg-pnl per group.
    Groups with fewer than min_group_size trades are still shown but flagged
    as low-sample, per the 'don't trust small samples' discipline established
    this week."""
    groups = defaultdict(list)
    for r in rows:
        groups[key_func(r)].append(r["net_pnl"])
    result = {}
    for key, pnls in groups.items():
        wins = [p for p in pnls if p > 0]
        result[key] = {
            "count": len(pnls), "wins": len(wins),
            "win_rate_pct": (len(wins) / len(pnls) * 100) if pnls else None,
            "total_pnl": sum(pnls), "avg_pnl": sum(pnls) / len(pnls) if pnls else None,
            "low_sample": len(pnls) < min_group_size,
        }
    return result


def build_filter_effectiveness(iso_date, min_group_size=5):
    rows = build_trade_reasons(iso_date)
    if not rows:
        return {"trade_count": 0, "reason": "no trades today"}

    by_confidence = _group_stats(rows, lambda r: r["technical_confidence"], min_group_size)
    by_alignment = _group_stats(rows, lambda r: r["market_alignment"], min_group_size)
    by_news = _group_stats([r for r in rows if r["news_sentiment"] not in ("N/A", "N/A (pre-fix)")],
                            lambda r: r["news_sentiment"], min_group_size)
    by_pa_sign = _group_stats(
        [r for r in rows if isinstance(r["price_action_score"], (int, float))],
        lambda r: "positive" if r["price_action_score"] > 0 else ("negative" if r["price_action_score"] < 0 else "zero"),
        min_group_size)

    return {
        "trade_count": len(rows),
        "by_confidence": by_confidence,
        "by_market_alignment": by_alignment,
        "by_news_sentiment": by_news,
        "by_price_action_sign": by_pa_sign,
    }


def format_report(iso_date):
    rows = build_trade_reasons(iso_date)
    effectiveness = build_filter_effectiveness(iso_date)

    lines = []
    lines.append(f"=== Daily Report: {iso_date} ===")
    lines.append("")
    lines.append(f"Total trades: {len(rows)}")
    lines.append("")

    lines.append("--- Per-Trade Breakdown ---")
    for r in rows:
        outcome = "WIN" if r["net_pnl"] > 0 else "LOSS"
        lines.append(f"{r['time']} {r['symbol']} {r['direction']} -> {outcome} "
                     f"(net Rs{r['net_pnl']:+.2f}, gross Rs{r['gross_pnl']:+.2f}, costs Rs{r['costs']:.2f}) "
                     f"| result={r['result']}")
        lines.append(f"    confidence={r['technical_confidence']} | alignment={r['market_alignment']} | "
                     f"news={r['news_sentiment']} | price_action={r['price_action_score']} | ADX={r['raw_adx']}")

    lines.append("")
    lines.append("--- Filter Effectiveness ---")
    if effectiveness.get("trade_count", 0) == 0:
        lines.append("No trades today -- nothing to analyze yet.")
    else:
        for label, data in [
            ("By Technical Confidence", effectiveness["by_confidence"]),
            ("By Market Alignment", effectiveness["by_market_alignment"]),
            ("By News Sentiment", effectiveness["by_news_sentiment"]),
            ("By Price Action Score Sign", effectiveness["by_price_action_sign"]),
        ]:
            lines.append(f"\n{label}:")
            if not data:
                lines.append("  (no data -- feature not active for any of today's trades)")
            for key, stats in data.items():
                sample_flag = " [LOW SAMPLE -- do not trust]" if stats["low_sample"] else ""
                lines.append(f"  {key}: {stats['count']} trades, {stats['win_rate_pct']:.0f}% win rate, "
                             f"total Rs{stats['total_pnl']:+.2f}, avg Rs{stats['avg_pnl']:+.2f}{sample_flag}")

    lines.append("")
    lines.append("--- Known Gaps (not yet tracked) ---")
    lines.append("MFE/MAE: N/A (not yet implemented)")
    lines.append("Counterfactual outcomes for rejected signals: N/A (not yet implemented)")

    return "\n".join(lines)
