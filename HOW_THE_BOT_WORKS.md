# How the Live Bot Actually Works
Compiled from source-code tracing and real audit data, Aug 17-18 2026 session.

**Confidence key:** ✅ = verified directly against real source code or real
audit records this session. ⚠️ = inherited from earlier tracing of a related
launcher chain, not independently re-verified against `combined_live_launcher.py`
specifically. Treat ⚠️ items as "probably still true" rather than confirmed.

---

## 1. What's actually running

**Service:** `kitebot-live-combined.service` ✅
**Entry point:** `combined_live_launcher.py`, which hands off execution to
`main.py` via `runpy.run_module("main", run_name="__main__")` ✅ — this is a
genuine hand-off, not a reimplementation, meaning `main.py`'s real risk
machinery (`RiskManager`) is genuinely in the execution path.

**Requires explicit human acknowledgment before real orders can fire:**
an environment variable (`KITE_LIVE_COMBINED_ACK=I_ACCEPT_REAL_ORDERS`) must
be set, or the launcher raises `SystemExit` immediately. ✅

**Daily schedule (all confirmed via `systemctl list-timers`):** ✅
- `kite-live-watchlist.timer` — 09:26:50 IST — rebuilds the 60-symbol
  watchlist and restarts the live bot
- `kitebot-stop.timer` — 15:20:00 IST — backup force-stop (the bot also
  exits on its own around 15:08)
- `kite-watchlist-range-analytics.timer` — 15:35:00 IST — dashboard
  analytics snapshot (unrelated to trading decisions)

---

## 2. Watchlist construction

**Script:** `paper_full_universe_top60_selector.py` ✅, run daily via
`run_live_combined_watchlist_daily.sh`

- Scans the full NSE universe, scores candidates by turnover and ATR%
- `--top 60 --min-selected 60` — always exactly 60 symbols
- `--min-turnover 1000000` (₹10 lakh)
- ATR% must fall within a configured min/max range (`--min-atr-pct`) —
  candidates too flat OR too wild are excluded
- Turnover score weighted logarithmically, capped
- Rebuilt fresh every trading morning — nothing carries over day to day

---

## 3. Entry pipeline — the full sequence

Each of the 60 watchlist symbols is evaluated roughly every ~25-50 seconds,
once per completed 3-minute candle. The sequence below is the order gates
are actually checked, based on real audit record structure. ✅

### Step 1 — Candle freshness
The latest 3-minute candle must be completed and recent (grace period
~5 seconds, max staleness ~90 seconds). Stale/incomplete candles are
rejected before anything else runs (`CANDLE_NOT_COMPLETED_OR_FRESH`). ✅

### Step 2 — Direction (EMA9 vs EMA21)
```
EMA9 > EMA21  →  BUY
EMA9 < EMA21  →  SELL
```
**No regime reversal exists.** This was explicitly fixed this session —
an older mechanism that flipped direction at low ADX has been fully
removed from both the source function and its runtime monkey-patch. ✅
Confirmed live via real startup log: *"EMA direction is never reversed."*

### Step 3 — ADX, two separate checks
1. **Coarse pre-filter:** ADX must be ≥ 20 (or the candidate is rejected
   before direction-specific logic runs at all)
2. **Direction-specific threshold**, checked once direction is known:
   - BUY requires ADX ≥ **25**
   - SELL requires ADX ≥ **20**

   (`PAPER_BUY_MIN_ADX = 25.0`, `PAPER_SELL_MIN_ADX = 20.0`, consistent
   across every file in the codebase that references them.) ✅

   This is a genuine two-stage funnel, not a bug — confirmed by reading
   both enforcement points directly.

### Step 4 — RSI
Calculated and logged, **observational only** — cannot override or block
based on EMA direction. ✅

### Step 5 — VWAP alignment
- BUY requires price above VWAP
- SELL requires price below VWAP
(`VWAP_DIRECTION_NOT_ACCEPTED_OR_UNAVAILABLE` if this fails or VWAP is
unavailable.) ✅

