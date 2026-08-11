#!/usr/bin/env python3
"""Paper-only momentum + volume-famine selector with full decision audit."""
from __future__ import annotations
import argparse,json,math,os,tempfile,time
from dataclasses import asdict,dataclass
from datetime import datetime,timedelta
from pathlib import Path
from typing import Any,Iterable
from zoneinfo import ZoneInfo
import config as cfg
from auth import get_kite_client
from auto_watchlist import download_nifty500,usable_nse_equity_instruments

IST=ZoneInfo("Asia/Kolkata"); PROJECT_DIR=Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH=PROJECT_DIR/"user_config.json"; DEFAULT_RUNTIME_DIR=PROJECT_DIR/"runtime"/"paper_watchlist"
DEFAULT_REPORT_PATH=DEFAULT_RUNTIME_DIR/"latest_report.json"; DEFAULT_OUTPUT_PATH=DEFAULT_RUNTIME_DIR/"latest_watchlist.json"
DEFAULT_AUDIT_PATH=DEFAULT_RUNTIME_DIR/"selection_audit.jsonl"; QUOTE_BATCH_SIZE=500; QUOTE_BATCH_DELAY_SECONDS=1.10
class PaperWatchlistError(RuntimeError): pass
@dataclass(frozen=True)
class PaperSelectorSettings:
    momentum_min_pct:float=.75; famine_rvol_min:float=.40; famine_rvol_max:float=.70; baseline_days:int=20
    historical_lookback_days:int=45; historical_delay_seconds:float=.36; earliest_famine_time:str="09:27"; top_n:int=60

def pf(v,d=0.0):
    try:r=float(v); return r if math.isfinite(r) else d
    except:return d
def pi(v,d=0):
    try:return max(int(v),0)
    except:return d
def chunks(v,s):
    for i in range(0,len(v),s):yield v[i:i+s]
def now_ist():return datetime.now(IST)
def atomic_write_json(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True); fd,name=tempfile.mkstemp(prefix=f".{path.name}.",suffix=".tmp",dir=str(path.parent)); tmp=Path(name)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as h: json.dump(payload,h,indent=2,ensure_ascii=False); h.write("\n"); h.flush(); os.fsync(h.fileno())
        os.replace(tmp,path)
    except Exception: tmp.unlink(missing_ok=True); raise
