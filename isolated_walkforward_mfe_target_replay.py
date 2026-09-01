#!/usr/bin/env python3
"""Walk-forward, read-only MFE target estimator for the strict PAPER setup.

Training examples are point-in-time technical signals from strictly earlier
dates. The live candidate policy remains 09:27-09:59 with ADX >= 30.
Targets are selected from a grid by estimated net expectancy. No orders or
paper/live state are read or written.
"""
from __future__ import annotations
import argparse,json,math,time
from collections import Counter,defaultdict
from pathlib import Path
from types import SimpleNamespace
import pandas as pd
from auth import get_kite_client
from costs import net_pnl_for_trade
from isolated_vwap_ema_replay import retained_history,valid_watchlists,fetch,ts
from isolated_current_paper_workflow_replay import enrich

def cli():
 p=argparse.ArgumentParser()
 p.add_argument('--mode',choices=('history','watchlist'),default='history')
 p.add_argument('--history',default='trade_history.jsonl');p.add_argument('--watchlist-glob',default='runtime/historical_all_nse_watchlist_*_0927.json')
 p.add_argument('--capital',type=float,default=5000);p.add_argument('--risk-pct',type=float,default=.20)
 p.add_argument('--stop-pct',type=float,default=.45);p.add_argument('--max-trades-day',type=int,default=3);p.add_argument('--max-open',type=int,default=2)
 p.add_argument('--min-training',type=int,default=15);p.add_argument('--neighbors',type=int,default=20);p.add_argument('--min-hit-prob',type=float,default=.35)
 p.add_argument('--target-min',type=float,default=.5);p.add_argument('--target-max',type=float,default=3.0);p.add_argument('--target-step',type=float,default=.1)
 p.add_argument('--output',default='runtime/isolated_walkforward_mfe_target.json');return p.parse_args()

def broad_signal(f,i):
 if i<53:return None
 r=f.iloc[i];p=f.iloc[i-1];old=f.iloc[i-3]
 vals=(r.ema9,r.ema21,r.ema50,r.vwap,r.adx14,r.rvol,r.acceleration,r.atr14)
 if any(pd.isna(x) for x in vals):return None
 buy=r.ema9>r.ema21>r.ema50 and r.ema9>old.ema9 and r.ema21>old.ema21 and r.ema50>old.ema50 and r.close>r.vwap
 sell=r.ema9<r.ema21<r.ema50 and r.ema9<old.ema9 and r.ema21<old.ema21 and r.ema50<old.ema50 and r.close<r.vwap
 if not(buy or sell):return None
 d='BUY' if buy else 'SELL'
 if r.adx14<(25 if d=='BUY' else 20) or not(.70<=r.rvol<1.0) or r.acceleration<1.10:return None
 span=float(r.high-r.low);body=abs(float(r.close-r.open))/span if span>0 else 0
 if body<.50:return None
 ext=abs(float(r.close-r.ema9))/float(r.atr14) if r.atr14>0 else 999
 if ext>1.50:return None
 breakout=float(r.close)>float(p.high) if d=='BUY' else float(r.close)<float(p.low)
 clv=float(r.close-r.low)/span if d=='BUY' else float(r.high-r.close)/span
 if sum((breakout,clv>=.65,r.acceleration>=1.10))<2:return None
 return {'direction':d,'signal_time':r.date,'adx':float(r.adx14),'rvol':float(r.rvol),'body_ratio':body,'acceleration':float(r.acceleration),'extension_atr':ext,'minute':r.date.hour*60+r.date.minute}

def forward_path(f,i,d,stop_pct):
 if i+1>=len(f):return None
 sig=f.iloc[i];nxt=f.iloc[i+1]
 if nxt.date.date()!=sig.date.date():return None
 entry=float(nxt.open);stop=entry*(1-stop_pct/100) if d=='BUY' else entry*(1+stop_pct/100)
 end=ts(str(sig.date.date())+' 15:08');mfe=0.0;bars=[];stop_time=None
 for j in range(i+1,len(f)):
  r=f.iloc[j]
  if r.date.date()!=sig.date.date():break
  stop_hit=float(r.low)<=stop if d=='BUY' else float(r.high)>=stop
  # Conservative: when the stop is inside this candle, do not use its favorable extreme.
  if stop_hit:stop_time=r.date;bars.append({'time':r.date,'stop':True,'fav':mfe,'close':float(r.close)});break
  fav=(float(r.high)-entry)/entry*100 if d=='BUY' else (entry-float(r.low))/entry*100
  mfe=max(mfe,fav);bars.append({'time':r.date,'stop':False,'fav':mfe,'close':float(r.close)})
  if r.date+pd.Timedelta(minutes=3)>=end:break
 if not bars:return None
 return {'entry_time':nxt.date,'entry':entry,'stop':stop,'mfe_pct':mfe,'bars':bars,'stop_time':stop_time}

