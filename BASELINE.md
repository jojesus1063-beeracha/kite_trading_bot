# Baseline v1.0 — Reproducibility Guide

This document defines how to reproduce the `baseline-v1.0` tagged state of
this project from scratch. It exists so reproduction never depends on
memory — see ARCHITECTURE.md, "Three Forms of Reproducibility."

## Operating System Assumptions

- Ubuntu 24.04 LTS
- Deployed on Oracle Cloud Infrastructure (VM.Standard.E2.1.Micro)

## Python Version

```bash
python3 --version   # Python 3.12.x
```

## Dependency Installation

```bash
cd ~/kite_trading_bot
python3 -m venv venv
source venv/bin/activate
pip install --break-system-packages -r requirements.txt
```

## Required Environment / Configuration Files

These are NOT tracked in git (see `.gitignore`) — they must exist
separately on the deployment target before startup:

- `access_token.txt` — generated daily via `python3 auth.py`
- `user_config.json` — dashboard-managed settings overrides (optional;
  falls back to `config.py` defaults if absent)
- `totp_config.json` — 2FA secret + backup codes (generated via
  `python3 setup_2fa.py`; dashboard falls back to password-only login
  if absent)
- Environment variables (set via systemd unit files):
  - `KITE_API_KEY`
  - `KITE_API_SECRET`
  - `CONFIG_UI_PASSWORD`
  - `TRADING_CAPITAL` (optional, defaults to 100000 in config.py)

## Startup Commands

Daily routine (token expires every day, before 9:25 AM IST):

```bash
cd ~/kite_trading_bot
source venv/bin/activate
python3 auth.py
# paste the redirect URL / request_token when prompted
sudo systemctl restart kitebot.service
sudo systemctl restart kitedashboard.service
```

## Expected Smoke-Test Outcome

After a fresh checkout + the above setup:

```bash
python3 -m py_compile main.py config.py strategy.py risk_manager.py \
    executor.py indicators.py patterns.py adx_confidence.py scheduler.py \
    configure_app.py backtest.py trade_log.py data_feed.py
echo "Exit code: $?"   # expect 0 -- all modules compile cleanly

sudo systemctl status kitebot.service
# expect: active (running), no tracebacks in the last 10 log lines

sudo journalctl -u kitebot.service -n 10 --no-pager
# expect a clean "Starting LIVE trading on [...]" (or PAPER, if
# PAPER_TRADING=True) line, watchlist listed, no exceptions
```

This constitutes "reproducible" for baseline-v1.0: clean checkout →
clean startup → expected log output, with zero manual debugging.
Once the Session 3 regression dataset exists, this smoke test evolves
into the deterministic regression suite (see ARCHITECTURE.md, Section 9).

## Notes on This Baseline

`baseline-v1.0` represents the fully working, live-tested state as of
2026-07-28, including: candle-aligned scheduler, ADX dynamic confidence
mode, MAX_OPEN_POSITIONS / MAX_POSITION_SIZE_PCT / live margin-check
safeguards, 2FA-protected dashboard, and the dark-theme UI. See
`ARCHITECTURE.md` for the full research-platform design this baseline
serves as the foundation for.
