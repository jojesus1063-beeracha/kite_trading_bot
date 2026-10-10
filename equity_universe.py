"""Ordinary equity universe and rate-limited quote retrieval for Matmon."""
from __future__ import annotations
import math
import re
import time
from typing import Any
from collections import Counter
from kiteconnect.exceptions import NetworkException

def positive_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default

    if not math.isfinite(result):
        return default

    return result


def positive_int(value: Any, default: int = 0) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default

    return max(result, 0)


ALLOWED_EXCHANGES = {"NSE", "BSE"}


DISALLOWED_SUFFIXES = (
    "-BE", "-BZ", "-ST", "-SM", "-IL", "-BT", "-GC", "-W1", "-W2",
)


SELECTOR_QUOTE_BATCH_SIZE = 200


SELECTOR_QUOTE_BATCH_DELAY_SECONDS = 1.10


SELECTOR_QUOTE_MAX_ATTEMPTS = 3


SELECTOR_QUOTE_RETRY_BACKOFF_SECONDS = (1.50, 3.00)


NON_ORDINARY_SERIES_RE = re.compile(
    r"(?:^SGB|-(?:GB|GS|IV|RR|N[A-Z0-9]|Y[A-Z0-9])$)",
    re.IGNORECASE,
)


NON_ORDINARY_SYMBOL_RE = re.compile(r"(?:ETF|IETF|BEES|NETF)$", re.IGNORECASE)


NON_ORDINARY_NAME_RE = re.compile(
    r"\b(?:ETF|EXCHANGE\s+TRADED\s+FUND|MUTUAL\s+FUND|INDEX\s+FUND|"
    r"SOVEREIGN\s+GOLD\s+BOND|TREASURY\s+BILL|GOVERNMENT\s+SECURIT(?:Y|IES)|"
    r"NON[- ]CONVERTIBLE\s+DEBENTURE|DEBENTURE|NCD|INFRASTRUCTURE\s+INVESTMENT\s+TRUST|"
    r"REAL\s+ESTATE\s+INVESTMENT\s+TRUST)\b",
    re.IGNORECASE,
)


def ordinary_equity_rejection_reason(instrument: dict[str, Any], exchange: str) -> str | None:
    ex = str(instrument.get("exchange") or "").upper()
    segment = str(instrument.get("segment") or "").upper()
    instrument_type = str(instrument.get("instrument_type") or "").upper()
    symbol = str(instrument.get("tradingsymbol") or "").strip().upper()
    name = str(instrument.get("name") or "").strip().upper()
    isin = str(instrument.get("isin") or "").strip().upper()
    lot_size = positive_int(instrument.get("lot_size"), 0)

    if exchange not in ALLOWED_EXCHANGES or ex != exchange:
        return "wrong_exchange"
    if segment != exchange:
        return "non_cash_segment"
    if instrument_type != "EQ":
        return "non_eq_instrument_type"
    if not symbol:
        return "blank_symbol"
    if lot_size != 1:
        return "lot_size_not_one"
    if symbol.endswith(DISALLOWED_SUFFIXES):
        return "special_series_suffix"
    if NON_ORDINARY_SERIES_RE.search(symbol):
        return "non_ordinary_series"
    # Kite's documented instrument-master columns do not include ISIN. Keep
    # this check only for supplied metadata/fixtures; absence itself is valid.
    if isin and not isin.startswith("INE"):
        return "non_ordinary_isin"
    if NON_ORDINARY_SYMBOL_RE.search(symbol):
        return "fund_like_symbol"
    if NON_ORDINARY_NAME_RE.search(name):
        return "fund_or_debt_like_name"
    return None


def cleaned_equity_instruments(kite: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    rejected: Counter[str] = Counter()
    raw_by_exchange: Counter[str] = Counter()
    clean_by_exchange: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()

    from data_feed import _get_instrument_master

    for exchange in ("NSE", "BSE"):
        master = _get_instrument_master(kite, exchange)
        instruments = list(master.values())
        if not isinstance(instruments, list):
            raise RuntimeError(f"Kite {exchange} instrument response was not a list")
        raw_by_exchange[exchange] += len(instruments)

        for instrument in instruments:
            reason = ordinary_equity_rejection_reason(instrument, exchange)
            if reason is not None:
                rejected[reason] += 1
                continue

            symbol = str(instrument.get("tradingsymbol") or "").strip().upper()
            key = (exchange, symbol)
            if key in seen:
                rejected["duplicate_exchange_symbol_in_master"] += 1
                continue
            seen.add(key)

            cleaned.append(
                {
                    "symbol": symbol,
                    "exchange": exchange,
                    "company_name": str(instrument.get("name") or "").strip(),
                    "industry": "",
                    "series": "EQ",
                    "isin": str(instrument.get("isin") or "").strip().upper(),
                    "instrument_token": positive_int(instrument.get("instrument_token")),
                    "tick_size": positive_float(instrument.get("tick_size"), 0.05),
                    "lot_size": positive_int(instrument.get("lot_size"), 1),
                }
            )
            clean_by_exchange[exchange] += 1

    return cleaned, {
        "raw_total": sum(raw_by_exchange.values()),
        "raw_by_exchange": dict(raw_by_exchange),
        "clean_total": len(cleaned),
        "clean_by_exchange": dict(clean_by_exchange),
        "cleaning_rejections": dict(sorted(rejected.items())),
    }


def fetch_selector_quotes(
    kite: Any,
    quote_keys: list[str],
) -> dict[str, dict[str, Any]]:
    """Fetch full quotes in rate-limited batches with bounded transient retry."""
    quotes: dict[str, dict[str, Any]] = {}

    for start in range(0, len(quote_keys), SELECTOR_QUOTE_BATCH_SIZE):
        if start:
            time.sleep(SELECTOR_QUOTE_BATCH_DELAY_SECONDS)

        batch = quote_keys[start : start + SELECTOR_QUOTE_BATCH_SIZE]

        for attempt in range(1, SELECTOR_QUOTE_MAX_ATTEMPTS + 1):
            try:
                response = kite.quote(batch)

                if not isinstance(response, dict):
                    raise RuntimeError(
                        "Kite quote response was not a dictionary"
                    )

                quotes.update(response)
                break

            except NetworkException:
                if attempt >= SELECTOR_QUOTE_MAX_ATTEMPTS:
                    raise

                backoff = SELECTOR_QUOTE_RETRY_BACKOFF_SECONDS[
                    attempt - 1
                ]
                time.sleep(backoff)

    return quotes
