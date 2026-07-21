# Kite intraday candle + MA trading bot

Strategy: 15-min trend filter (20/50 EMA + VWAP) confirms direction,
5-min bullish/bearish engulfing candle + EMA20 + above-average volume
triggers entry, strict stop-loss with a minimum 1:2 reward:risk.

## Before anything else

1. **This is not financial advice.** This code automates a strategy you
   design — it does not know whether the strategy is profitable. Backtest
   thoroughly, then paper-trade (`config.PAPER_TRADING = True`) for at
   least a few weeks before risking real capital.
2. **Compliance:** SEBI's retail algo trading framework (fully mandatory
   since April 2026) permits self-use — automating trades for your own
   account without third-party registration — but requires things like
   static IP whitelisting and 2FA on your API access. Rules have been
   evolving; check the current requirements directly on
   [Kite Connect's developer docs](https://kite.trade/docs/connect/v3/)
   and your Zerodha console before going live.
3. **Kite Connect is a separate paid subscription** from your regular
   Zerodha account — subscribe at https://developers.kite.trade.
4. **Static IP is mandatory for order placement**, effective since April 1,
   2026 — this applies to every API user regardless of order volume,
   including personal self-use. Data/websocket endpoints are unaffected;
   it's specifically order placement/modification/cancellation calls.
   Home broadband is almost always a *dynamic* IP, so if you're running
   this on your laptop over regular home internet, orders will start
   getting rejected whenever your IP changes. Options:
   - Ask your ISP if they offer a static IP add-on
   - Route only the order-placement calls through a static-IP proxy/VPN
     service, keeping the bot itself on your laptop
   - Whatever IP you land on, add it in the Kite Connect developer
     console (Profile → static IP) — you get one mandatory primary and
     one optional secondary
   This does **not** stop you from running the bot itself on your
   laptop — it's specifically about the IP your orders originate from.

## Setup

```bash
pip install -r requirements.txt
```

Set your credentials as environment variables (don't hardcode them):

```bash
export KITE_API_KEY="your_key"
export KITE_API_SECRET="your_secret"
export TRADING_CAPITAL="100000"   # your intraday capital in INR
```

Edit `config.py` to set your watchlist, risk parameters, and confirm
`PAPER_TRADING = True` for your first runs.

## Daily workflow

```bash
# 1. Generate today's access token (manual login step, Kite requires this daily)
python auth.py

# 2. Run the bot
python main.py
```

## Backtest before trading live

```bash
python backtest.py RELIANCE 2026-06-01 2026-07-01
```

## Project layout

| File | Purpose |
|---|---|
| `config.py` | All tunable settings — watchlist, EMAs, risk, timeframes |
| `auth.py` | Daily Kite Connect login/token generation |
| `data_feed.py` | Historical candle fetching |
| `indicators.py` | EMA, VWAP, rolling average volume |
| `patterns.py` | Bullish/bearish engulfing detection |
| `strategy.py` | Combines trend + entry + volume into buy/sell signals |
| `risk_manager.py` | Position sizing, daily loss kill-switch, trade count cap |
| `executor.py` | Places entry/exit orders (or logs them in paper mode) |
| `main.py` | Live/paper trading loop |
| `backtest.py` | Historical strategy validation |

## What still needs your judgment

- **Watchlist** — the 4 placeholder symbols in `config.py` are examples, not
  recommendations.
- **Position persistence** — `main.py` tracks open positions in memory only.
  If the script restarts mid-day, it loses track of any open position. For
  anything beyond paper-trading, add a small state file or DB.
- **Order/network failure handling** — the executor doesn't yet retry or
  reconcile against actual broker positions after a crash. Add that before
  trading real size.
- **Algo tagging** — if your broker requires an Algo ID/strategy tag on
  orders once registered, add it in `executor.py` (commented placeholder
  included).
