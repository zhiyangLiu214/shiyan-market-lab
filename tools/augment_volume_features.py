"""Add same-clock volume and price-volume relationships without future bars."""
import argparse
import copy
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from terminal_backtest import write_json
from terminal_paper import clock


def volume_price_features(daily,intraday,day):
    raw={r[0]:r for r in daily['raw']};qfq={r[0]:r for r in daily['qfq']}
    grouped=defaultdict(list)
    for row in intraday:grouped[row[0][:8]].append(row)
    times=['1000','1030','1100','1130','1330','1400','1430']
    def early(d):
        rows=[r for r in grouped[d.replace('-','')] if r[0][8:]<='1430']
        return rows if [r[0][8:] for r in rows]==times else []
    today=early(day)
    if not today:raise ValueError('当天14:30量价数据不完整')
    prior=sorted(d for d in raw if d<day);vol=sum(float(r[5]) for r in today)
    f={}
    for n in (5,20):
        history=[early(d) for d in prior[-n:]]
        if len(history)!=n or not all(history):
            f['same_time_volume_ratio_'+str(n)]=None
            if n==20:f['same_time_volume_percentile_20']=None
            continue
        values=[sum(float(r[5]) for r in rows) for rows in history]
        avg=float(np.mean(values));f['same_time_volume_ratio_'+str(n)]=vol/avg if avg>0 else None
        if n==20:f['same_time_volume_percentile_20']=sum(v<=vol for v in values)/n*100
    past=[float(raw[d][5]) for d in prior]
    f['prior_volume_ratio']=past[-1]/float(np.mean(past[-6:-1])) if len(past)>=6 and np.mean(past[-6:-1])>0 else None
    f['volume_contraction_3']=float(np.mean(past[-3:])/np.mean(past[-20:])) if len(past)>=20 and np.mean(past[-20:])>0 else None
    f['historical_volume_trend_5']=float(np.mean(past[-5:])/np.mean(past[-10:-5])-1)*100 if len(past)>=10 and np.mean(past[-10:-5])>0 else None
    days=[d for d in prior if d in qfq]
    if len(days)>=6:
        signed=sum(np.sign(float(qfq[d][2])-float(qfq[days[i-1]][2]))*float(raw[d][5]) for i,d in enumerate(days) if i>=len(days)-5)
        total=sum(float(raw[d][5]) for d in days[-5:])
        f['signed_volume_balance_5']=float(signed/total) if total>0 else None
    else:f['signed_volume_balance_5']=None
    recent=days[-11:]
    if len(recent)==11 and all(float(raw[d][5])>0 for d in recent):
        returns=np.diff(np.log([float(qfq[d][2]) for d in recent]));volume_changes=np.diff(np.log([float(raw[d][5]) for d in recent]))
        a,b=returns-returns.mean(),volume_changes-volume_changes.mean()
        denominator=math.sqrt(float(np.sum(a*a)*np.sum(b*b)))
        f['price_volume_correlation_10']=float(np.sum(a*b)/denominator) if denominator>1e-12 else 0.
    else:f['price_volume_correlation_10']=None
    v=float(np.mean([float(r[5]) for r in today[-3:-1]]));last=today[-1]
    acceleration=float(last[5])/v if v>0 else None
    change=(float(last[2])/float(today[-2][2])-1)*100
    f['last30_volume_acceleration']=acceleration;f['last30_price_change']=change
    f['last30_price_volume_strength']=change*math.log1p(acceleration) if acceleration is not None else None
    f['afternoon_volume_share']=sum(float(r[5]) for r in today[-3:])/vol if vol>0 else None
    f['last30_close_location']=(float(last[2])-float(last[4]))/(float(last[3])-float(last[4])) if float(last[3])>float(last[4]) else .5
    return {k:float(v) if v is not None and math.isfinite(v) else None for k,v in f.items()}


def volume_patterns(features):
    v=features.get('same_time_volume_ratio_5');change=features.get('change_today');position=features.get('range_position_20')
    if v is None or change is None:return []
    tags=[]
    if v<.8 and position is not None and position<=40:tags.append('低位缩量')
    if 1<=v<1.8 and change>0 and features.get('close_location',0)>=.7:tags.append('温和放量上涨')
    if v>=1.8 and ((features.get('change_5') or 0)>=8 or (position is not None and position>=80)):tags.append('急涨高位放量')
    if v<1 and change>0:tags.append('价涨量缩')
    if v>1 and change<0:tags.append('价跌量增')
    if v<1 and change<0:tags.append('缩量回调')
    if v>=1.2 and (features.get('distance_high_20') or 0)>0:tags.append('放量突破')
    if v>=1.5 and (abs(change)<1 or features.get('upper_shadow',0)>=.4):tags.append('放量滞涨或长上影')
    return tags


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--study',required=True);p.add_argument('--data',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    source=Path(a.study);out=Path(a.output);out.mkdir(parents=True,exist_ok=False)
    original=json.loads((source/'study.json').read_text());rows=[json.loads(l) for l in (source/'samples.jsonl').open()]
    grouped=defaultdict(list)
    for r in rows:grouped[r['code']].append(r)
    errors=[]
    with (out/'samples.jsonl').open('w') as stream:
        for code,items in sorted(grouped.items()):
            daily=json.loads((Path(a.data)/'daily'/(code+'.json')).read_text());bar_data=json.loads((Path(a.data)/'bars'/(code+'.json')).read_text());bars=bar_data['rows'] if isinstance(bar_data,dict) else bar_data
            for row in items:
                if row.get('features'):
                    try:
                        row['features'].update(volume_price_features(daily,bars,row['date']));row['volume_patterns']=volume_patterns(row['features'])
                    except Exception as exc:errors.append(dict(code=code,date=row['date'],error=str(exc)))
                stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
    manifest=copy.deepcopy(original);manifest.update(created=clock(),version='candidate-volume-patterns-v1',source_study=str(source),
        source_samples_sha256=hashlib.sha256((source/'samples.jsonl').read_bytes()).hexdigest(),volume_feature_errors=errors,
        assumptions=original['assumptions']+['同期量比仅比较过去交易日14:30累计量；最后30分钟仅用已完成的14:00至14:30段',
           '量价形态可重叠，描述性分组不当作自动买点；成交量不是主力净流入'])
    write_json(out/'study.json',manifest);print('Augmented',len(rows),'samples; errors',len(errors),flush=True)


if __name__=='__main__':main()
