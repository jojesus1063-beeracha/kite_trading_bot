# Stewardship risk hardening

This branch implements the agreed capital-preservation policy without claiming that Scripture is a trading strategy. The Bible references are the ethical framing; the actual trading decisions below are engineering/risk decisions.

## Decisions

- Risk per trade default: **0.20% of configured capital**.
- Daily loss budget default: **0.50% of configured capital**.
- Maximum simultaneous positions: **1** initially.
- Maximum trades/day: **5**.
- Maximum broker-margin allocation: **50%**.
- Minimum strategy reward:risk: **2.0R** and a fixed-percent target may never weaken it.
- The documented bullish/bearish engulfing confirmation is now actually enforced in `strategy.evaluate()`.
- A proposed entry must fit inside the prospective daily downside budget: realized loss used + current open stop risk + proposed stop risk.
- Unknown/missing stop information fails safe: it cannot be treated as zero risk.
- Existing ADX / price-action / market-alignment / news evidence is consolidated into a bounded entry-quality score; the default minimum is 70.
- A losing-trend exit requires two completed adverse candles; one noisy candle is insufficient. Hard stop remains immediate and has first priority.
- Gross/net P&L and broker-fill reconciliation remain unchanged; net P&L remains the risk-manager input.

## Biblical framing used for the design discussion

- Luke 14:28 — count the cost before committing.
- Matthew 25:14–30 — stewardship of entrusted resources.
- Luke 12:15 — guard against greed.
- Matthew 5:37 — truthful reporting/auditability.
- Matthew 7:17–20 — evaluate a strategy by sustained outcomes rather than isolated wins.

## Deployment caveat

`combined_live_launcher.py` is present on the trading VM but is not currently committed to GitHub. Therefore this branch deliberately does **not** invent or overwrite those live-only overrides. Before live deployment, sync the VM working tree, run the main patcher's `--check`, inspect the live launcher overrides, and ensure they do not re-raise risk above the values above.
