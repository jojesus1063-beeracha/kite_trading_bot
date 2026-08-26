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

Production integration must call `force_square_off()` on every clock/tick
cycle and must supply a current best-ask price for entry and best-bid price for
exit. Do not enable live execution without a separate reviewed implementation.
