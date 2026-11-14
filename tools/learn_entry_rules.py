"""Small causal entry models with purged chronological rule selection."""
import argparse
import copy
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from terminal_entry_features import finite,predict_entry
from terminal_backtest import replay_strategy,write_json
from terminal_paper import clock
from tools.compare_historical_rankings import CachedHistory,metrics,validate_ledger,ranking_key

FEATURES=['change_today','gap_open','amplitude','close_location','upper_shadow','lower_shadow','body',
 'change_3','change_5','change_10','change_20','ma_deviation_5','ma_deviation_10','ma_deviation_20',
 'volume_ratio','volume_5_over_20','volatility_20','ma20_slope_5','distance_high_20','distance_high_60',
 'range_position_20','range_position_60','market_change','market_change_5','relative_change_5']
VOLUME_FEATURES=['volume_ratio','volume_5_over_20','same_time_volume_ratio_5','same_time_volume_ratio_20',
 'same_time_volume_percentile_20','prior_volume_ratio','volume_contraction_3','historical_volume_trend_5',
 'signed_volume_balance_5','price_volume_correlation_10','last30_volume_acceleration',
 'last30_price_volume_strength','afternoon_volume_share']
FEATURES += [f for f in VOLUME_FEATURES if f not in FEATURES]+['last30_price_change','last30_close_location']
POLICIES=[dict(id='rank_only',threshold=None,stop_max=1.,penalty=0.),
 dict(id='positive',threshold=0.,stop_max=1.,penalty=0.),
 dict(id='edge025',threshold=.25,stop_max=1.,penalty=0.),
 dict(id='edge050',threshold=.5,stop_max=1.,penalty=0.),
 dict(id='positive_risk',threshold=0.,stop_max=.35,penalty=1.),
 dict(id='edge025_risk',threshold=.25,stop_max=.35,penalty=1.),
 dict(id='edge050_risk',threshold=.5,stop_max=.35,penalty=1.),
 dict(id='opportunity',threshold=None,stop_max=.35,rise_min=.5,penalty=0.,scoring='opportunity'),
 dict(id='opportunity_edge',threshold=0.,stop_max=.35,rise_min=.5,penalty=0.,scoring='opportunity')]




def weighted_corr(x,y,w):
    x=np.asarray(x,float);y=np.asarray(y,float);w=np.asarray(w,float);w=w/w.sum()
    a=x-np.sum(w*x);b=y-np.sum(w*y);den=math.sqrt(float(np.sum(w*a*a)*np.sum(w*b*b)))
    return float(np.sum(w*a*b)/den) if den>1e-12 else 0.


def training_samples(samples,cutoff):
    return [s for s in samples if s.get('status')=='complete' and s['date']<=cutoff and s.get('observed_end','9999')<=cutoff]


def fit_logistic(x,target,weights,penalty):
    mean=(target.sum()+.5)/(len(target)+1);beta=np.zeros(x.shape[1]);beta[0]=math.log(mean/(1-mean))
    for _ in range(40):
        p=1/(1+np.exp(-np.clip(np.sum(x*beta,axis=1),-30,30)))
        gradient=np.einsum('ni,n->i',x,weights*(p-target))+np.diag(penalty)*beta
        hessian=np.einsum('ni,n,nj->ij',x,weights*p*(1-p),x,optimize=False)+penalty+np.eye(x.shape[1])*1e-8
        step=np.linalg.solve(hessian,gradient);beta-=step
        if np.linalg.norm(step)<1e-7:break
    return beta


