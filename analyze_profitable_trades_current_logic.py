#!/usr/bin/env python3
"""Analyze profitable historical trades against reconstructed current entry gates.
Research-only: reads replay output and writes diagnostics; never touches live state.
"""
from pathlib import Path
from collections import Counter
import json, math
import numpy as np
import pandas as pd

SRC=Path('runtime/current_entry_replay_253/trade_level.csv')
OUT=Path('runtime/profitable_trade_current_logic_analysis'); OUT.mkdir(parents=True,exist_ok=True)
GATES=['direction_pass','adx_pass','vwap_pass','ema_distance_pass','structure_pass','volume_pass','atr_pass','clv_pass']
VALS=['adx14','ema_distance_atr','volume_ratio20','atr_multiple','clv','close','ema9','ema21','vwap','atr14','confirmation_count','expected_gross_proxy']

def b(v):
    if pd.isna(v): return None
    if isinstance(v,bool): return v
    return str(v).strip().lower() in {'true','1','yes'}

def qstats(s):
    s=pd.to_numeric(s,errors='coerce').dropna()
    if s.empty:return {}
    return {k:round(float(v),4) for k,v in {'min':s.min(),'p10':s.quantile(.1),'p25':s.quantile(.25),'median':s.median(),'p75':s.quantile(.75),'p90':s.quantile(.9),'max':s.max(),'mean':s.mean()}.items()}

