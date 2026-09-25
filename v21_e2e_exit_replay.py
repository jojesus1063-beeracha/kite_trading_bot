#!/usr/bin/env python3
"""High-fidelity offline replay of the leading EL-BETHEL V2.1 ARMED candidate.

Uses:
- exact downloaded 1-minute candles,
- current VM's local el_bethel.py pure EXIT_V2 functions,
- the original structural logical stop frozen at candidate approval,
- corrected ARMED entry semantics,
- intraminute protective-stop touch using 1m OHLC,
- completed 3m candles for EXIT_V2 state/trailing updates,
- estimated equity intraday charges.

No broker/API/service calls. No production state mutation.

Caveat: 1m OHLC cannot reproduce tick ordering/slippage exactly, so this is a
conservative candle-path replay rather than tick-perfect execution reconstruction.
"""
from __future__ import annotations

import argparse, json
from pathlib import Path
from datetime import time as dtime
import pandas as pd

import el_bethel
from v21_weekend_research import (
    load_trades, discover_1m, norm_ohlc, armed_decision
)

CONFIRM=0.15
ADVERSE=0.40
CHASE=0.20
EXPIRY=5

def estimate_cost(entry, exit_price, qty):
    turnover=(entry+exit_price)*qty
    brokerage=min(20.0,entry*qty*0.0003)+min(20.0,exit_price*qty*0.0003)
    exchange=turnover*0.0000345
    sebi=turnover*0.000001
    stamp=entry*qty*0.00003
    stt=exit_price*qty*0.00025
    gst=(brokerage+exchange+sebi)*0.18
    return brokerage+exchange+sebi+stamp+stt+gst

def get_initial_stop(t):
    r=t["raw"]
    keys=(
        "el_bethel_v2_initial_logical_stop",
        "initial_logical_stop",
        "structural_stop",
        "stop",
        "stop_loss",
    )
    for k in keys:
        v=r.get(k)
        if v is not None:
            try:
                x=float(v)
                if x>0:return x,k
            except Exception:pass
    return None,None

def get_qty(t):
    for k in ("qty","quantity","filled_quantity","requested_quantity"):
        v=t["raw"].get(k)
        if v is not None:
            try:
                q=int(float(v))
                if q>0:return q
            except Exception:pass
    return 1

def get_tick(t):
    try:
        x=float(t["raw"].get("tick_size") or 0.05)
        return x if x>0 else 0.05
    except Exception:
        return 0.05

def resample_3m(bars):
    z=bars.copy().set_index("ts").sort_index()
    # NSE/BSE regular session aligns naturally to 09:15 on a 3-minute grid.
    r=z.resample("3min",origin="start_day").agg({
        "open":"first","high":"max","low":"min","close":"last"
    }).dropna().reset_index()
    return r.rename(columns={"ts":"date"})

def stop_fill(direction, bar, trigger):
    op=float(bar.open)
    if direction=="BUY":
        if float(bar.low)>trigger:return None
        return min(op,trigger) if op<trigger else trigger
    if float(bar.high)<trigger:return None
    return max(op,trigger) if op>trigger else trigger

def last_completed_3m_time(minute_ts):
    # A 3m candle starting at t is usable at t+3m.
    return minute_ts.floor("3min") - pd.Timedelta(minutes=3)

def replay_one(t,bars,squareoff):
    status,entry_ts,entry_px=armed_decision(
        t,bars,CONFIRM,ADVERSE,CHASE,EXPIRY
    )
    if status!="ENTER":
        return {"status":status,"entered":False}

    initial_stop,stop_source=get_initial_stop(t)
    if initial_stop is None:
        return {"status":"NO_INITIAL_STOP","entered":False}

    direction=t["direction"]
    qty=get_qty(t)
    tick=get_tick(t)

    if direction=="BUY" and initial_stop>=entry_px:
        return {"status":"INVALID_INITIAL_STOP","entered":False,
                "entry_price":entry_px,"initial_stop":initial_stop}
    if direction=="SELL" and initial_stop<=entry_px:
        return {"status":"INVALID_INITIAL_STOP","entered":False,
                "entry_price":entry_px,"initial_stop":initial_stop}

    three=resample_3m(bars)
    current_stop=float(initial_stop)
    developed=False
    development_reason=None
    stop_updates=0

    day=entry_ts.date()
    sq=pd.Timestamp.combine(day,squareoff)
    if entry_ts.tzinfo is not None:
        sq=sq.tz_localize(entry_ts.tzinfo)

    path=bars[(bars.ts>=entry_ts.floor("min")) & (bars.ts<=sq)].copy()
    if path.empty:
        return {"status":"NO_POST_ENTRY_DATA","entered":False}

    exit_px=None;exit_ts=None;exit_reason=None
    last_state_bar=None

    for _,bar in path.iterrows():
        mts=bar.ts

        # Apply logical-state update only from fully completed 3m candles
        # available before this minute begins.
        cutoff=last_completed_3m_time(mts)
        completed=three[three["date"]<=cutoff]
        if not completed.empty:
            newest=completed.iloc[-1]["date"]
            if last_state_bar is None or newest>last_state_bar:
                state=el_bethel.exit_v2_state(
                    completed,
                    direction,
                    entry_price=float(entry_px),
                    initial_logical_stop=float(initial_stop),
                    current_stop=float(current_stop),
                    last_price=float(completed.iloc[-1]["close"]),
                    already_developed=bool(developed),
                    entry_time=entry_ts,
                    tick_size=tick,
                )
                developed=bool(state["profit_developed"])
                if state.get("development_reason"):
                    development_reason=state.get("development_reason")
                ns=float(state["logical_stop"])
                if abs(ns-current_stop)>1e-12:
                    current_stop=ns
                    stop_updates+=1
                last_state_bar=newest

        trigger=float(el_bethel.exit_v2_broker_trigger(
            current_stop,direction,tick_size=tick,offset_ticks=2
        ))

        fill=stop_fill(direction,bar,trigger)
        if fill is not None:
            exit_px=float(fill)
            exit_ts=mts
            exit_reason="BROKER_PROTECTIVE_TRIGGER"
            break

    if exit_px is None:
        eligible=path[path.ts<=sq]
        if eligible.empty:
            return {"status":"NO_SQUAREOFF_BAR","entered":False}
        b=eligible.iloc[-1]
        exit_px=float(b.close)
        exit_ts=b.ts
        exit_reason="SQUAREOFF"

    gross=((exit_px-entry_px)*qty if direction=="BUY"
           else (entry_px-exit_px)*qty)
    costs=estimate_cost(float(entry_px),exit_px,qty)
    return {
        "status":"REPLAYED","entered":True,
        "entry_time":str(entry_ts),"entry_price":round(float(entry_px),6),
        "exit_time":str(exit_ts),"exit_price":round(exit_px,6),
        "exit_reason":exit_reason,
        "qty":qty,"initial_stop":round(float(initial_stop),6),
        "stop_source":stop_source,"final_logical_stop":round(float(current_stop),6),
        "profit_developed":developed,
        "development_reason":development_reason,
        "stop_updates":stop_updates,
        "gross":round(gross,2),"costs":round(costs,2),"net":round(gross-costs,2),
    }