def append_jsonl(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("a",encoding="utf-8") as h:h.write(json.dumps(payload,ensure_ascii=False,default=str)+"\n")
def elapsed_session_fraction(now):
    a=now.replace(hour=9,minute=15,second=0,microsecond=0); b=now.replace(hour=15,minute=30,second=0,microsecond=0)
    return min(max((now-a).total_seconds()/max((b-a).total_seconds(),1),0),1)
def after_earliest_famine_time(now,hhmm):
    h,m=map(int,hhmm.split(":",1)); return now>=now.replace(hour=h,minute=m,second=0,microsecond=0)
def fetch_quotes(kite,keys):
    out={}
    for i,b in enumerate(chunks(keys,QUOTE_BATCH_SIZE)):
        if i:time.sleep(QUOTE_BATCH_DELAY_SECONDS)
        r=kite.quote(b)
        if not isinstance(r,dict):raise PaperWatchlistError("Kite quote response was not a dictionary")
        out.update(r)
    return out
def average_daily_volume(kite,token,s):
    today=now_ist().date(); c=kite.historical_data(token,today-timedelta(days=s.historical_lookback_days),today-timedelta(days=1),"day",continuous=False,oi=False)
    vols=[pi(x.get("volume")) for x in c if isinstance(x,dict) and pi(x.get("volume"))>0][-s.baseline_days:]
    return sum(vols)/len(vols) if vols else 0.0

def evaluate_candidate(row,quote,avg,fraction,s):
    o=quote.get("ohlc") or {}; lp=pf(quote.get("last_price")); vol=pi(quote.get("volume")); prev=pf(o.get("close")); high=pf(o.get("high")); low=pf(o.get("low")); opn=pf(o.get("open"))
    rec={"symbol":row["symbol"],"exchange":"NSE","instrument_token":pi(row.get("instrument_token")),"last_price":lp,"open":opn,"high":high,"low":low,"previous_close":prev,"current_volume":vol,"average_daily_volume":round(avg,2),"session_fraction":round(fraction,6),"thresholds":{"momentum_min_pct":s.momentum_min_pct,"famine_rvol_min":s.famine_rvol_min,"famine_rvol_max":s.famine_rvol_max}}
    reasons=[]
    if lp<=0:reasons.append("INVALID_LAST_PRICE")
    if prev<=0:reasons.append("INVALID_PREVIOUS_CLOSE")
    if vol<=0:reasons.append("INVALID_OR_ZERO_VOLUME")
    if high<=0 or low<=0:reasons.append("INVALID_DAY_RANGE")
    if avg<=0:reasons.append("NO_HISTORICAL_VOLUME_BASELINE")
    if fraction<=0:reasons.append("INVALID_SESSION_FRACTION")
    if reasons: rec.update({"selected":False,"decision":"REJECT","rejection_reasons":reasons}); return rec
    change=((lp-prev)/prev)*100; rng=((high-low)/prev)*100; momentum=max(abs(change),rng); expected=avg*fraction; rvol=vol/expected if expected>0 else 0
    rec.update({"change_pct":round(change,4),"day_range_pct":round(rng,4),"momentum_pct":round(momentum,4),"expected_volume_now":round(expected,2),"relative_volume":round(rvol,4),"momentum_pass":momentum>=s.momentum_min_pct,"famine_pass":s.famine_rvol_min<=rvol<=s.famine_rvol_max})
    if momentum<s.momentum_min_pct:reasons.append("MOMENTUM_BELOW_THRESHOLD")
    if rvol<s.famine_rvol_min:reasons.append("RVOL_BELOW_FAMINE_BAND")
    elif rvol>s.famine_rvol_max:reasons.append("RVOL_ABOVE_FAMINE_BAND")
    if reasons: rec.update({"selected":False,"decision":"REJECT","rejection_reasons":reasons}); return rec
    mid=(s.famine_rvol_min+s.famine_rvol_max)/2; width=max((s.famine_rvol_max-s.famine_rvol_min)/2,1e-9); quality=max(0,1-abs(rvol-mid)/width); score=momentum*(1+quality)
    rec.update({"selected":True,"decision":"SELECT","selection_reasons":["MOMENTUM_PASS","VOLUME_FAMINE_PASS"],"famine":True,"high_momentum":True,"score":round(score,6)})
    return rec

def generate_selection(kite,s,audit_path):
    if not bool(getattr(cfg,"PAPER_TRADING",False)):raise PaperWatchlistError("Refusing to run paper selector because PAPER_TRADING is not True")
    now=now_ist()
    if not after_earliest_famine_time(now,s.earliest_famine_time):raise PaperWatchlistError(f"Volume-famine selection is disabled before {s.earliest_famine_time} IST")
    fraction=elapsed_session_fraction(now); universe=download_nifty500(); imap=usable_nse_equity_instruments(kite.instruments("NSE")); matched=[]; unmatched=[]
    for row in universe:
        inst=imap.get(row["symbol"])
        if inst is None: unmatched.append({"symbol":row["symbol"],"selected":False,"decision":"REJECT","rejection_reasons":["NOT_USABLE_NSE_EQUITY"]}); continue
        token=pi(inst.get("instrument_token"))
        if token<=0: unmatched.append({"symbol":row["symbol"],"selected":False,"decision":"REJECT","rejection_reasons":["INVALID_INSTRUMENT_TOKEN"]}); continue
        matched.append({**row,"instrument_token":token})
    quotes=fetch_quotes(kite,[f"NSE:{r['symbol']}" for r in matched]); decisions=list(unmatched); baseline_failures=0
    for i,row in enumerate(matched):
        q=quotes.get(f"NSE:{row['symbol']}")
        if not isinstance(q,dict): decisions.append({"symbol":row["symbol"],"selected":False,"decision":"REJECT","rejection_reasons":["QUOTE_MISSING"]}); continue
        try:baseline=average_daily_volume(kite,pi(row.get("instrument_token")),s)
        except Exception as e: baseline_failures+=1; baseline=0; q=dict(q); q["baseline_error"]=str(e)
        decisions.append(evaluate_candidate(row,q,baseline,fraction,s))
        if i+1<len(matched):time.sleep(s.historical_delay_seconds)
    eligible=[d for d in decisions if d.get("selected")]; eligible.sort(key=lambda x:(-pf(x.get("score")),-pf(x.get("momentum_pct")),x["symbol"])); selected=eligible[:s.top_n]; selected_symbols={x["symbol"] for x in selected}
    for d in decisions:
        if d.get("selected") and d["symbol"] not in selected_symbols: d.update({"selected":False,"decision":"REJECT","rejection_reasons":["QUALIFIED_BUT_OUTSIDE_TOP_N"]})
    batch={"event":"WATCHLIST_SELECTION_RUN","generated_at":now.isoformat(timespec="seconds"),"settings":asdict(s),"statistics":{"universe_rows":len(universe),"matched_symbols":len(matched),"quotes_received":len(quotes),"baseline_failures":baseline_failures,"qualified_before_cap":len(eligible),"selected_symbols":len(selected)},"decisions":decisions}
    append_jsonl(audit_path,batch)
    return {"status":"success","paper_only":True,"generated_at":now.isoformat(timespec="seconds"),"strategy":"HIGH_MOMENTUM_AND_VOLUME_FAMINE_ONLY","settings":asdict(s),"statistics":{**batch["statistics"],"session_fraction":round(fraction,6)},"selected":selected,"all_decisions":decisions}
def write_watchlist(path,selected):
    if not bool(getattr(cfg,"PAPER_TRADING",False)):raise PaperWatchlistError("Refusing write because PAPER_TRADING is not True")
    payload=json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}; payload["watchlist"]=[{"symbol":x["symbol"],"exchange":"NSE"} for x in selected]; atomic_write_json(path,payload)
