#!/usr/bin/env python3
"""Robustness analysis for EL-BETHEL V2.1 candidate rules.

Offline/read-only. Uses trade_history.jsonl and the downloaded 1-minute cache.
No broker/order/service actions.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path
from collections import defaultdict
import pandas as pd

from v21_weekend_research import load_trades, discover_1m, norm_ohlc, armed_decision, failure_to_launch, agg

CANDIDATES = [
    {"name":"ARMED_C015_A040_C020_E5","confirm":0.15,"adverse":0.40,"chase":0.20,"expiry":5},
    {"name":"ARMED_C010_A040_C020_E5","confirm":0.10,"adverse":0.40,"chase":0.20,"expiry":5},
    {"name":"ARMED_C005_A040_C020_E5","confirm":0.05,"adverse":0.40,"chase":0.20,"expiry":5},
]
FTL = [
    {"name":"FTL_MFE_010_9M","thr":0.10},
    {"name":"FTL_MFE_015_9M","thr":0.15},
    {"name":"FTL_MFE_020_9M","thr":0.20},
]

def enrich(trades):
    out=[]
    for t in trades:
        got=discover_1m(t["symbol"],t["date"],t["exchange"])
        if not got: continue
        p,df,tcol,ts=got
        bars=norm_ohlc(df,tcol,ts)
        if bars is None: continue
        z=dict(t); z["bars"]=bars; z["candle_file"]=str(p)
        out.append(z)
    return out

def sim_armed(t,c):
    status,ts,px=armed_decision(t,t["bars"],c["confirm"],c["adverse"],c["chase"],c["expiry"])
    if status!="ENTER":
        return {"entered":False,"status":status,"gross":0.0}
    qty=t["raw"].get("qty") or t["raw"].get("quantity") or 1
    try: qty=float(qty)
    except Exception: qty=1.0
    gross=((t["exit"]-px) if t["direction"]=="BUY" else (px-t["exit"])) * qty
    return {"entered":True,"status":status,"gross":float(gross)}

def cost_estimate(t):
    # Prefer actual recorded costs when available; otherwise zero.
    for k in ("costs","charges","total_charges"):
        v=t["raw"].get(k)
        if v is not None:
            try:return float(v)
            except Exception:pass
    gp=t["raw"].get("gross_pnl"); np=t["raw"].get("pnl")
    if gp is not None and np is not None:
        try:return max(0.0,float(gp)-float(np))
        except Exception:pass
    return 0.0

def summarize(vals):
    if not vals:return dict(n=0,gross=0.0,net_proxy=0.0,wins=0,losses=0)
    gross=sum(v["gross"] for v in vals)
    costs=sum(v.get("cost",0.0) for v in vals)
    return dict(
        n=len(vals),
        gross=round(gross,2),
        net_proxy=round(gross-costs,2),
        wins=sum(v["gross"]>0 for v in vals),
        losses=sum(v["gross"]<0 for v in vals),
    )

def evaluate_armed(trades,c):
    rows=[]
    for t in trades:
        s=sim_armed(t,c)
        if s["entered"]:
            rows.append({"date":t["date"],"symbol":t["symbol"],"gross":s["gross"],"cost":cost_estimate(t)})
    return rows

def evaluate_ftl_keep(trades,thr):
    rows=[]
    flagged=[]
    for t in trades:
        f=failure_to_launch(t,t["bars"],9,thr)
        if f and f["flag"]:
            flagged.append(t)
        else:
            rows.append({"date":t["date"],"symbol":t["symbol"],"gross":t["gross_pnl"],"cost":cost_estimate(t)})
    return rows, flagged

def per_day(rows):
    by=defaultdict(list)
    for r in rows: by[r["date"]].append(r)
    out=[]
    for d in sorted(by):
        s=summarize(by[d]); out.append({"date":d,**s})
    return out

def loo(rows):
    days=sorted({r["date"] for r in rows})
    out=[]
    for d in days:
        s=summarize([r for r in rows if r["date"]!=d])
        out.append({"left_out":d,**s})
    return out

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--trade-history",default="trade_history.jsonl")
    ap.add_argument("--out",default="runtime/v21_weekend_research/robustness")
    args=ap.parse_args()
    out=Path(args.out); out.mkdir(parents=True,exist_ok=True)

    trades=enrich(load_trades(Path(args.trade_history)))
    print("TRADES_ENRICHED =",len(trades))
    if len(trades)==0: raise SystemExit("No enriched trades")

    baseline=[{"date":t["date"],"symbol":t["symbol"],"gross":t["gross_pnl"],"cost":cost_estimate(t)} for t in trades]
    records=[]

    def add(name,rows,kind):
        allm=summarize(rows)
        pre=summarize([r for r in rows if r["date"]<"2026-09-25"])
        sep25=summarize([r for r in rows if r["date"]=="2026-09-25"])
        exaug=summarize([r for r in rows if r["date"]!="2026-08-31"])
        records.append({"rule":name,"kind":kind,
                        **{f"all_{k}":v for k,v in allm.items()},
                        **{f"prior_{k}":v for k,v in pre.items()},
                        **{f"sep25_{k}":v for k,v in sep25.items()},
                        **{f"ex_aug31_{k}":v for k,v in exaug.items()}})
        pd.DataFrame(per_day(rows)).to_csv(out/f"{name}_per_day.csv",index=False)
        pd.DataFrame(loo(rows)).to_csv(out/f"{name}_loo.csv",index=False)

    add("BASELINE",baseline,"baseline")
    for c in CANDIDATES:
        add(c["name"],evaluate_armed(trades,c),"armed")
    for f in FTL:
        kept,flagged=evaluate_ftl_keep(trades,f["thr"])
        add(f["name"],kept,"ftl_keep")

    summary=pd.DataFrame(records)
    summary.to_csv(out/"summary.csv",index=False)
    print("\n===== ROBUSTNESS SUMMARY =====")
    print(summary.to_string(index=False))
    print("\nOUTPUT_DIR =",out)
    print("READ_ONLY = TRUE")
    print("CAUTION = armed gross/net_proxy reuse historical exits; diagnostic only")

if __name__=="__main__":
    main()