def metrics(rows):
    x=[r for r in rows if r.get("entered")]
    return {
        "trades":len(x),
        "wins":sum(r["net"]>0 for r in x),
        "losses":sum(r["net"]<0 for r in x),
        "gross":round(sum(r["gross"] for r in x),2),
        "costs":round(sum(r["costs"] for r in x),2),
        "net":round(sum(r["net"] for r in x),2),
    }

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--trade-history",default="trade_history.jsonl")
    ap.add_argument("--out",default="runtime/v21_weekend_research/e2e_replay")
    ap.add_argument("--squareoff",default="15:08")
    args=ap.parse_args()
    hh,mm=map(int,args.squareoff.split(":"))
    sq=dtime(hh,mm)

    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    trades=load_trades(Path(args.trade_history))
    results=[]
    for t in trades:
        got=discover_1m(t["symbol"],t["date"],t["exchange"])
        if not got:
            results.append({**{k:t[k] for k in ("date","symbol","exchange","direction")},
                            "status":"NO_1M_DATA","entered":False})
            continue
        p,df,tcol,ts=got
        bars=norm_ohlc(df,tcol,ts)
        if bars is None:
            results.append({**{k:t[k] for k in ("date","symbol","exchange","direction")},
                            "status":"BAD_1M_DATA","entered":False})
            continue
        rr=replay_one(t,bars,sq)
        rr.update({k:t[k] for k in ("date","symbol","exchange","direction")})
        rr["historical_entry"]=t["entry"]
        rr["historical_exit"]=t["exit"]
        rr["historical_gross"]=t["gross_pnl"]
        results.append(rr)

    pd.DataFrame(results).to_csv(out/"trades.csv",index=False)
    replayed=[r for r in results if r.get("status")=="REPLAYED"]
    by_day=[]
    for d in sorted({r["date"] for r in replayed}):
        m=metrics([r for r in replayed if r["date"]==d])
        by_day.append({"date":d,**m})
    pd.DataFrame(by_day).to_csv(out/"per_day.csv",index=False)

    m=metrics(replayed)
    status_counts=pd.Series([r["status"] for r in results]).value_counts().to_dict()
    summary={
        "rule":{"confirm_pct":CONFIRM,"adverse_pct":ADVERSE,
                "chase_pct":CHASE,"expiry_minutes":EXPIRY},
        "squareoff":args.squareoff,
        "source_trades":len(trades),
        "status_counts":status_counts,
        "metrics":m,
        "caveats":[
            "Uses 1-minute OHLC, not tick-by-tick path.",
            "Initial structural logical stop is frozen from the original approved candidate.",
            "EXIT_V2 state/trailing is recalculated from completed 3-minute candles.",
            "Broker protective-stop fill is conservatively modeled at trigger or worse gap-open.",
            "Charges are estimated with the repository equity-intraday cost model.",
        ],
    }
    (out/"summary.json").write_text(json.dumps(summary,indent=2,default=str))

    print("===== E2E SUMMARY =====")
    print(json.dumps(summary,indent=2,default=str))
    print("\n===== REPLAYED TRADES =====")
    if replayed:
        cols=["date","symbol","direction","entry_price","initial_stop","exit_price",
              "exit_reason","profit_developed","development_reason","stop_updates",
              "gross","costs","net"]
        print(pd.DataFrame(replayed)[cols].to_string(index=False))
    print("\n===== PER DAY =====")
    print(pd.DataFrame(by_day).to_string(index=False))
    print("\nOUTPUT_DIR =",out)
    print("OFFLINE_ONLY = TRUE")
    print("NO_BROKER_CALLS = TRUE")

if __name__=="__main__":
    main()
