#!/usr/bin/env python3
"""Read-only VWAP + EMA(9/20/50) pullback replay for retained Kite days.

Universe modes:
  history: symbols retained in trade_history.jsonl for each date (selection-biased)
  watchlist: selected symbols in correctly dated historical watchlist JSON files

No imports from strategy/config/risk modules and no order/state writes.
"""
from __future__ import annotations

import argparse, json, math, time
from collections import Counter, defaultdict
from pathlib import Path
import pandas as pd

from auth import get_kite_client
from costs import net_pnl_for_trade

IST = "Asia/Kolkata"
ENTRY_MINUTES = 3

def args():
    p=argparse.ArgumentParser()
    p.add_argument("--mode",choices=("history","watchlist"),default="history")
    p.add_argument("--history",default="trade_history.jsonl")
    p.add_argument("--watchlist-glob",default="runtime/historical_all_nse_watchlist_*_0927.json")
    p.add_argument("--from-date"); p.add_argument("--to-date")
    p.add_argument("--capital",type=float,default=5000.0)
    p.add_argument("--risk-pct",type=float,default=0.20)
    p.add_argument("--rr",type=float,default=1.5)
    p.add_argument("--touch-pct",type=float,default=0.15)
    p.add_argument("--volume-ratio",type=float,default=1.0)
    p.add_argument("--max-vwap-crosses",type=int,default=2)
    p.add_argument("--max-trades-day",type=int,default=100)
    p.add_argument("--max-trades-symbol",type=int,default=2)
    p.add_argument("--output",default="runtime/isolated_vwap_ema_all_days.json")
    return p.parse_args()

def ts(x):
    v=pd.Timestamp(x)
    return v.tz_localize(IST) if v.tzinfo is None else v.tz_convert(IST)

def retained_history(path):
    out=defaultdict(set)
    for raw in Path(path).open(errors="replace"):
        try: r=json.loads(raw)
        except Exception: continue
        d=str(r.get("date") or ""); s=str(r.get("symbol") or "").strip()
        if d and s: out[d].add(s)
    return out, {d:"retained_trade_symbols_selection_biased" for d in out}

def valid_watchlists(pattern):
    out=defaultdict(set); labels={}; rejected={}
    for path in sorted(Path().glob(pattern)):
        try: doc=json.loads(path.read_text())
        except Exception as e: rejected[str(path)]=f"JSON:{e}"; continue
        d=str(doc.get("session_date") or doc.get("date") or "")
        cutoff=str(doc.get("cutoff") or "")
        try: cutoff_date=str(ts(cutoff).date())
        except Exception: cutoff_date=""
        # Hard integrity gate: filename/session and point-in-time cutoff must agree.
        if not d or cutoff_date != d:
            rejected[str(path)]=f"MISDATED session={d} cutoff={cutoff}"
            continue
        rows=doc.get("qualified") or doc.get("selected") or []
        selected_count=int(doc.get("selected_count") or len(rows))
        rows=rows[:selected_count]
        for r in rows:
            if bool(r.get("selected",True)) and r.get("symbol"): out[d].add(str(r["symbol"]))
        labels[d]="genuine_point_in_time_watchlist"
    return out,labels,rejected

def fetch(kite,token,start,end):
    last=None
    for n in range(3):
        try:
            f=pd.DataFrame(kite.historical_data(token,start,end,"3minute"))
            if f.empty:return f
            f["date"]=pd.to_datetime(f["date"])
            if f.date.dt.tz is None:f["date"]=f.date.dt.tz_localize(IST)
            else:f["date"]=f.date.dt.tz_convert(IST)
            return f[["date","open","high","low","close","volume"]].sort_values("date").reset_index(drop=True)
        except Exception as e:last=e;time.sleep(2**n)
    raise RuntimeError(last)

