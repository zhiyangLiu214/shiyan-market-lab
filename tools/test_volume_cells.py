"""Four prespecified volume-price interactions; thresholds fitted on past only."""
import argparse
import copy
import hashlib
import json
import math
import sys
from collections import Counter,defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.learn_entry_rules import training_samples,finite
from tools.compare_historical_rankings import CachedHistory,metrics,validate_ledger
from terminal_backtest import replay_strategy,write_json
from terminal_paper import clock

PAIRS=[('clock_volume_return5','same_time_volume_ratio_5','change_5'),
 ('clock_volume_position20','same_time_volume_ratio_5','range_position_20'),
 ('last30_volume_price','last30_volume_acceleration','last30_price_change'),
 ('prior_today_volume','prior_volume_ratio','same_time_volume_ratio_5')]


def fit_cells(samples,cutoff,feature_a,feature_b):
    rows=[r for r in training_samples(samples,cutoff) if all(finite(r['features'].get(f)) for f in (feature_a,feature_b))]
    if len(rows)<40:raise ValueError('交互训练样本不足')
    days=defaultdict(list)
    for s in rows:days[s['date']].append(s['net_return_pct'])
    overall=float(np.mean([np.mean(v) for v in days.values()]))
    edges=[sorted(set(float(x) for x in np.quantile([s['features'][f] for s in rows],[1/3,2/3]))) for f in (feature_a,feature_b)]
    def bin_of(s):return tuple(sum(s['features'][f]>x for x in e) for f,e in zip((feature_a,feature_b),edges))
    grouped=defaultdict(list)
    for s in rows:grouped[bin_of(s)].append(s)
    cells={}
    for key,items in grouped.items():
        byday=defaultdict(list)
        for s in items:byday[s['date']].append(s['net_return_pct'])
        average=float(np.mean([np.mean(v) for v in byday.values()]));n=len(byday)
        score=(average*n+overall*20)/(n+20)
        cells[str(key[0])+','+str(key[1])]=dict(n=len(items),days=n,mean_net_pct=average,score=score,
            qualified=len(items)>=30 and n>=10,profit_rate=sum(s['actual_profit'] for s in items)/len(items),
            stop_rate=sum(s['loss_stop_exit'] for s in items)/len(items),rise5_rate=sum(s['forward']['rise_5'] for s in items)/len(items))
    return dict(features=[feature_a,feature_b],edges=edges,cells=cells,training_count=len(rows),cutoff=cutoff,
                prior_days=20,overall_mean=overall)


def cell_score(model,features):
    if any(not finite(features.get(f)) for f in model['features']):return None
    key=','.join(str(sum(features[f]>edge for edge in e)) for f,e in zip(model['features'],model['edges']))
    cell=model['cells'].get(key)
    return cell['score'] if cell and cell['qualified'] else None


def one_strategy(job):
    sid,base_path,study_path,data_root,output=job
    source=Path(base_path);base=json.load(open(source/'report.json'));sessions=base['sessions']
    train_end=sessions[int(len(sessions)*.6)-1];val_end=sessions[int(len(sessions)*.8)-1]
    own=[s for s in map(json.loads,Path(study_path,'samples.jsonl').open()) if s['strategy']==sid]
    cfg=next(s['config'] for s in base['results'] if s['id']==sid);strategy=dict(id=sid,config=copy.deepcopy(cfg))
    pool=[r for r in json.load(open(source/'selections.json')) if r['strategy']==sid]
    data=CachedHistory(data_root,base['start'],base['end']);out=Path(output)/sid;out.mkdir(parents=True,exist_ok=True)
    results=[]
    for label,a,b in PAIRS:
        try:model=fit_cells(own,train_end,a,b)
        except ValueError as exc:results.append(dict(id=label,status='insufficient',error=str(exc)));continue
        scores={(s['date'],s['code']):cell_score(model,s['features']) for s in own if s.get('features')}
        def run(period,first,last):
            dates=[d for d in sessions if first<=d<=last]
            rows=[dict(r,entry_score=scores[(r['date'],r['code'])]) for r in pool if r['date'] in dates and
                  scores.get((r['date'],r['code'])) is not None and scores[(r['date'],r['code'])]>0]
            result=replay_strategy(strategy,rows,data,dates,candidate_key=lambda r:(-r['entry_score'],r['code']))
            validate_ledger(result,data);write_json(out/(period+'-'+label+'.json'),result)
            summary=dict(period=period,complete=result['complete'],errors=result['errors'],**metrics(result,dates[len(dates)//2-1]))
            summary['average_exposure']=float(np.mean([(e['nav']-e['cash']-e.get('receivable',0))/e['nav']*100 for e in result['equity']]))
            return summary
        validation=run('validation',next(d for d in sessions if d>train_end),val_end)
        # Freeze the training table before inspecting the later-period result.
        write_json(out/(label+'-model.json'),model)
        test=run('test',next(d for d in sessions if d>val_end),base['end'])
        results.append(dict(id=label,status='completed' if validation['complete'] and test['complete'] else 'partial',model=model,validation=validation,test=test))
    eligible=[r for r in results if r.get('validation',{}).get('complete') and r['validation']['buys']>=8]
    selected=max(eligible,key=lambda r:r['validation']['return_pct'])['id'] if eligible else None
    answer=dict(strategy=sid,name=cfg['name'],selected_by_validation=selected,results=results)
    write_json(out/'result.json',answer);return answer


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',required=True);p.add_argument('--study',required=True)
    p.add_argument('--data',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    base=json.load(open(Path(a.report)/'report.json'));out=Path(a.output);out.mkdir(parents=True,exist_ok=False)
    report=dict(created=clock(),status='running',pairs=PAIRS,results=[],source_report=base['id'],
                samples_sha256=hashlib.sha256(Path(a.study,'samples.jsonl').read_bytes()).hexdigest(),
                method='4组预先固定的量价交互；只用训练段生成阈值和格子分数，验证段选方案；测试段只报告，不再调参',
                limitations='同一历史已多轮研究，属于探索性时间分段复核，需后续新数据验证')
    write_json(out/'interactions.json',report)
    jobs=[(s['id'],a.report,a.study,a.data,a.output) for s in base['results']]
    with ProcessPoolExecutor(3) as ex:
        for r in ex.map(one_strategy,jobs):
            report['results'].append(r);write_json(out/'interactions.json',report)
            print(r['name'],r['selected_by_validation'],[(x['id'],x.get('validation',{}).get('return_pct'),x.get('test',{}).get('return_pct'),x.get('test',{}).get('buys')) for x in r['results']],flush=True)
    report['status']='completed' if all(case['status']=='completed' for item in report['results'] for case in item['results']) else 'partial'
    write_json(out/'interactions.json',report)


if __name__=='__main__':main()