def main():
    if not SRC.exists(): raise SystemExit(f'Missing {SRC}; run replay_current_entry_filters_253.py first')
    df=pd.read_csv(SRC)
    pnl=pd.to_numeric(df['actual_net_pnl'],errors='coerce').fillna(0)
    replay=df[df['replay_status'].eq('OK')].copy() if 'replay_status' in df else df.copy()
    rpnl=pd.to_numeric(replay['actual_net_pnl'],errors='coerce').fillna(0)
    winners=replay[rpnl>0].copy(); losers=replay[rpnl<0].copy()
    for g in GATES:
        if g in winners: winners[g]=winners[g].map(b)
    winners['fail_count']=sum((~winners[g].fillna(False)).astype(int) for g in GATES if g in winners)
    winners['failed_gates']=winners.apply(lambda r:';'.join(g.replace('_pass','').upper() for g in GATES if g in winners and r[g] is False),axis=1)
    accepted=winners[winners.get('core_pass',False).map(b) if 'core_pass' in winners else winners.fail_count.eq(0)]
    blocked=winners[~winners.index.isin(accepted.index)]

    print('===== PROFITABLE TRADE / CURRENT LOGIC ANALYSIS =====')
    print(f'All historical trades             : {len(df)}')
    print(f'Replayable trades                 : {len(replay)}')
    print(f'Replayable profitable trades      : {len(winners)}')
    print(f'Profit from replayable winners    : Rs {pd.to_numeric(winners.actual_net_pnl).sum():,.2f}')
    print(f'Current CORE accepted winners     : {len(accepted)} | Rs {pd.to_numeric(accepted.actual_net_pnl).sum():,.2f}')
    print(f'Profitable trades blocked         : {len(blocked)} | Rs {pd.to_numeric(blocked.actual_net_pnl).sum():,.2f}')
    print()
    print('===== WHICH CURRENT GATES BLOCKED HISTORICAL WINNERS =====')
    gate_rows=[]
    for g in GATES:
        if g not in winners: continue
        fail=winners[g].eq(False)
        n=int(fail.sum()); lost=float(pd.to_numeric(winners.loc[fail,'actual_net_pnl']).sum())
        gate_rows.append((g,n,lost))
        print(f'{g:24} blocked_winners={n:3d} blocked_profit=Rs {lost:9.2f}')

    print('\n===== WINNER VALUE DISTRIBUTIONS =====')
    stats={}
    for c in VALS:
        if c in winners:
            stats[c]=qstats(winners[c]); print(c, json.dumps(stats[c]))

    print('\n===== WINNERS BY NUMBER OF FAILED CURRENT GATES =====')
    fc=winners.groupby('fail_count')['actual_net_pnl'].agg(['count','sum']).reset_index()
    print(fc.to_string(index=False))

    print('\n===== SOLE-BLOCKER PROFITABLE TRADES =====')
    sole=winners[winners.fail_count.eq(1)].copy()
    if sole.empty: print('None')
    else:
        s=sole.groupby('failed_gates')['actual_net_pnl'].agg(['count','sum']).sort_values('sum',ascending=False)
        print(s.to_string())

    # Near-miss analysis for numeric gates: tells how many winners are just outside threshold.
    print('\n===== NEAR-MISS WINNERS =====')
    near=[]
    tests=[
      ('ADX','adx14',lambda x:(x<20)&(x>=18)),
      ('EMA_DISTANCE','ema_distance_atr',lambda x:(x>2)&(x<=2.25)),
      ('VOLUME','volume_ratio20',lambda x:(x<1.5)&(x>=1.3)),
      ('ATR_MULTIPLE','atr_multiple',lambda x:(x<1.2)&(x>=1.0)),
    ]
    for name,col,fn in tests:
        if col not in winners: continue
        x=pd.to_numeric(winners[col],errors='coerce'); m=fn(x)
        near.append((name,int(m.sum()),float(pd.to_numeric(winners.loc[m,'actual_net_pnl']).sum())))
    if 'clv' in winners:
        x=pd.to_numeric(winners.clv,errors='coerce'); dirs=winners.direction.astype(str).str.upper()
        m=((dirs=='BUY')&(x<.6)&(x>=.5))|((dirs=='SELL')&(x>-.6)&(x<=-.5))
        near.append(('CLV',int(m.sum()),float(pd.to_numeric(winners.loc[m,'actual_net_pnl']).sum())))
    for x in near: print(f'{x[0]:16} winners={x[1]:3d} profit=Rs {x[2]:9.2f}')

    # Save all profitable trades with every reconstructed current value for inspection.
    cols=[c for c in ['trade_index','date','symbol','direction','entry','historical_exit','qty','actual_result','actual_net_pnl','historical_costs','signal_match_quality','signal_timestamp','core_pass','strict_pass']+VALS+GATES+['fail_count','failed_gates','failed_core_components'] if c in winners]
    winners[cols].sort_values('actual_net_pnl',ascending=False).to_csv(OUT/'all_profitable_trades_with_current_values.csv',index=False)
    blocked[cols].sort_values('actual_net_pnl',ascending=False).to_csv(OUT/'blocked_profitable_trades.csv',index=False)
    sole[cols].sort_values('actual_net_pnl',ascending=False).to_csv(OUT/'sole_blocker_profitable_trades.csv',index=False)

    # Missingness is crucial: distinguish bad values from fields we cannot reconstruct.
    missing={c:int(winners[c].isna().sum()) for c in VALS+GATES if c in winners}
    summary={
      'replayable_trades':len(replay),'profitable_replayable_trades':len(winners),
      'profitable_net_pnl':float(pd.to_numeric(winners.actual_net_pnl).sum()),
      'accepted_profitable_trades':len(accepted),'accepted_profitable_pnl':float(pd.to_numeric(accepted.actual_net_pnl).sum()),
      'blocked_profitable_trades':len(blocked),'blocked_profitable_pnl':float(pd.to_numeric(blocked.actual_net_pnl).sum()),
      'gate_blocked_winner_counts':{g:n for g,n,_ in gate_rows},
      'gate_blocked_profit':{g:round(v,4) for g,_,v in gate_rows},
      'winner_value_distributions':stats,'winner_missing_values':missing,
      'near_miss_winners':{n:{'count':c,'profit':round(p,4)} for n,c,p in near},
      'important_note':'A historically profitable trade blocked by a current gate is not proof the gate is wrong. Use sole blockers, near misses, and loser-side comparison before changing thresholds.'
    }
    (OUT/'summary.json').write_text(json.dumps(summary,indent=2))
    print('\nWrote:',OUT/'all_profitable_trades_with_current_values.csv')
    print('Wrote:',OUT/'blocked_profitable_trades.csv')
    print('Wrote:',OUT/'sole_blocker_profitable_trades.csv')
    print('Wrote:',OUT/'summary.json')

if __name__=='__main__': main()
