#!/usr/bin/env python3
"""Fetch 3-minute historical context for V2.1 replay.
Historical-data only; no order/position/service actions.
"""
from __future__ import annotations
import argparse, json, time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
from auth import get_kite_client
from data_feed import get_instrument_token

IST=ZoneInfo("Asia/Kolkata")

def pairs(path):
    out={}
    for line in Path(path).read_text().splitlines():
        if not line.strip(): continue
        try:r=json.loads(line)
        except Exception: continue
        if not isinstance(r,dict): continue
        sym=r.get("symbol"); ts=r.get("entry_time") or r.get("timestamp")
        if not sym or not ts: continue
        t=pd.Timestamp(ts)
        if t.tzinfo is None:t=t.tz_localize("Asia/Kolkata")
        else:t=t.tz_convert("Asia/Kolkata")
        ex=str(r.get("exchange") or "NSE").upper()
        out[(t.date().isoformat(),ex,str(sym).upper())]=1
    return sorted(out)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--trade-history",default="trade_history.jsonl")
    ap.add_argument("--out",default="runtime/v21_weekend_research/candles_3minute_context")
    ap.add_argument("--lookback-days",type=int,default=12)
    ap.add_argument("--sleep",type=float,default=.4)
    args=ap.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    kite=get_kite_client()
    ps=pairs(args.trade_history)
    ok=skip=fail=0
    print("UNIQUE_PAIRS =",len(ps))
    for i,(day,ex,sym) in enumerate(ps,1):
        p=out/f"{day}_{ex}_{sym}.parquet"
        if p.exists():
            try:
                x=pd.read_parquet(p)
                if len(x)>=20:
                    skip+=1;print(f"SKIP {i}/{len(ps)} {day} {ex}:{sym} rows={len(x)}");continue
            except Exception: pass
        try:
            token=get_instrument_token(kite,sym,ex)
            d=datetime.strptime(day,"%Y-%m-%d").replace(tzinfo=IST)
            start=(d-timedelta(days=args.lookback_days)).replace(hour=9,minute=15,second=0)
            end=d.replace(hour=15,minute=30,second=0)
            data=kite.historical_data(token,start,end,"3minute",continuous=False,oi=False)
            x=pd.DataFrame(data)
            if x.empty: raise RuntimeError("empty historical response")
            if "date" in x.columns:x=x.rename(columns={"date":"timestamp"})
            x["timestamp"]=pd.to_datetime(x["timestamp"])
            x=x.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
            x.to_parquet(p,index=False)
            ok+=1;print(f"FETCH {i}/{len(ps)} {day} {ex}:{sym} rows={len(x)}")
        except Exception as e:
            fail+=1;print(f"FAIL {i}/{len(ps)} {day} {ex}:{sym} error={e}")
        time.sleep(args.sleep)
    print("FETCH_OK =",ok);print("CACHE_SKIP =",skip);print("FETCH_FAIL =",fail)
    print("HISTORICAL_DATA_ONLY = TRUE")

if __name__=="__main__": main()
