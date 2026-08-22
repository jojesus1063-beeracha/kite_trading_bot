#!/usr/bin/env python3
"""Download only the 3-minute candles needed to replay recorded equity trades.

Reads trade_history.jsonl, resolves the unique NSE symbols, and downloads a
single continuous historical window covering the first trade minus warm-up
calendar days through the last trade.  It is research-only and never imports
or changes live trading state.

Output:
  runtime/trade_replay_history/candles_3minute/<SYMBOL>.parquet
  runtime/trade_replay_history/instrument_map.csv
  runtime/trade_replay_history/download_report.csv
  runtime/trade_replay_history/summary.json

Resume-safe: an existing symbol file is merged/deduplicated and only missing
edge ranges are requested unless --force is supplied.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd

IST = "Asia/Kolkata"
INTERVAL = "3minute"
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
DEFAULT_OUT = Path("runtime/trade_replay_history")


@dataclass
class Report:
    symbol: str
    token: int | None
    status: str
    rows: int = 0
    first: str | None = None
    last: str | None = None
    requests: int = 0
    error: str | None = None


def args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trade-history", default="trade_history.jsonl")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--warmup-days", type=int, default=10,
                   help="Calendar days before first trade for indicator warmup")
    p.add_argument("--chunk-days", type=int, default=50)
    p.add_argument("--request-sleep", type=float, default=0.42)
    p.add_argument("--retries", type=int, default=5)
    p.add_argument("--limit", type=int, default=0,
                   help="Only first N symbols for smoke testing")
    p.add_argument("--force", action="store_true")
    p.add_argument("--csv-fallback", action="store_true")
    return p.parse_args()


def load_trades(path: Path):
    rows=[]
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            x=json.loads(line)
        except Exception:
            continue
        if x.get("symbol") and x.get("date"):
            rows.append(x)
    if not rows:
        raise SystemExit("No valid trades found")
    symbols=sorted({str(x["symbol"]).strip() for x in rows})
    dates=sorted(datetime.strptime(str(x["date"]), "%Y-%m-%d").date() for x in rows)
    return rows, symbols, dates[0], dates[-1]


def get_kite():
    from auth import get_kite_client
    kite=get_kite_client()
    kite.profile()
    return kite


def parquet_ok():
    try:
        import pyarrow  # noqa
        return True
    except Exception:
        try:
            import fastparquet  # noqa
            return True
        except Exception:
            return False


def chunks(start: date, end: date, n: int):
    cur=start
    while cur <= end:
        e=min(end, cur + timedelta(days=n-1))
        yield cur,e
        cur=e+timedelta(days=1)


def request(kite, token, start, end, retries, pause):
    attempt=0
    while True:
        try:
            data=kite.historical_data(
                token,
                datetime.combine(start, MARKET_OPEN),
                datetime.combine(end, MARKET_CLOSE),
                INTERVAL,
                continuous=False,
                oi=False,
            )
            time.sleep(max(0.0,pause))
            return data
        except Exception:
            attempt += 1
            if attempt > retries:
                raise
            time.sleep(min(30, 1.5*(2**(attempt-1))))


def normalize(data, symbol, token):
    if not data:
        return pd.DataFrame(columns=["timestamp","symbol","instrument_token","open","high","low","close","volume"])
    df=pd.DataFrame(data).rename(columns={"date":"timestamp"})
    ts=pd.to_datetime(df["timestamp"], errors="coerce")
    if getattr(ts.dt,"tz",None) is None:
        ts=ts.dt.tz_localize(IST)
    else:
        ts=ts.dt.tz_convert(IST)
    df["timestamp"]=ts
    df["symbol"]=symbol
    df["instrument_token"]=token
    keep=["timestamp","symbol","instrument_token","open","high","low","close","volume"]
    df=df[keep]
    for c in ["open","high","low","close","volume"]:
        df[c]=pd.to_numeric(df[c],errors="coerce")
    return df.dropna(subset=["timestamp","open","high","low","close"])


def load_existing(path, csv):
    if not path.exists():
        return pd.DataFrame()
    df=pd.read_csv(path) if csv else pd.read_parquet(path)
    if not df.empty:
        ts=pd.to_datetime(df["timestamp"],errors="coerce")
        if getattr(ts.dt,"tz",None) is None:
            ts=ts.dt.tz_localize(IST)
        else:
            ts=ts.dt.tz_convert(IST)
        df["timestamp"]=ts
    return df


def save(df,path,csv):
    tmp=path.with_suffix(path.suffix+".tmp")
    if csv:
        df.to_csv(tmp,index=False)
    else:
        df.to_parquet(tmp,index=False,compression="zstd")
    tmp.replace(path)


def needed_ranges(existing,start,end,force):
    if force or existing.empty:
        return [(start,end)]
    first=existing["timestamp"].min().date(); last=existing["timestamp"].max().date()
    out=[]
    if start < first: out.append((start,min(end,first-timedelta(days=1))))
    if end > last: out.append((max(start,last+timedelta(days=1)),end))
    return [(a,b) for a,b in out if a<=b]


def main():
    a=args()
    trade_path=Path(a.trade_history)
    trades,symbols,first_trade,last_trade=load_trades(trade_path)
    if a.limit:
        symbols=symbols[:a.limit]
    fetch_start=first_trade-timedelta(days=max(0,a.warmup_days))
    fetch_end=last_trade

    out=Path(a.out); candle_dir=out/"candles_3minute"
    candle_dir.mkdir(parents=True,exist_ok=True)
    use_csv=False
    if not parquet_ok():
        if not a.csv_fallback:
            raise SystemExit("Parquet engine unavailable; pip install pyarrow or use --csv-fallback")
        use_csv=True

    kite=get_kite()
    requested=set(symbols)
    inst=[]; mapping={}
    for x in kite.instruments("NSE"):
        s=str(x.get("tradingsymbol") or "").strip()
        if s in requested and str(x.get("segment"))=="NSE":
            mapping[s]=int(x["instrument_token"])
            inst.append({"symbol":s,"instrument_token":mapping[s],"name":x.get("name"),"instrument_type":x.get("instrument_type")})
    pd.DataFrame(inst).sort_values("symbol").to_csv(out/"instrument_map.csv",index=False)

    reports=[]
    print(f"Trades={len(trades)} | selected_symbols={len(symbols)} | range={fetch_start}..{fetch_end}")
    for i,symbol in enumerate(symbols,1):
        token=mapping.get(symbol)
        if token is None:
            reports.append(Report(symbol,None,"TOKEN_NOT_FOUND"))
            print(f"[{i}/{len(symbols)}] {symbol}: TOKEN_NOT_FOUND")
            continue
        path=candle_dir/f"{symbol.replace('/','_')}.{'csv' if use_csv else 'parquet'}"
        existing=load_existing(path,use_csv)
        frames=[]; reqs=0
        try:
            for rs,re in needed_ranges(existing,fetch_start,fetch_end,a.force):
                for cs,ce in chunks(rs,re,a.chunk_days):
                    frames.append(normalize(request(kite,token,cs,ce,a.retries,a.request_sleep),symbol,token))
                    reqs += 1
            all_frames=([existing] if not existing.empty else [])+[x for x in frames if not x.empty]
            if all_frames:
                df=pd.concat(all_frames,ignore_index=True).drop_duplicates("timestamp",keep="last").sort_values("timestamp").reset_index(drop=True)
                save(df,path,use_csv)
            else:
                df=pd.DataFrame()
            r=Report(symbol,token,"OK",len(df),
                     df["timestamp"].min().isoformat() if not df.empty else None,
                     df["timestamp"].max().isoformat() if not df.empty else None,
                     reqs)
            reports.append(r)
            print(f"[{i}/{len(symbols)}] {symbol}: OK rows={len(df)} requests={reqs}")
        except Exception as exc:
            reports.append(Report(symbol,token,"ERROR",requests=reqs,error=f"{type(exc).__name__}: {exc}"))
            print(f"[{i}/{len(symbols)}] {symbol}: ERROR {type(exc).__name__}: {exc}")

    pd.DataFrame([asdict(x) for x in reports]).to_csv(out/"download_report.csv",index=False)
    summary={
        "trade_count":len(trades),"requested_symbols":len(symbols),"resolved_tokens":sum(x.token is not None for x in reports),
        "ok_symbols":sum(x.status=="OK" for x in reports),"errors":sum(x.status=="ERROR" for x in reports),
        "token_not_found":sum(x.status=="TOKEN_NOT_FOUND" for x in reports),
        "first_trade_date":first_trade.isoformat(),"last_trade_date":last_trade.isoformat(),
        "fetch_start":fetch_start.isoformat(),"fetch_end":fetch_end.isoformat(),"interval":INTERVAL,
        "storage":"csv" if use_csv else "parquet",
    }
    (out/"summary.json").write_text(json.dumps(summary,indent=2))
    print("\n===== SUMMARY =====")
    print(json.dumps(summary,indent=2))
    print(f"Wrote {out}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