def target_outcome(example,target):
 entry=example['entry'];d=example['direction'];price=entry*(1+target/100) if d=='BUY' else entry*(1-target/100)
 for b in example['bars']:
  if b['stop']:return 'STOP',example['stop'],b['time']
  if b['fav']+1e-12>=target:return 'TARGET',price,b['time']
 b=example['bars'][-1];return 'TIMEOUT',b['close'],b['time']

def distance(a,b):
 if a['direction']!=b['direction']:return 9e9
 def z(x,scale):return x/scale
 return math.sqrt(z(a['minute']-b['minute'],60)**2+z(a['adx']-b['adx'],20)**2+z(a['rvol']-b['rvol'],.15)**2+z(a['body_ratio']-b['body_ratio'],.25)**2+z(math.log1p(a['acceleration'])-math.log1p(b['acceleration']),.7)**2+z(a['extension_atr']-b['extension_atr'],.5)**2)

def quantile(values,q):
 if not values:return 0
 s=sorted(values);pos=(len(s)-1)*q;lo=int(pos);hi=min(lo+1,len(s)-1);return s[lo]+(s[hi]-s[lo])*(pos-lo)

def predict(candidate,training,a,qty):
 same=[x for x in training if x['direction']==candidate['direction']]
 if len(same)<a.min_training:return None
 near=sorted(same,key=lambda x:distance(candidate,x))[:min(a.neighbors,len(same))]
 targets=[];nsteps=int(round((a.target_max-a.target_min)/a.target_step))+1
 loss=net_pnl_for_trade(candidate['direction'],qty,candidate['entry'],candidate['stop'])['net_pnl']
 for k in range(nsteps):
  target=round(a.target_min+k*a.target_step,10);outs=[target_outcome(x,target) for x in near]
  hits=sum(o[0]=='TARGET' for o in outs);stops=sum(o[0]=='STOP' for o in outs);timeouts=len(outs)-hits-stops
  # Mild Beta/Dirichlet smoothing prevents tiny neighborhoods from producing certainty.
  ph=(hits+1)/(len(outs)+3);ps=(stops+1)/(len(outs)+3);pto=(timeouts+1)/(len(outs)+3)
  target_price=candidate['entry']*(1+target/100) if candidate['direction']=='BUY' else candidate['entry']*(1-target/100)
  win=net_pnl_for_trade(candidate['direction'],qty,candidate['entry'],target_price)['net_pnl']
  timeout_returns=[]
  for x,o in zip(near,outs):
   if o[0]=='TIMEOUT':
    if x['direction']=='BUY':timeout_returns.append((o[1]-x['entry'])/x['entry'])
    else:timeout_returns.append((x['entry']-o[1])/x['entry'])
  avg_to=sum(timeout_returns)/len(timeout_returns) if timeout_returns else 0
  timeout_price=candidate['entry']*(1+avg_to) if candidate['direction']=='BUY' else candidate['entry']*(1-avg_to)
  timeout_net=net_pnl_for_trade(candidate['direction'],qty,candidate['entry'],timeout_price)['net_pnl']
  ev=ph*win+ps*loss+pto*timeout_net
  targets.append({'target_pct':target,'hit_probability':ph,'stop_probability':ps,'timeout_probability':pto,'expected_net_pnl':ev,'net_win':win})
 eligible=[x for x in targets if x['hit_probability']>=a.min_hit_prob and x['expected_net_pnl']>0]
 if not eligible:return {'chosen':None,'neighbors':len(near),'mfe_p50':quantile([x['mfe_pct'] for x in near],.5),'mfe_p75':quantile([x['mfe_pct'] for x in near],.75),'targets':targets}
 chosen=max(eligible,key=lambda x:x['expected_net_pnl'])
 return {'chosen':chosen,'neighbors':len(near),'mfe_p50':quantile([x['mfe_pct'] for x in near],.5),'mfe_p75':quantile([x['mfe_pct'] for x in near],.75),'targets':targets}