### Step 6 — EMA200
Calculated and logged, **observational only** — does not block. ✅

### Step 7 — Breakout validation (`candle_eligibility.py`)
Four independent sub-checks, all evaluated together: ✅
| Check | Requirement |
|---|---|
| Structure | Close must break the prior 20-bar high (BUY) or low (SELL) |
| Volume | Breakout candle's volume ≥ 1.5× the 20-bar volume SMA |
| Volatility | True range ≥ 1.2× ATR(14) |
| Close position (CLV) | ≥ +0.6 for BUY, ≤ -0.6 for SELL |

Real Aug-17 data: this is genuinely the hardest gate — 98.7% of
evaluations failed it overall, but **69% of the 59 watchlist symbols
passed all four sub-checks at least once during the day.** This is not a
quiet-market artifact; real breakouts happen regularly.

### Step 8 — Overall confirmation count (2-of-3)
Requires at least 2 of these 3 to be true: ✅
1. A Tier-1 candlestick pattern confirmed
2. Ordinary volume confirmation (volume > 1.2× SMA20 — separate from the
   breakout-specific 1.5× check above)
3. Positive price-action score

`INSUFFICIENT_ENTRY_CONFIRMATIONS` if fewer than 2 — this was the single
largest real bottleneck on Aug 17, blocking 86.7% of evaluations.

### Step 9 — Tier-1 candlestick patterns
Detected via `candlestick_engine.py` ✅ — engulfing, hammer, morning/
evening star, three-soldiers/crows, doji, and others. **These patterns
are NOT labeled or treated as "continuation" vs "reversal"** — that
distinction exists in a separate, non-live analysis tool
(`chart_pattern_replay.py`, confirmed not imported anywhere in the live
chain) and doesn't apply here.

