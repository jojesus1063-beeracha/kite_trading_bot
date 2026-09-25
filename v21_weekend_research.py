#!/usr/bin/env python3
"""Weekend EL-BETHEL V2.1 research battery.

Read-only/offline:
- reads trade_history.jsonl
- auto-discovers 1-minute parquet/csv candle caches under runtime/
- evaluates full-post-entry 3-minute failure-to-launch telemetry
- evaluates corrected ARMED confirmation semantics using 1-minute bars
- evaluates ADX/DI threshold retention
- prints prior-only, all-sample, per-day, and leave-one-day-out summaries

No broker/API/order calls. No production files are modified.
"""
from __future__ import annotations

import argparse, json, math
from pathlib import Path
from collections import defaultdict
import pandas as pd

ROOT=Path(".")
ACTIVE_STATUSES={"WIN","LOSS"}

def load_trades(path: Path):
    out=[]
    for i,line in enumerate(path.read_text().splitlines(),1):
        if not line.strip(): continue
        try: r=json.loads(line)
        except Exception: continue
        if not isinstance(r,dict): continue
        sym=r.get("symbol"); direction=str(r.get("direction") or "").upper()
        et=r.get("entry_time") or r.get("timestamp")
        entry=r.get("entry"); exitp=r.get("exit")
        pnl=r.get("gross_pnl", r.get("pnl"))
        if not (sym and direction in {"BUY","SELL"} and et and entry is not None and exitp is not None and pnl is not None):
            continue
        try:
            ts=pd.Timestamp(et)
            entry=float(entry); exitp=float(exitp); pnl=float(pnl)
        except Exception: continue
        if ts.tzinfo is None:
            ts=ts.tz_localize("Asia/Kolkata")
        else:
            ts=ts.tz_convert("Asia/Kolkata")
        ctx=r.get("entry_context_detail") or {}
        plus=ctx.get("plus_di", r.get("plus_di"))
        minus=ctx.get("minus_di", r.get("minus_di"))
        adx=ctx.get("adx_current", r.get("adx_current"))
        sig=(r.get("entry_quality_detail") or {}).get("signal_close", r.get("signal_close"))
        try: sig=float(sig) if sig is not None else entry
        except Exception: sig=entry
        try: adx=float(adx) if adx is not None else None
        except Exception: adx=None
        try:
            plus=float(plus) if plus is not None else None
            minus=float(minus) if minus is not None else None
        except Exception:
            plus=minus=None
        gap=None
        if plus is not None and minus is not None:
            gap=(plus-minus) if direction=="BUY" else (minus-plus)
        out.append(dict(raw=r,line=i,symbol=str(sym),direction=direction,entry_time=ts,date=ts.date().isoformat(),
                        entry=entry,exit=exitp,gross_pnl=pnl,ref=sig,adx=adx,di_gap=gap))
    return out

def discover_1m(symbol,date):
    pats=[
        f"**/{symbol}*1minute*.parquet",f"**/{symbol}*.parquet",
        f"**/{symbol}*1minute*.csv",f"**/{symbol}*.csv",
    ]
    candidates=[]
    for pat in pats:
        candidates.extend(ROOT.glob("runtime/"+pat))
    best=[]
    for p in dict.fromkeys(candidates):
        try:
            if p.stat().st_size==0: continue
            df=pd.read_parquet(p) if p.suffix==".parquet" else pd.read_csv(p)
        except Exception:
            continue
        cols={c.lower():c for c in df.columns}
        tcol=cols.get("date") or cols.get("datetime") or cols.get("timestamp") or cols.get("time")
        if not tcol: continue
        try:
            ts=pd.to_datetime(df[tcol],errors="coerce")
            if getattr(ts.dt,"tz",None) is None:
                ts=ts.dt.tz_localize("Asia/Kolkata",nonexistent="shift_forward",ambiguous="NaT")
            else:
                ts=ts.dt.tz_convert("Asia/Kolkata")
            if (ts.dt.date.astype(str)==date).any():
                best.append((p,df,tcol,ts))
        except Exception: continue
    return best[0] if best else None