def main():
 a=cli()
 if a.mode=='history':universes,labels=retained_history(a.history);bad={}
 else:universes,labels,bad=valid_watchlists(a.watchlist_glob)
 print('READ_ONLY_REPLAY=True',flush=True);print('METHOD=WALK_FORWARD_PRIOR_DAYS_ONLY',flush=True)
 kite=get_kite_client();tokens={str(x.get('tradingsymbol')):int(x['instrument_token']) for x in kite.instruments('NSE') if x.get('instrument_type')=='EQ'}
 pairs=[(d,s) for d in sorted(universes) for s in sorted(universes[d])];frames={};fail={}
 for n,(d,s) in enumerate(pairs,1):
  try:
   token=tokens[s];start=ts(d+' 09:15')-pd.Timedelta(days=10);end=ts(d+' 15:30')
   frames[(d,s)]=enrich(fetch(kite,token,start.to_pydatetime(),end.to_pydatetime()));print(f'FETCH {n}/{len(pairs)} {d} NSE:{s}',flush=True);time.sleep(.35)
  except Exception as e:fail[f'{d}:{s}']=str(e)
 examples=[]
 for d,symbols in sorted(universes.items()):
  for s in sorted(symbols):
   f=frames.get((d,s));
   if f is None:continue
   idx=f.index[(f.date.dt.strftime('%Y-%m-%d')==d)&(f.date.dt.time>=pd.Timestamp('09:27').time())&(f.date.dt.time<=pd.Timestamp('15:00').time())]
   for i in idx:
    sig=broad_signal(f,i)
    if not sig:continue
    path=forward_path(f,i,sig['direction'],a.stop_pct)
    if path:examples.append({'date':d,'symbol':s,'universe_label':labels[d],**sig,**path})
 examples.sort(key=lambda x:(x['entry_time'],x['symbol']))
 predictions=[];eligible=[]
 for x in examples:
  # Predictor training never includes the current or a future date.
  training=[z for z in examples if z['date']<x['date']]
  execution_candidate=(567<=x['minute']<=599 and x['adx']>=30)
  if not execution_candidate:continue
  risk_rupees=a.capital*a.risk_pct/100;risk_share=x['entry']*a.stop_pct/100;qty=min(int(risk_rupees/risk_share),int(a.capital/x['entry'])) if risk_share>0 else 0
  if qty<1:continue
  pred=predict(x,training,a,qty);row={k:v for k,v in x.items() if k!='bars'};row['prediction']=pred;predictions.append(row)
  if pred and pred['chosen']:
   target=pred['chosen']['target_pct'];out=target_outcome(x,target);exit_price=out[1];pnl=net_pnl_for_trade(x['direction'],qty,x['entry'],exit_price)
   eligible.append({**row,'qty':qty,'selected_target_pct':target,'exit_reason':out[0],'exit':exit_price,'exit_time':out[2],**pnl})
 # Realistic admission using prediction-time order and existing limits.
 eligible.sort(key=lambda x:(x['entry_time'],x['symbol']));trades=[];openpos=[];daycount=Counter();symcount=Counter();rejects=Counter()
 for x in eligible:
  now=x['entry_time'];openpos=[p for p in openpos if p['exit_time']>now]
  if daycount[x['date']]>=a.max_trades_day:rejects['DAILY_CAP']+=1;continue
  if len(openpos)>=a.max_open:rejects['MAX_OPEN']+=1;continue
  if symcount[(x['date'],x['symbol'])]>=1:rejects['SYMBOL_CAP']+=1;continue
  used=sum(p['entry']*p['qty'] for p in openpos);available=a.capital-used
  risk_rupees=a.capital*a.risk_pct/100;risk_share=x['entry']*a.stop_pct/100;qty=min(int(risk_rupees/risk_share),int(available/x['entry'])) if risk_share>0 else 0
  if qty<1:rejects['CAPITAL']+=1;continue
  pnl=net_pnl_for_trade(x['direction'],qty,x['entry'],x['exit']);t={**x,'qty':qty,**pnl};trades.append(t);openpos.append(t);daycount[x['date']]+=1;symcount[(x['date'],x['symbol'])]+=1
 wins=sum(t['net_pnl']>0 for t in trades);byday={}
 for d in sorted(universes):
  rows=[t for t in trades if t['date']==d];w=sum(t['net_pnl']>0 for t in rows)
  byday[d]={'trades':len(rows),'wins':w,'losses':len(rows)-w,'net_pnl':sum(t['net_pnl'] for t in rows)}
 summary={'dates':len(universes),'training_examples':len(examples),'opening_candidates':len(predictions),'positive_ev_predictions':len(eligible),'trades':len(trades),'wins':wins,'losses':len(trades)-wins,'win_rate':wins/len(trades)*100 if trades else 0,'gross_pnl':sum(t['gross_pnl'] for t in trades),'costs':sum(t['costs'] for t in trades),'net_pnl':sum(t['net_pnl'] for t in trades),'profitable_days':sum(v['net_pnl']>0 for v in byday.values())}
 result={'read_only':True,'method':'walk-forward nearest-neighbor MFE; prior dates only','selection_bias':a.mode=='history','parameters':vars(a),'summary':summary,'by_day':byday,'rejections':dict(rejects),'fetch_failures':fail,'rejected_watchlists':bad,'predictions':predictions,'trades':trades}
 out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,indent=2,default=str));print(json.dumps({'summary':summary,'by_day':byday,'selected_targets':Counter(str(t['selected_target_pct']) for t in trades)},indent=2),flush=True);print('DETAIL=',out,flush=True)
if __name__=='__main__':main()