### Step 10 — Price-action score
From `price_action.py` — market structure, breakout, pullback, rejection
candle, BOS (Break of Structure), CHoCH (Change of Character), range
detection, support/resistance proximity. Score range roughly -60 to +50.
⚠️ *(The exact standalone hard-gate threshold for THIS launcher wasn't
independently re-confirmed — earlier tracing of a different launcher
found score ≤ 0 blocks outright; whether that identical rule applies
here specifically wasn't re-verified.)*

Real Aug-17 distribution: 31.3% positive, 14.2% zero, 54.4% negative,
average -8.46 — the formula's negative-weighted design means it skews
negative by construction on any non-trending day, not as a defect.

### Step 11 — Triple-pattern policy (separate, parallel path)
`triple_pattern_policy.py` ✅ — detects triple-top/triple-bottom chart
patterns independently, with its own requirements:
- Fresh neckline cross with a small buffer
- Volume ratio ≥ 1.5×
- VWAP alignment
- Own fixed exit: 0.45% stop, 1.0% target (triple top / SELL) or 2.0%
  target (triple bottom / BUY) — **this asymmetry was flagged this
  session as worth confirming was deliberate; not yet resolved.**

Trades from this path use a `PATTERN_FIXED` exit policy instead of the
standard exit stack (see Section 5).

### Step 12 — Cost-aware movement gate
Compares expected move (based on recent true range × an ATR multiplier)
against estimated round-trip trading costs at the current position size.
Requires expected gross P&L ≥ 2× the estimated cost. ✅
`EXPECTED_MOVE_DOES_NOT_COVER_COSTS` — the second-largest real bottleneck
on Aug 17 (28.6%), and it specifically blocked several otherwise-fully-
qualified setups (confirmed via the near-miss deep-dive). This is a
structural interaction with position size at ₹5,000 capital / 0.2% risk
— not a market-quality problem.

### Step 13 — Symbol-level risk guard
- Max 2 completed trades per symbol per day ✅
- 30-minute cooldown after a losing trade on that symbol before
  re-entry is allowed ✅ (confirmed live: MARINE, GENUSPOWER both
  correctly blocked under `LOSS_REENTRY_COOLDOWN` earlier this week)

### Step 14 — Account-level risk limits (`combined_live_launcher.py`)
All confirmed from real startup log and source: ✅
```
risk_per_trade_pct:       0.20%
max_open_positions:       1
max_trades_per_day:       7
max_daily_loss_pct:       0.50%
max_position_size_pct:    20.0% of available margin
check_margin_before_entry: True
```

### Step 15 — Position sizing
0.20% of capital risked per trade, sized against the stop distance. ✅

---

## 4. Data integrity — what prevents look-ahead / stale data

- Every historical fetch used in analysis this session was required to be
  explicitly date-bounded — `fetch_candles()`'s own `datetime.now()`
  default was identified as unsafe for backtesting and avoided throughout
- Live entry decisions use only the latest *completed* candle, with an
  explicit freshness window (Step 1 above)
- `candle_engine` shadow-mode comparison — a WebSocket-fed candle stream is
  cross-checked against REST-fetched candles in the background,
  logging (not blocking on) any mismatches ✅ — confirmed benign, observed
  in real logs, small discrepancies only

---

## 5. Exit stack ⚠️

*This section is inherited from extensive tracing done earlier this
session against `paper_50pct_risk_launcher.py` / `paper_cp9_eod_launcher.py`.
The spec shared during this session states the same exit stack applies to
"clean-candle" trades in the live-combined system, and real live logs
(e.g. `mfe_time_giveback_20_40`, `cp9_mae20_failed_development_eod`,
`trailing_stop` exit reasons) are consistent with this — but the exact
exit code for `combined_live_launcher.py` specifically was not re-read
line-by-line this session.*

**Priority order (first condition to fire wins):**
```
1. Emergency stop        0.75% from entry, triggers on candle CLOSE
2. ATR(14) trailing stop  ×1.2 (tight) or ×2.5 (normal), peak ratchets on close
3. Structure break        10-bar lookback on native 3m candles
4. Trend reversal         15m trend check; ANY deviation from wanted
                           direction counts as reversed (including
                           indeterminate trend)
5. CP9 checkpoint          9-minute one-shot check: MAE ≤ -0.20% AND
                           current P&L < 0
6. MAE adverse-trend       after 10 min: MAE ≤ -0.30%, current ≤ -0.15%,
                           MFE < 0.30%, AND 3 consecutive adverse candles
7. MFE/time bands          20-40-40 minute structure; dead-loser and
                           giveback rules
8. EOD square-off          15:08 IST, unconditional, overrides everything
```

**Known architectural fact, verified earlier this session:** the 2R
profit target computed by the candlestick engine is never actually
checked against price — `ENABLE_FIXED_TARGET=False` in this exit stack
means the geometric target is stored but dead code. The ATR trailing
stop does the real work of capturing profit. Triple-pattern trades are
the exception — they use their own fixed stop/target instead (Section 3,
Step 11).

---

## 6. Safety architecture — genuinely strong points

- Fail-**closed** in live mode for persistence-read failures (fail-open
  in paper mode) — a real, deliberate distinction found in the source
  diff this session ✅
- Explicit environment-variable acknowledgment gate before any real order
  can be placed ✅
- Conservative live-specific limits, tighter than paper defaults
  (0.5% daily loss vs 2% in paper; 1 max position vs up to 10) ✅
- Full audit trail — every single evaluation, accepted or rejected, is
  logged with complete metrics to `runtime/live_combined_audit/entry_audit.jsonl`
  — this is what made all of tonight's analysis possible ✅

---

## 7. What's known to be uncertain or worth further checking

- Triple-pattern target asymmetry (1.0% vs 2.0%) — intentional or not?
- Exact live-combined exit-stack source not independently re-read
  (Section 5)
- Price-action standalone hard-gate exact rule for this specific launcher
  not re-confirmed
- Whether `EXPECTED_MOVE_DOES_NOT_COVER_COSTS` is well-calibrated for
  current capital/risk sizing, given it blocked several fully-qualified
  setups on Aug 17