def norm_ohlc(df,tcol,ts):
    cols={c.lower():c for c in df.columns}
    need=["open","high","low","close"]
    if not all(x in cols for x in need): return None
    z=pd.DataFrame({
        "ts":ts,
        "open":pd.to_numeric(df[cols["open"]],errors="coerce"),
        "high":pd.to_numeric(df[cols["high"]],errors="coerce"),
        "low":pd.to_numeric(df[cols["low"]],errors="coerce"),
        "close":pd.to_numeric(df[cols["close"]],errors="coerce"),
    }).dropna().sort_values("ts")
    return z

def pct(direction,ref,price):
    return ((price-ref)/ref*100) if direction=="BUY" else ((ref-price)/ref*100)

def armed_decision(tr, bars, confirm=.10, adverse=None, chase=None, expiry_min=9):
    armed=tr["entry_time"]
    start=armed.floor("min")+pd.Timedelta(minutes=1)
    end=armed+pd.Timedelta(minutes=expiry_min)
    q=bars[(bars.ts>=start)&(bars.ts<=end)]
    ref=tr["ref"]
    d=tr["direction"]
    if q.empty:return ("NO_DATA",None,None)
    for _,b in q.iterrows():
        op=pct(d,ref,b.open); fav=pct(d,ref,b.high if d=="BUY" else b.low); adv=-pct(d,ref,b.low if d=="BUY" else b.high)
        # gap/open semantics first
        if adverse is not None and adv>=adverse and ((d=="BUY" and b.open<=ref*(1-adverse/100)) or (d=="SELL" and b.open>=ref*(1+adverse/100))):
            return ("ADVERSE_CANCEL",b.ts,None)
        if chase is not None and ((d=="BUY" and b.open>ref*(1+chase/100)) or (d=="SELL" and b.open<ref*(1-chase/100))):
            return ("CHASE_CANCEL",b.ts,None)
        if op>=confirm and (chase is None or op<=chase):
            return ("ENTER",b.ts,float(b.open))
        touched_confirm=fav>=confirm
        touched_adverse=(adverse is not None and adv>=adverse)
        if touched_confirm and touched_adverse:
            return ("AMBIGUOUS_CANCEL",b.ts,None) # conservative OHLC ordering
        if touched_adverse:return ("ADVERSE_CANCEL",b.ts,None)
        if touched_confirm:
            px=ref*(1+confirm/100) if d=="BUY" else ref*(1-confirm/100)
            return ("ENTER",b.ts,float(px))
    return ("EXPIRE",None,None)

def failure_to_launch(tr,bars,minutes=9,thr=.10):
    # three FULL 3m bars strictly after the entry minute; aggregate from full 1m bars.
    start=(tr["entry_time"].floor("3min")+pd.Timedelta(minutes=3))
    q=bars[(bars.ts>=start)&(bars.ts<start+pd.Timedelta(minutes=minutes))]
    if len(q)<minutes: return None
    q=q.head(minutes)
    ref=tr["entry"]
    d=tr["direction"]
    highs=[pct(d,ref,x) for x in (q.high if d=="BUY" else q.low)]
    mfe=max(highs) if highs else None
    cur=pct(d,ref,float(q.iloc[-1].close))
    return {"mfe":mfe,"current":cur,"flag":mfe is not None and mfe<=thr and cur<=0}

