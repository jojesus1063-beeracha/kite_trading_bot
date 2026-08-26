"""Read-only Zerodha quote parsing for option-buying v1."""
from typing import Optional


def quote_key(exchange: str, tradingsymbol: str) -> str:
    return f"{exchange}:{tradingsymbol}"


def _instrument_quote(payload: dict, key: str) -> dict:
    if not isinstance(payload, dict):
        return {}
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def best_ask_from_quote(payload: dict, key: str) -> Optional[float]:
    levels = ((_instrument_quote(payload, key).get("depth") or {}).get("sell") or [])
    try:
        value = float(levels[0]["price"])
        return value if value > 0 else None
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def best_bid_from_quote(payload: dict, key: str) -> Optional[float]:
    levels = ((_instrument_quote(payload, key).get("depth") or {}).get("buy") or [])
    try:
        value = float(levels[0]["price"])
        return value if value > 0 else None
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def ltp_from_quote(payload: dict, key: str) -> Optional[float]:
    try:
        value = float(_instrument_quote(payload, key).get("last_price"))
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None
