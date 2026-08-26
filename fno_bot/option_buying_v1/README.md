# F&O Option Buying V1

This package is an isolated, PAPER-only execution layer. It does not modify
the equity strategy, watchlist, capital, executor, position store or order
parameters.

The boundary is `UnderlyingSignal`: an upstream strategy supplies only the
underlying symbol, bullish/bearish direction, spot price and timestamp. The
resolver then loads/accepts the current NFO master, excludes expiry dates with
less than one day remaining, selects the nearest remaining expiry, finds the
closest listed ATM strike, chooses CE for bullish or PE for bearish, reads the
lot size from that exact contract, obtains an executable BUY price and applies
the whole-lot ₹5,000 affordability check.

V1 buys exactly one whole lot. It never falls back to OTM, never shorts an
option and exposes no live broker-order method. The position engine measures
MFE/MAE and persists closed PAPER records. It intentionally has no arbitrary
target or stop; its only automatic exit is the 15:10 mandatory square-off.

`launcher.py` supplies the PAPER orchestration. It tails only equity signals
already recorded with `executed=true`, rejects stale signals, refreshes spot
from Zerodha, resolves against today's NFO master, prices entry at best ask,
subscribes to the selected option and monitors ticks. From 15:10 it retries
best-bid exits; at the final 15:15 PAPER deadline it uses an explicitly audited
last-price fallback so a simulated position cannot remain overnight. State and
consumed signal IDs are persisted atomically for crash-safe restarts.
When deployed from a separate worktree, set `FNO_V1_SIGNAL_LOG_DIR` to
the equity bot's actual `signal_logs` directory; the example systemd unit
does this explicitly.

Do not enable live execution without a separate reviewed implementation.