def agg(rows,key="gross_pnl"):
    if not rows:return {"n":0,"gross":0.0,"wins":0,"losses":0}
    vals=[float(r[key]) for r in rows]
    return {"n":len(vals),"gross":round(sum(vals),2),"wins":sum(v>0 for v in vals),"losses":sum(v<0 for v in vals)}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--trade-history",default="trade_history.jsonl")
    ap.add_argument("--out",default="runtime/v21_weekend_research")
    args=ap.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    trades=load_trades(Path(args.trade_history))
    print("TRADES_LOADED =",len(trades))
    enriched=[]
    misses=[]
    for t in trades:
        got=discover_1m(t["symbol"],t["date"])
        if not got:
            misses.append((t["date"],t["symbol"]));continue
        p,df,tcol,ts=got
        bars=norm_ohlc(df,tcol,ts)
        if bars is None:
            misses.append((t["date"],t["symbol"]));continue
        z=dict(t);z["bars"]=bars;z["candle_file"]=str(p)
        enriched.append(z)
    print("TRADES_WITH_1M =",len(enriched))
    print("MISSING_1M =",len(misses))
    if misses: print("MISSING_PAIRS =",misses[:50])

    # FTL thresholds
    ftl_rows=[]
    for thr in [.05,.10,.15,.20,.25,.30,.50]:
        flagged=[]
        for t in enriched:
            f=failure_to_launch(t,t["bars"],9,thr)
            if f and f["flag"]: flagged.append(t)
        ftl_rows.append({"mfe_thr":thr,**agg(flagged)})
    pd.DataFrame(ftl_rows).to_csv(out/"failure_to_launch_thresholds.csv",index=False)

    # corrected ARMED grid
    rows=[]
    for confirm in [.05,.10,.15]:
      for adverse in [None,.05,.10,.25,.40,.50]:
       for chase in [None,.15,.20,.30,.50]:
        for expiry in [3,5,9,15]:
            entered=[]; cancelled=[]; decisions=defaultdict(int)
            for t in enriched:
                status,ts,px=armed_decision(t,t["bars"],confirm,adverse,chase,expiry)
                decisions[status]+=1
                if status=="ENTER":
                    nt=dict(t)
                    nt["gross_pnl"]=((t["exit"]-px)*1 if t["direction"]=="BUY" else (px-t["exit"])*1)
                    # scale by historical qty if available
                    qty=(t["raw"].get("qty") or t["raw"].get("quantity") or 1)
                    try: nt["gross_pnl"]*=float(qty)
                    except Exception: pass
                    entered.append(nt)
                else: cancelled.append(t)
            a=agg(entered)
            rows.append(dict(confirm=confirm,adverse=adverse,chase=chase,expiry=expiry,
                             enter=a["n"],gross=a["gross"],wins=a["wins"],losses=a["losses"],
                             **{f"d_{k}":v for k,v in decisions.items()}))
    ar=pd.DataFrame(rows).sort_values(["gross","enter"],ascending=[False,False])
    ar.to_csv(out/"armed_corrected_grid.csv",index=False)

    # ADX/DI retention
    gd=[]
    for adx in [None,20,22,23,24,25,30]:
      for gap in [None,0,2,3,4,5,7.5,10,15,20]:
        kept=[t for t in trades if (adx is None or (t["adx"] is not None and t["adx"]>=adx)) and
                                  (gap is None or (t["di_gap"] is not None and t["di_gap"]>=gap))]
        gd.append({"adx_min":adx,"gap_min":gap,**agg(kept)})
    pd.DataFrame(gd).to_csv(out/"adx_di_matrix.csv",index=False)

    # per-day baseline and leave-one-day-out
    days=sorted({t["date"] for t in trades})
    per=[]
    for d in days: per.append({"date":d,**agg([t for t in trades if t["date"]==d])})
    pd.DataFrame(per).to_csv(out/"per_day_baseline.csv",index=False)
    loo=[]
    for d in days: loo.append({"left_out":d,**agg([t for t in trades if t["date"]!=d])})
    pd.DataFrame(loo).to_csv(out/"leave_one_day_out_baseline.csv",index=False)

    summary={
        "baseline":agg(trades),
        "trades_with_1m":len(enriched),
        "missing_1m":misses,
        "top_armed":ar.head(20).to_dict("records"),
        "ftl":ftl_rows,
    }
    (out/"summary.json").write_text(json.dumps(summary,indent=2,default=str))
    print("\n===== BASELINE =====");print(summary["baseline"])
    print("\n===== FAILURE TO LAUNCH =====");print(pd.DataFrame(ftl_rows).to_string(index=False))
    print("\n===== TOP CORRECTED ARMED CONFIGS =====");print(ar.head(20).to_string(index=False))
    print("\nOUTPUT_DIR =",out)
    print("READ_ONLY_RESEARCH = TRUE")
    print("NO_BROKER_CALLS = TRUE")

if __name__=="__main__":
    main()
