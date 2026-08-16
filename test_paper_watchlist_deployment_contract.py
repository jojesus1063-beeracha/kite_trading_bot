"""Deployment contract for the validated frozen top-60 PAPER universe."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def test_runner_uses_full_universe_top60_policy():
    runner = (ROOT / "run_paper_watchlist_daily.sh").read_text(encoding="utf-8")

    assert "paper_full_universe_top60_selector.py" in runner
    assert "--top 60" in runner
    assert "--min-selected 60" in runner
    assert "FULL_ZERODHA_CLEAN_TOP60_MOMENTUM" in runner
    assert "expected exactly 60 stocks" in runner
    assert "paper_nse_top_movers_selector.py" not in runner


def test_timer_and_runner_share_0927_boundary():
    timer = (
        ROOT / "systemd" / "kite-paper-watchlist.timer"
    ).read_text(encoding="utf-8")
    runner = (ROOT / "run_paper_watchlist_daily.sh").read_text(encoding="utf-8")

    assert "OnCalendar=Mon..Fri *-*-* 09:26:50 Asia/Kolkata" in timer
    assert "Waiting 20 seconds for the 09:27:10 selector boundary" in runner
    assert "Persistent=false" in timer


def test_service_fails_closed_around_selection():
    service = (
        ROOT / "systemd" / "kite-paper-watchlist.service"
    ).read_text(encoding="utf-8")

    stop = service.index(
        "ExecStartPre=+/usr/bin/systemctl stop kitebot-paper-contrarian.service"
    )
    select = service.index(
        "ExecStart=/bin/bash /home/ubuntu/kite_trading_bot/run_paper_watchlist_daily.sh"
    )
    start = service.index(
        "ExecStartPost=+/usr/bin/systemctl start kitebot-paper-contrarian.service"
    )
    assert stop < select < start
