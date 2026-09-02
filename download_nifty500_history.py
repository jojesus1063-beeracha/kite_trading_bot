#!/usr/bin/env python3
"""Download current Nifty 500 constituents' Kite historical OHLCV safely.

This utility is intentionally independent of the live trading service.
It downloads the CURRENT Nifty 500 universe backwards over a requested date
range. That is useful for strategy research but introduces survivorship bias;
it is not a point-in-time historical constituent database.

Outputs (default under runtime/historical_nifty500/):
  universe/nifty500_constituents.csv
  universe/kite_instrument_map.csv
  candles_3minute/<SYMBOL>.parquet
  state/progress.json
  reports/download_report.csv
  reports/completeness_summary.json

The downloader is resume-safe at the symbol-file level. Existing candles are
loaded, missing date ranges are requested, and duplicate timestamps are removed.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.request import Request, urlopen

import pandas as pd
from kiteconnect import KiteConnect

IST = "Asia/Kolkata"
DEFAULT_UNIVERSE_URL = "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv"
DEFAULT_OUT = Path("runtime/historical_nifty500")
INTERVAL = "3minute"
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
LOG = logging.getLogger("nifty500_history")


@dataclass
class SymbolReport:
    symbol: str
    instrument_token: int | None
    status: str
    rows_before: int = 0
    rows_after: int = 0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    requests: int = 0
    retries: int = 0
    error: str | None = None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", help="YYYY-MM-DD. Default: one year before --end")
    p.add_argument("--end", help="YYYY-MM-DD. Default: today")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--chunk-days", type=int, default=60,
                   help="Calendar days per Kite historical request (default 60)")
    p.add_argument("--request-sleep", type=float, default=0.42,
                   help="Minimum pause after successful historical calls")
    p.add_argument("--retries", type=int, default=5)
    p.add_argument("--limit", type=int, default=0,
                   help="Only first N symbols; 0 means all. Useful for smoke test")
    p.add_argument("--symbols", nargs="*",
                   help="Optional explicit symbols instead of all constituents")
    p.add_argument("--universe-url", default=DEFAULT_UNIVERSE_URL)
    p.add_argument("--refresh-universe", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="Refetch full requested range even when local rows exist")
    p.add_argument("--csv-fallback", action="store_true",
                   help="Use CSV if parquet engine is unavailable")
    return p.parse_args()


def date_arg(value: str | None, default: date) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date() if value else default


def setup_dirs(root: Path) -> dict[str, Path]:
    paths = {
        "root": root,
        "universe": root / "universe",
        "candles": root / f"candles_{INTERVAL}",
        "state": root / "state",
        "reports": root / "reports",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def download_universe(url: str, target: Path, refresh: bool) -> pd.DataFrame:
    if target.exists() and not refresh:
        df = pd.read_csv(target)
    else:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as resp:
            payload = resp.read()
        df = pd.read_csv(io.BytesIO(payload))
        df.to_csv(target, index=False)

    symbol_col = next((c for c in df.columns if c.strip().lower() == "symbol"), None)
    if not symbol_col:
        raise RuntimeError(f"Nifty 500 CSV has no Symbol column: {list(df.columns)}")
    df = df.rename(columns={symbol_col: "Symbol"})
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    df = df[df["Symbol"].ne("")].drop_duplicates("Symbol")
    return df


def kite_client() -> KiteConnect:
    try:
        import config
    except Exception as exc:
        raise RuntimeError(f"Cannot import config.py: {exc}") from exc

    api_key = None
    for name in ("API_KEY", "KITE_API_KEY"):
        value = getattr(config, name, None)
        if value:
            api_key = str(value).strip()
            break
    if not api_key:
        api_key = os.environ.get("KITE_API_KEY")
    if not api_key:
        raise RuntimeError("Kite API key not found in config or KITE_API_KEY")

    token_path = Path("access_token.txt")
    if not token_path.exists():
        raise RuntimeError("access_token.txt not found; run auth.py first")
    access_token = token_path.read_text().strip()
    if not access_token:
        raise RuntimeError("access_token.txt is empty; run auth.py first")

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    kite.profile()  # fail fast on expired auth
    return kite


def instrument_map(kite: KiteConnect, symbols: Iterable[str], target: Path) -> dict[str, int]:
    requested = set(symbols)
    rows = kite.instruments("NSE")
    selected = []
    mapping: dict[str, int] = {}
    for row in rows:
        symbol = str(row.get("tradingsymbol") or "").strip()
        if symbol not in requested:
            continue
        # Nifty 500 constituents should be ordinary NSE equity symbols.
        if str(row.get("segment")) != "NSE":
            continue
        token = int(row["instrument_token"])
        mapping[symbol] = token
        selected.append({
            "symbol": symbol,
            "instrument_token": token,
            "name": row.get("name"),
            "exchange": row.get("exchange"),
            "segment": row.get("segment"),
            "instrument_type": row.get("instrument_type"),
            "tick_size": row.get("tick_size"),
        })
    pd.DataFrame(selected).sort_values("symbol").to_csv(target, index=False)
    return mapping


def chunks(start: date, end: date, chunk_days: int):
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def request_candles(kite: KiteConnect, token: int, start: date, end: date,
                    retries: int, request_sleep: float) -> tuple[list[dict], int]:
    attempt = 0
    while True:
        try:
            from_dt = datetime.combine(start, MARKET_OPEN)
            to_dt = datetime.combine(end, MARKET_CLOSE)
            data = kite.historical_data(
                token,
                from_dt,
                to_dt,
                INTERVAL,
                continuous=False,
                oi=False,
            )
            time.sleep(max(0.0, request_sleep))
            return data, attempt
        except Exception as exc:
            attempt += 1
            if attempt > retries:
                raise
            delay = min(30.0, 1.5 * (2 ** (attempt - 1)))
            LOG.warning("historical_data failed token=%s %s..%s attempt=%s/%s: %s; sleeping %.1fs",
                        token, start, end, attempt, retries, exc, delay)
            time.sleep(delay)


def normalize(rows: list[dict], symbol: str, token: int) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=[
            "timestamp", "symbol", "instrument_token", "open", "high", "low", "close", "volume"
        ])
    df = pd.DataFrame(rows)
    if "date" not in df.columns:
        raise RuntimeError(f"Historical response missing date for {symbol}")
    df = df.rename(columns={"date": "timestamp"})
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    # Kite usually returns tz-aware timestamps. Preserve/standardize to IST.
    if getattr(ts.dt, "tz", None) is None:
        ts = ts.dt.tz_localize(IST)
    else:
        ts = ts.dt.tz_convert(IST)
    df["timestamp"] = ts
    df["symbol"] = symbol
    df["instrument_token"] = token
    keep = ["timestamp", "symbol", "instrument_token", "open", "high", "low", "close", "volume"]
    df = df[keep]
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["timestamp", "open", "high", "low", "close"])


def parquet_available() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except Exception:
        try:
            import fastparquet  # noqa: F401
            return True
        except Exception:
            return False


def storage_path(candles_dir: Path, symbol: str, use_csv: bool) -> Path:
    safe = symbol.replace("/", "_")
    return candles_dir / f"{safe}.{'csv' if use_csv else 'parquet'}"


def load_existing(path: Path, use_csv: bool) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path) if use_csv else pd.read_parquet(path)
    if not df.empty:
        ts = pd.to_datetime(df["timestamp"], errors="coerce")
        if getattr(ts.dt, "tz", None) is None:
            ts = ts.dt.tz_localize(IST)
        else:
            ts = ts.dt.tz_convert(IST)
        df["timestamp"] = ts
    return df


def write_frame(df: pd.DataFrame, path: Path, use_csv: bool) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    if use_csv:
        df.to_csv(tmp, index=False)
    else:
        df.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)


def merge_frames(existing: pd.DataFrame, incoming: list[pd.DataFrame]) -> pd.DataFrame:
    frames = ([existing] if existing is not None and not existing.empty else []) + [f for f in incoming if not f.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["timestamp"], keep="last")
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def requested_subranges(existing: pd.DataFrame, start: date, end: date, force: bool) -> list[tuple[date, date]]:
    if force or existing.empty:
        return [(start, end)]
    first = pd.Timestamp(existing["timestamp"].min()).date()
    last = pd.Timestamp(existing["timestamp"].max()).date()
    ranges: list[tuple[date, date]] = []
    if start < first:
        ranges.append((start, min(end, first - timedelta(days=1))))
    if end > last:
        ranges.append((max(start, last + timedelta(days=1)), end))
    # If existing spans the requested endpoints, we treat the symbol as complete
    # for resume purposes. Final completeness diagnostics still identify date gaps.
    return [(a, b) for a, b in ranges if a <= b]


def completeness(df: pd.DataFrame, start: date, end: date) -> dict:
    if df.empty:
        return {"rows": 0, "trading_dates": 0, "first": None, "last": None,
                "duplicate_timestamps": 0, "large_intraday_gaps": 0}
    x = df[(df["timestamp"].dt.date >= start) & (df["timestamp"].dt.date <= end)].copy()
    duplicates = int(x.duplicated("timestamp").sum())
    x = x.sort_values("timestamp")
    # Gaps > 6 minutes within the same trading date are worth reviewing. They may
    # be legitimate halts/illiquidity; this is diagnostic, not an automatic failure.
    prev = x["timestamp"].shift()
    delta = x["timestamp"] - prev
    same_day = x["timestamp"].dt.date == prev.dt.date
    gap_count = int(((delta > pd.Timedelta(minutes=6)) & same_day).sum())
    return {
        "rows": int(len(x)),
        "trading_dates": int(x["timestamp"].dt.date.nunique()),
        "first": x["timestamp"].min().isoformat() if not x.empty else None,
        "last": x["timestamp"].max().isoformat() if not x.empty else None,
        "duplicate_timestamps": duplicates,
        "large_intraday_gaps": gap_count,
    }


def save_progress(path: Path, start: date, end: date, reports: list[SymbolReport]) -> None:
    payload = {
        "updated_at": datetime.now().astimezone().isoformat(),
        "interval": INTERVAL,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbols_processed": len(reports),
        "reports": [asdict(r) for r in reports],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(path)


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    end = date_arg(args.end, date.today())
    start = date_arg(args.start, end - timedelta(days=365))
    if start > end:
        raise SystemExit("--start must be <= --end")
    if args.chunk_days < 1:
        raise SystemExit("--chunk-days must be >= 1")

    paths = setup_dirs(Path(args.out))
    universe_path = paths["universe"] / "nifty500_constituents.csv"
    universe = download_universe(args.universe_url, universe_path, args.refresh_universe)
    symbols = universe["Symbol"].tolist()
    if args.symbols:
        wanted = set(args.symbols)
        symbols = [s for s in symbols if s in wanted]
        missing_requested = sorted(wanted - set(symbols))
        if missing_requested:
            LOG.warning("Requested symbols not in current Nifty 500 list: %s", missing_requested)
    if args.limit:
        symbols = symbols[: args.limit]
    if not symbols:
        raise SystemExit("No symbols selected")

    use_csv = False
    if not parquet_available():
        if not args.csv_fallback:
            raise SystemExit("Parquet engine missing. Install pyarrow: pip install pyarrow, or rerun with --csv-fallback")
        use_csv = True
        LOG.warning("Parquet engine unavailable; using CSV fallback")

    kite = kite_client()
    mapping = instrument_map(kite, symbols, paths["universe"] / "kite_instrument_map.csv")
    missing = [s for s in symbols if s not in mapping]
    if missing:
        LOG.warning("%d constituent symbols were not resolved in current NSE instruments: %s",
                    len(missing), missing[:30])

    reports: list[SymbolReport] = []
    progress_path = paths["state"] / "progress.json"

    LOG.info("Starting %s download: %s..%s symbols=%d chunk_days=%d storage=%s",
             INTERVAL, start, end, len(symbols), args.chunk_days, "CSV" if use_csv else "Parquet")

    for index, symbol in enumerate(symbols, start=1):
        token = mapping.get(symbol)
        if token is None:
            report = SymbolReport(symbol, None, "TOKEN_NOT_FOUND")
            reports.append(report)
            save_progress(progress_path, start, end, reports)
            continue

        path = storage_path(paths["candles"], symbol, use_csv)
        try:
            existing = load_existing(path, use_csv)
            before = len(existing)
            ranges = requested_subranges(existing, start, end, args.force)
            incoming: list[pd.DataFrame] = []
            request_count = 0
            retry_count = 0

            LOG.info("[%d/%d] %s token=%s existing=%d ranges=%s",
                     index, len(symbols), symbol, token, before, ranges or "complete")

            for outer_start, outer_end in ranges:
                for chunk_start, chunk_end in chunks(outer_start, outer_end, args.chunk_days):
                    rows, retries_used = request_candles(
                        kite, token, chunk_start, chunk_end, args.retries, args.request_sleep
                    )
                    request_count += 1
                    retry_count += retries_used
                    incoming.append(normalize(rows, symbol, token))

            combined = merge_frames(existing, incoming)
            if not combined.empty:
                # Keep local symbol files bounded to the union already downloaded;
                # do not discard older rows if the user later expands the range.
                write_frame(combined, path, use_csv)

            diag = completeness(combined, start, end)
            report = SymbolReport(
                symbol=symbol,
                instrument_token=token,
                status="OK",
                rows_before=before,
                rows_after=int(len(combined)),
                first_timestamp=diag["first"],
                last_timestamp=diag["last"],
                requests=request_count,
                retries=retry_count,
            )
        except KeyboardInterrupt:
            LOG.warning("Interrupted; progress has been preserved. Rerun the same command to resume.")
            save_progress(progress_path, start, end, reports)
            return 130
        except Exception as exc:
            LOG.exception("%s failed", symbol)
            report = SymbolReport(symbol, token, "ERROR", error=f"{type(exc).__name__}: {exc}")

        reports.append(report)
        save_progress(progress_path, start, end, reports)

    report_df = pd.DataFrame([asdict(r) for r in reports])
    report_df.to_csv(paths["reports"] / "download_report.csv", index=False)

    diagnostics = {}
    total_rows = 0
    ok_count = 0
    for report in reports:
        if report.status != "OK":
            diagnostics[report.symbol] = {"status": report.status, "error": report.error}
            continue
        ok_count += 1
        path = storage_path(paths["candles"], report.symbol, use_csv)
        frame = load_existing(path, use_csv)
        diag = completeness(frame, start, end)
        diagnostics[report.symbol] = diag
        total_rows += int(diag["rows"])

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "universe_note": "Current Nifty 500 constituents applied backward; survivorship bias exists.",
        "interval": INTERVAL,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "requested_symbols": len(symbols),
        "resolved_tokens": len(mapping),
        "ok_symbols": ok_count,
        "total_rows_in_requested_window": total_rows,
        "storage": "csv" if use_csv else "parquet",
        "symbols": diagnostics,
    }
    (paths["reports"] / "completeness_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )

    print("\n=== NIFTY 500 HISTORY DOWNLOAD COMPLETE ===")
    print(f"Range:          {start} .. {end}")
    print(f"Interval:       {INTERVAL}")
    print(f"Symbols:        {len(symbols)} requested / {ok_count} OK")
    print(f"Rows in range:  {total_rows:,}")
    print(f"Output:         {paths['root']}")
    print(f"Report:         {paths['reports'] / 'download_report.csv'}")
    print(f"Completeness:   {paths['reports'] / 'completeness_summary.json'}")
    return 0 if ok_count == len(symbols) else 2


if __name__ == "__main__":
    raise SystemExit(main())