def parse_args():
    p=argparse.ArgumentParser(); p.add_argument("--write",action="store_true"); p.add_argument("--momentum-min-pct",type=float,default=.75); p.add_argument("--famine-rvol-min",type=float,default=.40); p.add_argument("--famine-rvol-max",type=float,default=.70); p.add_argument("--baseline-days",type=int,default=20); p.add_argument("--historical-lookback-days",type=int,default=45); p.add_argument("--historical-delay-seconds",type=float,default=.36); p.add_argument("--earliest-famine-time",default="09:27"); p.add_argument("--top",type=int,default=60); p.add_argument("--config",type=Path,default=DEFAULT_CONFIG_PATH); p.add_argument("--report",type=Path,default=DEFAULT_REPORT_PATH); p.add_argument("--output",type=Path,default=DEFAULT_OUTPUT_PATH); p.add_argument("--audit",type=Path,default=DEFAULT_AUDIT_PATH); return p.parse_args()
def main():
    a=parse_args(); s=PaperSelectorSettings(a.momentum_min_pct,a.famine_rvol_min,a.famine_rvol_max,a.baseline_days,a.historical_lookback_days,a.historical_delay_seconds,a.earliest_famine_time,a.top)
    try:
        r=generate_selection(get_kite_client(),s,a.audit); atomic_write_json(a.report,r); atomic_write_json(a.output,{"status":"success","generated_at":r["generated_at"],"watchlist":[{"symbol":x["symbol"],"exchange":"NSE"} for x in r["selected"]],"selected_details":r["selected"]})
        print("PAPER WATCHLIST: momentum + famine only"); print("Selected:",r["statistics"]["selected_symbols"]); print("Audited decisions:",len(r["all_decisions"]));
        for x in r["selected"]:print(f"{x['symbol']:<14} momentum={x['momentum_pct']:>7.3f}% rvol={x['relative_volume']:>6.3f} score={x['score']:>8.3f}")
        if a.write:write_watchlist(a.config,r["selected"]); print("Paper watchlist written to:",a.config)
        return 0
    except Exception as e:
        f={"status":"failed","paper_only":True,"generated_at":now_ist().isoformat(timespec="seconds"),"error_type":type(e).__name__,"error":str(e),"configuration_changed":False}; atomic_write_json(a.report,f); append_jsonl(a.audit,{"event":"WATCHLIST_SELECTION_FAILURE",**f}); print("PAPER WATCHLIST FAILED\nError:",e); return 1
if __name__=="__main__":raise SystemExit(main())