def fit_entry_model(samples,cutoff,minimum=40,profile='general'):
    if profile not in ('general','volume_price'):raise ValueError('Unknown feature profile')
    rows=training_samples(samples,cutoff)
    if len(rows)<minimum:raise ValueError('完整训练样本不足'+str(minimum))
    counts=Counter(s['date'] for s in rows);weights=np.array([1/counts[s['date']] for s in rows]);weights*=len(rows)/weights.sum()
    target=np.array([s['net_return_pct'] for s in rows],float)
    ranks=pd.Series(target).rank().to_numpy();correlations={};arrays={}
    for f in FEATURES:
        raw=np.array([s['features'].get(f) if finite(s['features'].get(f)) else np.nan for s in rows],float)
        if np.isfinite(raw).mean()<.95:continue
        available=np.isfinite(raw)
        if np.std(raw[available])<1e-8:continue
        arrays[f]=raw;correlations[f]=weighted_corr(pd.Series(raw[available]).rank().to_numpy(),ranks[available],weights[available])
    chosen=[];ordered=sorted(correlations,key=lambda f:(-abs(correlations[f]),f))
    families=[(ordered,3)] if profile=='general' else [([f for f in ordered if f in VOLUME_FEATURES],2),([f for f in ordered if f not in VOLUME_FEATURES],1)]
    for family,limit in families:
        added=0
        for f in family:
            def independent(old):
                valid=np.isfinite(arrays[f])&np.isfinite(arrays[old])
                return abs(weighted_corr(arrays[f][valid],arrays[old][valid],weights[valid]))<.85
            if all(independent(old) for old in chosen):
                chosen.append(f);added+=1
            if added==limit:break
        if profile=='volume_price' and added<limit:raise ValueError('独立量价特征不足')
    if not chosen:raise ValueError('无足够可用特征')
    selection_count=len(rows)
    rows=[row for row in rows if all(finite(row['features'].get(f)) for f in chosen)]
    if len(rows)<minimum:raise ValueError('特征完整训练样本不足'+str(minimum))
    counts=Counter(s['date'] for s in rows);weights=np.array([1/counts[s['date']] for s in rows]);weights*=len(rows)/weights.sum()
    target=np.array([s['net_return_pct'] for s in rows],float)
    arrays={f:np.array([s['features'][f] for s in rows],float) for f in chosen}
    for f in chosen:correlations[f]=weighted_corr(pd.Series(arrays[f]).rank().to_numpy(),pd.Series(target).rank().to_numpy(),weights)
    transformations=[];columns=[]
    for f in chosen:
        v=arrays[f];lo,hi=np.quantile(v,[.01,.99]);center=float(np.median(v));scale=float(np.quantile(v,.75)-np.quantile(v,.25)) or float(np.std(v))
        transformations.append(dict(feature=f,center=center,scale=scale,lower=float(lo),upper=float(hi),correlation=correlations[f]))
        columns.append((np.clip(v,lo,hi)-center)/scale)
    x=np.column_stack([np.ones(len(rows))]+columns);penalty=np.eye(x.shape[1])*10;penalty[0,0]=0
    lower,upper=np.quantile(target,[.02,.98]);robust=np.clip(target,lower,upper)
    gram=np.einsum('ni,n,nj->ij',x,weights,x,optimize=False)+penalty
    beta=np.linalg.solve(gram,np.einsum('ni,n->i',x,weights*robust))
    stopped=np.array([s['loss_stop_exit'] for s in rows],float)
    logit=fit_logistic(x,stopped,weights,penalty)
    rise=fit_logistic(x,np.array([s['forward']['rise_5'] for s in rows],float),weights,penalty)
    if not all(np.isfinite(v).all() for v in (beta,logit,rise)):
        raise ValueError('模型数值无效，不生成预测')
    return dict(version='entry-model-v1',profile=profile,cutoff=cutoff,training_count=len(rows),training_days=len(counts),
                feature_selection_count=selection_count,dropped_missing_count=selection_count-len(rows),
                features=transformations,return_coefficients=beta.tolist(),stop_coefficients=logit.tolist(),rise_coefficients=rise.tolist(),
                target_clip=[float(lower),float(upper)],ridge=10,training_mean_return=float(np.average(target,weights=weights)),
                training_stop_rate=float(np.average(stopped,weights=weights)),
                small_sample=len(rows)<100,correlations=correlations)




def policy_accept(prediction,policy):
    return bool(prediction and (policy['threshold'] is None or prediction['expected_net_pct']>policy['threshold'])
                and prediction['stop_probability']<=policy['stop_max']
                and prediction.get('rise5_probability',0)>=policy.get('rise_min',0))


def ranking_value(prediction,policy):
    if policy.get('scoring')=='opportunity':return prediction['rise5_probability']-prediction['stop_probability']
    return prediction['expected_net_pct']-policy['penalty']*prediction['stop_probability']


