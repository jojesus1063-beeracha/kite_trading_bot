"""
Loads and validates the F&O contract master (option chain instrument
list) for a given underlying, from Kite's own instruments() dump --
never hardcoded, never manually maintained (spec #29).

Caches the raw dump to disk once per calendar day (same pattern as the
equity bot's instruments_cache*.json, which is why that filename
pattern is already gitignored) so repeated lookups during PREPARE
don't re-fetch the full instrument list, which is large and rate-
limited. The cache is keyed by (exchange, date) and is NEVER reused
across days -- a stale contract master could describe an expired or
delisted contract as valid.
"""
import json
import os
import logging
from datetime import date, datetime
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("fno.contract_master")

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instrument_cache")


@dataclass(frozen=True)
class ContractRecord:
    tradingsymbol: str
    exchange: str
    instrument_token: int
    name: str
    expiry: date
    strike: float
    instrument_type: str  # "CE" | "PE" | "FUT"
    lot_size: int
    tick_size: float
    segment: str


def _cache_path(exchange: str, as_of: date) -> str:
    return os.path.join(CACHE_DIR, f"instruments_{exchange}_{as_of.isoformat()}.json")


def _parse_expiry(raw) -> Optional[date]:
    if raw in (None, ""):
        return None
    if isinstance(raw, date):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, str):
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def fetch_and_cache(kite, exchange: str, as_of: date = None) -> list[dict]:
    """
    Fetches kite.instruments(exchange) and writes it to today's cache
    file (atomic write). Always hits the broker if no cache exists yet
    for today; never silently reuses yesterday's file.
    """
    as_of = as_of or date.today()
    os.makedirs(CACHE_DIR, exist_ok=True)
    raw = kite.instruments(exchange)
    path = _cache_path(exchange, as_of)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(raw, f, default=str)
    os.replace(tmp_path, path)
    logger.info(f"INSTRUMENT_MASTER_LOADED exchange={exchange} count={len(raw)} as_of={as_of.isoformat()}")
    return raw


def load_contract_master(kite, exchange: str, as_of: date = None, force_refresh: bool = False) -> list[ContractRecord]:
    """
    Returns today's contract master for `exchange` (e.g. "BFO" for
    SENSEX, "NFO" for NIFTY/BANKNIFTY) as a list of ContractRecord.
    Uses today's on-disk cache if present, unless force_refresh=True.
    Never falls back to a stale (previous-day) cache silently.
    """
    as_of = as_of or date.today()
    path = _cache_path(exchange, as_of)

    raw = None
    if not force_refresh and os.path.exists(path):
        try:
            with open(path) as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError):
            raw = None

    if raw is None:
        raw = fetch_and_cache(kite, exchange, as_of)

    records = []
    for row in raw:
        instrument_type = row.get("instrument_type")
        if instrument_type not in ("CE", "PE", "FUT"):
            continue  # skip equities/futures-of-other-kinds/rows that don't belong in an option chain view
        expiry = _parse_expiry(row.get("expiry"))
        if expiry is None:
            continue
        try:
            records.append(ContractRecord(
                tradingsymbol=row["tradingsymbol"],
                exchange=row.get("exchange", exchange),
                instrument_token=int(row["instrument_token"]),
                name=row.get("name", ""),
                expiry=expiry,
                strike=float(row.get("strike", 0.0)),
                instrument_type=instrument_type,
                lot_size=int(row.get("lot_size", 0)),
                tick_size=float(row.get("tick_size", 0.05)),
                segment=row.get("segment", exchange),
            ))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"Skipping malformed instrument row {row.get('tradingsymbol')}: {e}")
            continue

    if not records:
        raise ValueError(f"Contract master for {exchange} loaded but contains zero usable option/future rows "
                          f"-- refusing to proceed with an empty instrument universe")

    return records


def filter_for_underlying(records: list[ContractRecord], underlying_name: str) -> list[ContractRecord]:
    """
    Kite's `name` field on option rows is the underlying's name (e.g.
    "SENSEX"). Filtering here rather than assuming a tradingsymbol
    prefix, since prefixes are not guaranteed stable across contract
    master changes (spec #29).
    """
    return [r for r in records if r.name.upper() == underlying_name.upper()]
