"""
PREMIUM_ROTATION_SHADOW -- performance report (section 19).

All headline metrics are computed on NET P&L (post estimated costs),
per the spec's explicit instruction not to judge on gross or win rate
alone. Handles zero-trade and zero-loss edge cases explicitly rather
than raising or dividing by zero.
"""
from dataclasses import dataclass
from typing import List

from fno_bot.strategies.premium_rotation_session import ClosedTrade
from fno_bot.strategies.premium_rotation_costs_log import net_pnl_for_closed_trade


@dataclass
class PerformanceReport:
    total_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    gross_pnl: float
    total_estimated_costs: float
    net_pnl: float
    profit_factor: float   # None-safe: reported as float('inf') if there are wins and zero losses, 0.0 if no wins
    average_win: float
    average_loss: float
    expectancy_per_trade: float
    max_drawdown: float   # in net-P&L terms, running peak-to-trough
    average_holding_seconds: float
    disclaimer: str


def build_performance_report(trades: List[ClosedTrade]) -> PerformanceReport:
    disclaimer = "Net P&L figures use ESTIMATED, unverified charge rates -- see individual trade cost_rates_used."

    if not trades:
        return PerformanceReport(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, disclaimer)

    net_pnls = []
    gross_total = 0.0
    cost_total = 0.0
    holding_times = []

    for t in trades:
        pnl = net_pnl_for_closed_trade(t)
        net_pnls.append(pnl["net_pnl_estimate"])
        gross_total += pnl["gross_pnl"]
        cost_total += pnl["estimated_costs"]
        holding_times.append(t.exit_time - t.entry_time)

    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p <= 0]
    total = len(trades)
    win_rate = len(wins) / total * 100 if total > 0 else 0.0

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0

    gross_win_sum = sum(wins)
    gross_loss_sum = abs(sum(losses))
    if gross_loss_sum > 0:
        profit_factor = gross_win_sum / gross_loss_sum
    elif gross_win_sum > 0:
        profit_factor = float("inf")   # all wins, no losses -- explicit, not a fabricated finite number
    else:
        profit_factor = 0.0

    win_rate_frac = len(wins) / total
    loss_rate_frac = len(losses) / total
    expectancy = (win_rate_frac * avg_win) - (loss_rate_frac * avg_loss)

    # running peak-to-trough on cumulative net P&L, in trade sequence order
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in net_pnls:
        cumulative += p
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)

    net_total = gross_total - cost_total
    avg_holding = sum(holding_times) / len(holding_times) if holding_times else 0.0

    return PerformanceReport(
        total_trades=total, wins=len(wins), losses=len(losses), win_rate_pct=round(win_rate, 2),
        gross_pnl=round(gross_total, 2), total_estimated_costs=round(cost_total, 2), net_pnl=round(net_total, 2),
        profit_factor=profit_factor, average_win=round(avg_win, 2), average_loss=round(avg_loss, 2),
        expectancy_per_trade=round(expectancy, 2), max_drawdown=round(max_dd, 2),
        average_holding_seconds=round(avg_holding, 1), disclaimer=disclaimer,
    )
