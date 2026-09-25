#!/usr/bin/env python3
"""Fetch exact 1-minute candles needed by the V2.1 weekend research.

Historical-data only. No order, position, modify, cancel, or live-service actions.
"""
from __future__ import annotations

import argparse, json, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd

from auth import get_kite_client
from data_feed import get_instrument_token

IST=ZoneInfo("Asia/Kolkata")

def load_pairs(path: Path):
    pairs={}
    for line in path.read_text().splitlines():
        if not line.strip(): continue
        try:r=json.loads(line)
        except Exception:continue
        if not isinstance(r,dict):continue
        sym=r.get("symbol")
        ts=r.get("entry_time") or r.get("timestamp")
        if not sym or not ts:continue
        try:
            t=pd.Timestamp(ts)
            if t.tzinfo is None:t=t.tz_localize("Asia/Kolkata")
            else:t=t.tz_convert("Asia/Kolkata")
        except Exception:continue
        ex=str(r.get("exchange") or "NSE").upper()
        pairs[(t.date().isoformat(),ex,str(sym).upper())]=None
    return sorted(pairs)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--trade-history",default="trade_history.jsonl")
    ap.add_argument("--out",default="runtime/v21_weekend_research/candles_1minute")
    ap.add_argument("--sleep",type=float,default=.4)
    args=ap.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    pairs=load_pairs(Path(args.trade_history))
    kite=get_kite_client()
    ok=skip=fail=0
    failures=[]
    print("UNIQUE_PAIRS =",len(pairs))
    for i,(day,ex,sym) in enumerate(pairs,1):
        save=out/f"{day}_{ex}_{sym}.parquet"
        if save.exists():
            try:
                x=pd.read_parquet(save)
                if not x.empty:
                    skip+=1; print(f"SKIP {i}/{len(pairs)} {day} {ex}:{sym} rows={len(x)}");continue
            except Exception:pass
        try:
            token=get_instrument_token(kite,sym,ex)
            start=datetime.strptime(day+" 09:15:00","%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            end=datetime.strptime(day+" 15:30:00","%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            data=kite.historical_data(token,start,end,"minute",continuous=False,oi=False)
            x=pd.DataFrame(data)
            if x.empty:
                raise RuntimeError("empty historical response")
            if "date" in x.columns:x=x.rename(columns={"date":"timestamp"})
            x["timestamp"]=pd.to_datetime(x["timestamp"])
            x=x.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
            x.to_parquet(save,index=False)
            ok+=1
            print(f"FETCH {i}/{len(pairs)} {day} {ex}:{sym} token={token} rows={len(x)}")
        except Exception as e:
            fail+=1;failures.append((day,ex,sym,str(e)))
            print(f"FAIL {i}/{len(pairs)} {day} {ex}:{sym} error={e}")
        time.sleep(args.sleep)
    print("\nFETCH_OK =",ok)
    print("CACHE_SKIP =",skip)
    print("FETCH_FAIL =",fail)
    if failures:
        print("FAILURES =")
        for row in failures:print(row)
    print("OUTPUT_DIR =",out)
    print("HISTORICAL_DATA_ONLY = TRUE")

if __name__=="__main__":
    main()