def pattern_tables(samples,train_end,val_end):
    complete=[s for s in samples if s.get('status')=='complete'];out=[];cohorts=[]
    for sid in sorted({s['strategy'] for s in samples}):
        own=[s for s in complete if s['strategy']==sid]
        train=training_samples(own,train_end)
        for period,rows in [('train',train),('validation',[s for s in own if train_end<s['date']<=val_end and s['observed_end']<=val_end]),
                            ('test',[s for s in own if s['date']>val_end])]:
            n=len(rows)
            cohorts.append(dict(strategy=sid,period=period,n=n,rise3=sum(s['forward']['rise_3'] for s in rows),
                rise5=sum(s['forward']['rise_5'] for s in rows),rise10=sum(s['forward']['rise_10'] for s in rows),
                profit=sum(s['actual_profit'] for s in rows),stop=sum(s['loss_stop_exit'] for s in rows),
                rise5_but_loss=sum(s['rise5_but_loss'] for s in rows),mean_return=float(np.mean([s['net_return_pct'] for s in rows])) if n else None))
        for f in FEATURES:
            values=[s['features'].get(f) for s in train if finite(s['features'].get(f))]
            if len(values)<15:continue
            thresholds=sorted(set(float(v) for v in np.quantile(values,[1/3,2/3])))
            bounds=[-math.inf]+thresholds+[math.inf]
            for b in range(len(bounds)-1):
                lo,hi=bounds[b],bounds[b+1]
                for period,rows in [('train',train),('validation',[s for s in own if train_end<s['date']<=val_end and s['observed_end']<=val_end]),
                                    ('test',[s for s in own if s['date']>val_end])]:
                    group=[s for s in rows if finite(s['features'].get(f)) and lo<s['features'][f]<=hi]
                    n=len(group)
                    day_counts=Counter(s['date'] for s in group)
                    balanced=[1/day_counts[s['date']] for s in group]
                    out.append(dict(strategy=sid,feature=f,lower=lo if math.isfinite(lo) else None,
                        upper=hi if math.isfinite(hi) else None,period=period,n=n,
                        days=len(day_counts),
                        rise5_rate=sum(s['forward']['rise_5'] for s in group)/n if n else None,
                        profit_rate=sum(s['actual_profit'] for s in group)/n if n else None,
                        stop_rate=sum(s['loss_stop_exit'] for s in group)/n if n else None,
                        mean_net_pct=float(np.mean([s['net_return_pct'] for s in group])) if n else None,
                        day_weighted_net_pct=float(np.average([s['net_return_pct'] for s in group],weights=balanced)) if n else None,
                        day_weighted_stop_rate=float(np.average([s['loss_stop_exit'] for s in group],weights=balanced)) if n else None))
    return cohorts,out