def indicators(f):
    f=f.copy(); c=f.close.astype(float)
    for n in (9,20,50): f[f"ema{n}"]=c.ewm(span=n,adjust=False).mean()
    f["volavg20"]=f.volume.astype(float).rolling(20).mean().shift(1)
    session=f.date.dt.date
    typical=(f.high+f.low+f.close)/3.0
    f["vwap"]=(typical*f.volume).groupby(session).cumsum()/f.volume.groupby(session).cumsum().replace(0,float("nan"))
    side=(f.close-f.vwap).apply(lambda x: 1 if x>0 else (-1 if x<0 else 0))
    f["vwap_cross"]=(side!=side.shift()).astype(int)
    f["cross10"]=f.vwap_cross.rolling(10).sum()
    return f

def signal_at(f,i,a):
    if i<53:return None
    p=f.iloc[i-1]; c=f.iloc[i]
    vals=[p.ema9,p.ema20,p.ema50,p.vwap,c.ema9,c.ema20,c.ema50,c.vwap,c.volavg20]
    if any(pd.isna(x) for x in vals) or c.cross10>a.max_vwap_crosses:return None
    up=c.ema9>c.ema20>c.ema50 and c.ema9>f.iloc[i-3].ema9 and c.ema20>f.iloc[i-3].ema20 and c.ema50>f.iloc[i-3].ema50
    dn=c.ema9<c.ema20<c.ema50 and c.ema9<f.iloc[i-3].ema9 and c.ema20<f.iloc[i-3].ema20 and c.ema50<f.iloc[i-3].ema50
    tol=a.touch_pct/100.0
    near=lambda price,level: abs(price-level)/level<=tol
    touched=near(p.low,p.vwap) or near(p.low,p.ema9) or near(p.low,p.ema20)
    touched_s=near(p.high,p.vwap) or near(p.high,p.ema9) or near(p.high,p.ema20)
    vol_ok=float(c.volume)>=float(c.volavg20)*a.volume_ratio
    if up and p.close>=p.vwap and c.close>c.open and c.close>p.high and c.close>c.vwap and touched and vol_ok:return "BUY"
    if dn and p.close<=p.vwap and c.close<c.open and c.close<p.low and c.close<c.vwap and touched_s and vol_ok:return "SELL"
    return None

def simulate(f,i,d,a):
    if i+1>=len(f):return None
    pb=f.iloc[i-1]; conf=f.iloc[i]; nxt=f.iloc[i+1]
    if nxt.date.date()!=conf.date.date():return None
    entry=float(nxt.open)
    raw_stop=min(float(pb.low),float(conf.low)) if d=="BUY" else max(float(pb.high),float(conf.high))
    risk=entry-raw_stop if d=="BUY" else raw_stop-entry
    if risk<=0 or risk/entry<0.0005 or risk/entry>0.01:return None
    target=entry+a.rr*risk if d=="BUY" else entry-a.rr*risk
    risk_rupees=a.capital*a.risk_pct/100.0
    qty=int(risk_rupees/risk)
    if qty<1:return None
    qty=min(qty,int(a.capital/entry))
    if qty<1:return None
    end=ts(str(conf.date.date())+" 15:08")
    exit_price=None;reason=None;exit_time=None
    for j in range(i+1,len(f)):
        r=f.iloc[j]
        if r.date.date()!=conf.date.date():break
        stop_hit=float(r.low)<=raw_stop if d=="BUY" else float(r.high)>=raw_stop
        target_hit=float(r.high)>=target if d=="BUY" else float(r.low)<=target
        if stop_hit:exit_price=raw_stop;reason="STOP";exit_time=r.date;break
        if target_hit:exit_price=target;reason="TARGET";exit_time=r.date;break
        if r.date+pd.Timedelta(minutes=3)>=end:exit_price=float(r.close);reason="SQUARE_OFF";exit_time=r.date;break
    if exit_price is None:return None
    cost=net_pnl_for_trade(d,qty,entry,exit_price)
    return {"direction":d,"signal_time":str(conf.date),"entry_time":str(nxt.date),"exit_time":str(exit_time),"entry":entry,"stop":raw_stop,"target":target,"exit":exit_price,"qty":qty,"reason":reason,**cost}

