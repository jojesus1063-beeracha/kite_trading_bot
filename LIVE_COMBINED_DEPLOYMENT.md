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