def run_strategy(job):
    sid,source_path,data_root,study_path,output_path,train_end,val_end,profile=job
    source=Path(source_path);base=json.loads((source/'report.json').read_text());all_rows=json.loads((source/'selections.json').read_text())
    samples=[json.loads(line) for line in (Path(study_path)/'samples.jsonl').open()]
    own=[s for s in samples if s['strategy']==sid];cfg=next(s['config'] for s in base['results'] if s['id']==sid)
    name=cfg['name'];out=Path(output_path)/sid;out.mkdir(parents=True,exist_ok=True)
    try:model=fit_entry_model(own,train_end,profile=profile)
    except ValueError as exc:return dict(strategy=sid,name=name,status='insufficient_training',error=str(exc))
    write_json(out/'model.json',model)
    predictions={(s['date'],s['code']):predict_entry(model,s['features']) for s in own if s.get('features')}
    data=CachedHistory(data_root,base['start'],base['end']);dates=base['sessions']
    strategy=dict(id=sid,config=copy.deepcopy(cfg));pool=[r for r in all_rows if r['strategy']==sid]
    def replay(label,start,end,policy=None,control=None):
        window=[d for d in dates if start<=d<=end];rows=[r for r in pool if r['date'] in window]
        if policy:
            rows=[dict(r,prediction=predictions[(r['date'],r['code'])]) for r in rows if policy_accept(predictions.get((r['date'],r['code'])),policy)]
            key=lambda r:(-ranking_value(r['prediction'],policy),r['code'])
        else:key=ranking_key(control,sid)
        result=replay_strategy(strategy,rows,data,window,candidate_key=key)
        validate_ledger(result,data)
        for t in result['trades']:
            if t['side']=='buy':t['prediction']=predictions.get((t['date'],t['code']))
        result['research_case']=label;write_json(out/(label+'.json'),result)
        summary=dict(label=label,complete=result['complete'],errors=result['errors'],start=start,end=end,**metrics(result,window[len(window)//2-1]))
        summary['average_exposure']=float(np.mean([(e['nav']-e['cash']-e.get('receivable',0))/e['nav']*100 for e in result['equity']])) if result['equity'] else None
        summary['active_days']=sum(e['positions']>0 for e in result['equity'])
        return summary
    val_start=next(d for d in dates if d>train_end);test_start=next(d for d in dates if d>val_end)
    validation=[]
    for policy in POLICIES:
        r=replay('validation-'+policy['id'],val_start,val_end,policy=policy);r['policy']=policy;validation.append(r)
        write_json(out/'progress.json',dict(strategy=name,phase='validation',policy=policy['id'],result=r['return_pct']))
    eligible=[r for r in validation if r['complete'] and r['buys']>=8]
    if not eligible:
        return dict(strategy=sid,name=name,status='insufficient_validation',model=model,validation=validation)
    chosen=max(eligible,key=lambda r:(r['return_pct'],-r['drawdown']))
    policy=chosen['policy'];write_json(out/'frozen-policy.json',dict(model=model,policy=policy,selected_at=val_end))
    tests=[]
    for control in ('composite_desc','composite_asc'):
        tests.append(replay('test-'+control,test_start,base['end'],control=control))
    tests.append(replay('test-learned-ranking',test_start,base['end'],policy=POLICIES[0]))
    tests.append(replay('test-selected-policy',test_start,base['end'],policy=policy))
    tests.append(replay('test-fixed-positive-filter',test_start,base['end'],policy=POLICIES[1]))
    answer=dict(strategy=sid,name=name,status='completed' if all(r['complete'] for r in tests) else 'partial',
                model=model,validation=validation,chosen_policy=policy,
                validation_positive=chosen['return_pct']>0,tests=tests)
    write_json(out/'result.json',answer);return answer


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report',required=True);p.add_argument('--data',required=True);p.add_argument('--study',required=True)
    p.add_argument('--output',required=True);p.add_argument('--workers',type=int,default=3,choices=range(1,4))
    p.add_argument('--profile',choices=['general','volume_price'],default='general')
    a=p.parse_args();study=json.loads((Path(a.study)/'study.json').read_text())
    if study['status']!='completed':raise ValueError('Candidate labelling is not complete')
    base=json.loads((Path(a.report)/'report.json').read_text());dates=base['sessions']
    train_end=dates[int(len(dates)*.6)-1];val_end=dates[int(len(dates)*.8)-1]
    out=Path(a.output);out.mkdir(parents=True,exist_ok=False)
    samples=[json.loads(line) for line in (Path(a.study)/'samples.jsonl').open()]
    cohorts,bins=pattern_tables(samples,train_end,val_end)
    write_json(out/'cohorts.json',cohorts);write_json(out/'feature-bins.json',bins)
    report=dict(created=clock(),status='running',train_end=train_end,validation_end=val_end,test_start=next(d for d in dates if d>val_end),
                source_report=base['id'],study=str(Path(a.study)),sample_counts=study['counts'],
                samples_sha256=hashlib.sha256((Path(a.study)/'samples.jsonl').read_bytes()).hexdigest(),
                learner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                policies=POLICIES,results=[],method=dict(profile=a.profile,maximum_features=3,ridge=10,day_equal_weights=True,
                training_purge='标签观察终点须不晚于训练截止日',selection='验证段期末权益最高，至少8笔买入；测试段不再选规则；正期望过滤另列为预先固定的对照',
                limitations='同一历史已用于先前研究，留出段用于时间隔离检验，仍需后续前向模拟'))
    write_json(out/'learning.json',report)
    jobs=[(s['id'],a.report,a.data,a.study,a.output,train_end,val_end,a.profile) for s in base['results']]
    with ProcessPoolExecutor(a.workers) as ex:
        for result in ex.map(run_strategy,jobs):
            report['results'].append(result);write_json(out/'learning.json',report)
            print(result['name'],result['status'],'features',result.get('model',{}).get('features'),'tests',result.get('tests'),flush=True)
    report['status']='completed' if all(r['status']=='completed' for r in report['results']) else 'partial';write_json(out/'learning.json',report)


if __name__=='__main__':main()