def main():
    a=args()
    if a.mode=="history": universes,labels=retained_history(a.history); rejected={}
    else: universes,labels,rejected=valid_watchlists(a.watchlist_glob)
    universes={d:s for d,s in universes.items() if (not a.from_date or d>=a.from_date) and (not a.to_date or d<=a.to_date)}
    if not universes:raise SystemExit("No valid date/universe pairs")
    kite=get_kite_client(); tokens={str(x.get("tradingsymbol")):int(x["instrument_token"]) for x in kite.instruments("NSE") if x.get("instrument_type")=="EQ"}
    pairs=[(d,s) for d in sorted(universes) for s in sorted(universes[d])]
    frames={};failures={}
    for n,(d,s) in enumerate(pairs,1):
        token=tokens.get(s)
        if token is None:failures[f"{d}:{s}"]="TOKEN";continue
        try:
            start=ts(d+" 09:15")-pd.Timedelta(days=10);end=ts(d+" 15:30")
            frames[(d,s)]=indicators(fetch(kite,token,start.to_pydatetime(),end.to_pydatetime()))
            print(f"FETCH {n}/{len(pairs)} {d} NSE:{s} candles={len(frames[(d,s)])}")
            time.sleep(.35)
        except Exception as e:failures[f"{d}:{s}"]=str(e)
    trades=[];rejections=Counter()
    for d in sorted(universes):
        day=[];symcount=Counter()
        for s in sorted(universes[d]):
            f=frames.get((d,s))
            if f is None:continue
            idx=f.index[(f.date.dt.strftime("%Y-%m-%d")==d)&(f.date.dt.time>=pd.Timestamp("09:27").time())&(f.date.dt.time<=pd.Timestamp("15:00").time())]
            for i in idx:
                direction=signal_at(f,i,a)
                if not direction:continue
                if len(day)>=a.max_trades_day:rejections["DAILY_CAP"]+=1;break
                if symcount[s]>=a.max_trades_symbol:rejections["SYMBOL_CAP"]+=1;continue
                t=simulate(f,i,direction,a)
                if not t:rejections["INVALID_SIZE_STOP_OR_EXIT"]+=1;continue
                t.update(date=d,symbol=s,universe_label=labels[d]);day.append(t);symcount[s]+=1
        trades.extend(day)
    byday={}
    for d in sorted(universes):
        rows=[t for t in trades if t["date"]==d];w=sum(t["net_pnl"]>0 for t in rows)
        byday[d]={"symbols":len(universes[d]),"trades":len(rows),"wins":w,"losses":len(rows)-w,"gross_pnl":sum(t["gross_pnl"] for t in rows),"costs":sum(t["costs"] for t in rows),"net_pnl":sum(t["net_pnl"] for t in rows)}
    wins=sum(t["net_pnl"]>0 for t in trades); net=sum(t["net_pnl"] for t in trades)
    result={"read_only":True,"strategy":"VWAP_EMA9_20_50_PULLBACK_CONFIRMATION","mode":a.mode,"limitations":["history mode is selection-biased","watchlist files rejected unless cutoff date equals session date","next-candle-open entry","same-candle ambiguity stop-first","estimated Zerodha equity MIS costs"],"parameters":vars(a),"rejected_watchlists":rejected,"fetch_failures":failures,"rejections":dict(rejections),"summary":{"dates":len(universes),"trades":len(trades),"wins":wins,"losses":len(trades)-wins,"win_rate":wins/len(trades)*100 if trades else 0,"gross_pnl":sum(t["gross_pnl"] for t in trades),"costs":sum(t["costs"] for t in trades),"net_pnl":net},"by_day":byday,"trades":trades}
    out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,indent=2,default=str))
    print(json.dumps({"summary":result["summary"],"by_day":byday,"rejected_watchlists":rejected},indent=2));print("DETAIL=",out)

if __name__=="__main__":main()
