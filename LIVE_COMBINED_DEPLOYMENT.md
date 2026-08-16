# Guarded combined LIVE strategy

This deployment keeps the proven PAPER stack available while adding a separate
LIVE stack. Installing these files does **not** enable or start live trading.

## Hard limits

- Risk per trade: 0.20% of configured capital
- Simultaneous positions: 1
- Entries per day: 7
- Daily loss shutdown: 0.50%
- Margin check: mandatory
- Product: MIS only
- Exact environment acknowledgement:
  `KITE_LIVE_COMBINED_ACK=I_ACCEPT_REAL_ORDERS`

## Effective parameter sheet

### Universe and schedule

- Universe: cleaned ordinary NSE/BSE cash equities
- Daily frozen watchlist: top 60 unique symbols
- Snapshot: approximately 09:27:10 IST
- Price: Rs20-Rs2,200
- Turnover: at least Rs10 lakh
- Spread: at most 0.25%
- Circuit distance: at least 1.0%
- Movement: absolute change at least 0.30% OR day range at least 0.40%
- Score weights: turnover 30, movement 25, volume 20, spread 10,
  depth 5, open-extreme 5, previous-day momentum 5

### Timeframes and entry window

- Entry candles: completed 3-minute candles
- Trend context: latest completed 15-minute candle
- New entries: 09:25-15:00 IST
- Position checks: every 25 seconds
- Scheduled square-off: 15:08 IST
- Systemd stop backstop: 15:20 IST

### Clean-candle route

- Direction: EMA9 above EMA21 = BUY; EMA9 below EMA21 = SELL
- ADX: at least 25 for BUY and 20 for SELL
- RSI(14): observational only
- VWAP directional acceptance: mandatory
- Validated breakout: mandatory
- Secondary confirmations: any 2 of 3
  - direction-matching tier-1 TA-Lib pattern
  - volume greater than 1.20x the prior 20-candle average
  - positive price-action score
- Cost-aware movement: 14-candle lookback, 1.0 ATR expected move,
  expected gross at least 2x estimated round-trip cost
- EMA200 and standalone RVOL gates: disabled
- Entry timing: maximum 1.50 ATR extension, body ratio at least 0.50,
  volume acceleration at least 1.10

### Confirmed triple-pattern route

- Patterns: triple top SELL and triple bottom BUY only
- Fresh completed 3-minute neckline cross
- VWAP alignment mandatory
- Volume: at least 1.50x prior 20-candle average
- Stop: 0.45%
- Triple-top target: 1.00%
- Triple-bottom target: 2.00%
- Maximum: one pattern entry per symbol per day

### Risk and execution

- Capital: `TRADING_CAPITAL` environment value (must be positive)
- Risk per trade: 0.20%
- Maximum position notional: 20% of capital
- Maximum simultaneous positions: 1
- Maximum total entries: 7 per day
- Maximum clean-candle entries per symbol: 2 per day
- Post-loss same-symbol cooldown: 30 minutes
- Daily-loss shutdown: 0.50% of capital
- Product/order: MIS regular MARKET
- Margin check: mandatory
- Market protection: required (`-1` means broker/exchange automatic)
- Exact live acknowledgement: mandatory

### Exit behavior

- Pattern trades: fixed pattern stop/target; hybrid and paper MFE/MAE overlays skipped
- Clean-candle trades with at least two shares: 50% at 1R, remaining
  50% at 2R, then verified broker stop moved to break-even
- One-share clean-candle trade: fixed 0.70% target fallback
- Trailing stop: disabled
- Paper-only MAE/MFE/time overlays: disabled in live mode

## Flow

```text
09:26:50 live timer
        |
        v
stop competing bot services
        |
        v
09:27:10 read-only full-universe selector
        |
        v
validate 60 clean NSE/BSE symbols + today's report
        |
        v
verify local journals flat + broker MIS positions/orders flat
        |
        v
atomically update only user_config.watchlist
        |
        v
start guarded combined live launcher
        |
        +--> confirmed triple top/bottom
        |      fixed 0.45% stop; 1%/2% target
        |
        +--> existing clean-candle eligibility
               existing fixed/hybrid exit handling
        |
        v
confirmed broker fill -> durable position -> verified broker stop
        |
        v
15:08 in-process square-off; 15:20 systemd stop backstop
```

## Deployment rule

Do not enable `kite-live-watchlist.timer` while
`kite-paper-watchlist.timer` is enabled. Install and test first; switch timers
only after the config, acknowledgement, broker-flat check, and unit tests pass.
