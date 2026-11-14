"""Causal entry features, forward price labels and independent rule-based exits."""
import argparse
import copy
import hashlib
import json
import math
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from terminal_backtest import selection_bars, adjustment_basis, replay_strategy, minute_points, write_json
from terminal_paper import clock, FEE_SCHEDULE
from tools.compare_historical_rankings import CachedHistory

FEATURE_VERSION='candidate-patterns-v1'


def entry_features(bars, market=None):
    """Input must end at the decision time; no future labels enter this function."""
    if len(bars)<35:raise ValueError('Entry history shorter than 35 bars')
    close=np.array([b['close'] for b in bars],float);volume=np.array([b['volume'] for b in bars],float)
    high=np.array([b['high'] for b in bars],float);low=np.array([b['low'] for b in bars],float)
    price=close[-1];previous=close[-2];opening=float(bars[-1]['open']);width=high[-1]-low[-1]
    f=dict(price_1430=price,change_today=(price/previous-1)*100,gap_open=(opening/previous-1)*100,
           amplitude=(high[-1]-low[-1])/previous*100,
           close_location=(price-low[-1])/width if width else .5,
           upper_shadow=(high[-1]-max(opening,price))/width if width else 0.,
           lower_shadow=(min(opening,price)-low[-1])/width if width else 0.,
           body=(price-opening)/width if width else 0.)
    for n in (3,5,10,20):f['change_'+str(n)]=(price/close[-n-1]-1)*100
    for n in (5,10,20):f['ma_deviation_'+str(n)]=(price/close[-n:].mean()-1)*100
    f['volume_ratio']=volume[-1]/volume[-6:-1].mean() if volume[-6:-1].mean()>0 else None
    f['volume_5_over_20']=volume[-6:-1].mean()/volume[-21:-1].mean() if volume[-21:-1].mean()>0 else None
    f['volatility_20']=float(np.std(np.diff(np.log(close[-21:-1]))))*100
    f['ma20_slope_5']=(close[-20:].mean()/close[-25:-5].mean()-1)*100
    for n in (20,60):
        if len(bars)>=n+1:
            h,l=max(high[-n-1:-1]),min(low[-n-1:-1]);f['distance_high_'+str(n)]=(price/h-1)*100
            f['range_position_'+str(n)]=(price-l)/(h-l)*100 if h>l else 50.
        else:f['distance_high_'+str(n)]=f['range_position_'+str(n)]=None
    if market:
        f.update(market_change=market.get('change_today'),market_change_5=market.get('change_5'))
        f['relative_change_5']=f['change_5']-market['change_5'] if market.get('change_5') is not None else None
    return {key:float(value) if value is not None and math.isfinite(value) else None for key,value in f.items()}


def forward_labels(daily, actions, day, future_days):
    if len(future_days)!=5:return dict(complete=False,reason='不足5个后续交易日')
    adjusted={r[0]:r for r in daily['qfq']}
    if any(d not in adjusted for d in [day]+future_days):return dict(complete=False,reason='未来日线缺失或停牌待核验')
    scale,offset=adjustment_basis(daily,day,actions)
    raw={r[0]:r for r in daily['raw']};entry=float(raw[day][2])
    equivalent=lambda value:(float(value)-offset)/scale
    maximum=max(equivalent(adjusted[d][3]) for d in future_days)
    minimum=min(equivalent(adjusted[d][4]) for d in future_days)
    up=(maximum/entry-1)*100;down=(minimum/entry-1)*100
    return dict(complete=True,end=future_days[-1],maximum_up_pct=up,minimum_return_pct=down,
                close_return_pct=(equivalent(adjusted[future_days[-1]][2])/entry-1)*100,
                rise_3=up>=3,rise_5=up>=5,rise_10=up>=10,touch_minus5=down<=-5,
                basis='未来5个交易日，不含入选日；复权还原等价价格，不含税费，不是模拟成交收益')


_DATA=_BASE=_ROWS=_OUT=_MARKET=None


def initialize(report_path,data_root,output):
    global _DATA,_BASE,_ROWS,_OUT,_MARKET
    root=Path(report_path);_BASE=json.loads((root/'report.json').read_text())
    _DATA=CachedHistory(data_root,_BASE['start'],_BASE['end']);_OUT=Path(output)
    _MARKET=json.loads((_OUT/'market-context.json').read_text()).get('features',{})


def one_stock(job):
    code, rows=job;results=[]
    configs={r['id']:r['config'] for r in _BASE['results']}
    dates=_BASE['sessions'];index={d:i for i,d in enumerate(dates)}
    try:daily=_DATA.daily(code);intraday=_DATA.intraday(code);actions=_DATA.actions(code)
    except Exception as exc:
        return [dict(strategy=row['strategy'],code=code,date=row['date'],name=row['name'],status='feature_error',error=str(exc)) for row in rows]
    for row in sorted(rows,key=lambda r:(r['date'],r['strategy'])):
        sid,day=row['strategy'],row['date'];path=_OUT/'samples'/(sid+'-'+day+'-'+code+'.json')
        signature=hashlib.sha256(json.dumps([FEATURE_VERSION,configs[sid],row],sort_keys=True).encode()).hexdigest()
        if path.exists():
            cached=json.loads(path.read_text())
            if cached.get('signature')==signature:results.append(cached);continue
        sample=dict(strategy=sid,date=day,code=code,name=row['name'],rank=row['rank'],signature=signature)
        try:
            bars=selection_bars(daily,intraday,day,actions)
            sample['features']=entry_features(bars,_MARKET.get(day))
            sample['features'].update(composite_score=row['rank'],structure_score=row['scores'][0],
                                      trend_score=row['scores'][1],momentum_score=row['scores'][2])
            i=index[day];future=dates[i+1:i+6]
            sample['forward']=forward_labels(daily,actions,day,future)
            if not sample['forward']['complete']:
                sample.update(status='unlabelled',error=sample['forward']['reason'])
            else:
                cfg=copy.deepcopy(configs[sid]);strategy=dict(id=sid,config=cfg)
                replay_dates=dates[i:i+20]
                result=replay_strategy(strategy,[row],_DATA,replay_dates)
                if result['complete'] and result['positions'] and i+20<len(dates):
                    result=replay_strategy(strategy,[row],_DATA,dates[i:])
                if not result['complete']:
                    sample.update(status='execution_error',errors=result['errors'])
                elif not result['trades']:
                    sample.update(status='no_fill',reason=result['skipped'][0]['reason'] if result['skipped'] else '未成交')
                elif result['positions']:
                    sample.update(status='open_at_end',reason='延持或待卖，尚无完整收益')
                else:
                    buy,sell=result['trades'][0],result['trades'][-1]
                    net=sell['pnl']/(buy['qty']*buy['price']+buy['fees'])*100
                    stop=net<0 and '跌破止损/涨停保护线' in sell['reason']
                    sample.update(status='complete',buy=buy,sell=sell,net_return_pct=net,
                                  actual_profit=net>0,loss_stop_exit=stop,
                                  rise5_but_loss=sample['forward']['rise_5'] and net<=0,
                                  observed_end=max(sample['forward']['end'],sell['date']))
        except Exception as exc:
            sample.update(status='feature_error',error=type(exc).__name__+': '+str(exc))
        write_json(path,sample);results.append(sample)
    return results


def collect_market(data_root,source,output):
    """Fetch the Shanghai index explicitly; stock 000001 is not an index."""
    cache=Path(data_root)/'market-context.json'
    if cache.exists():
        value=json.loads(cache.read_text())
        if value.get('retrieved_at','')[:10]>=source['end']:
            write_json(output/'market-context.json',value);return
    from terminal_backtest import HistoricalData,sina_intraday_rows
    data=HistoricalData(data_root,source['start'],source['end'])
    session=data.http();daily=session.get('https://web.ifzq.gtimg.cn/appstock/app/kline/kline',
        params={'param':'sh000001,day,,,500'},timeout=15).json()['data']['sh000001']['day']
    payload=session.get('https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData',
        params={'symbol':'sh000001','scale':30,'ma':'no','datalen':1970},timeout=15).text
    bars=sina_intraday_rows(payload);grouped=defaultdict(list)
    for row in bars:grouped[row[0][:8]].append(row)
    features={}
    for i,row in enumerate(daily):
        day=row[0];early=[x for x in grouped[day.replace('-','')] if x[0][8:]<='1430']
        if i>=5 and [x[0][8:] for x in early]==['1000','1030','1100','1130','1330','1400','1430']:
            c=float(early[-1][2]);features[day]=dict(change_today=(c/float(daily[i-1][2])-1)*100,
                                                   change_5=(c/float(daily[i-5][2])-1)*100)
    value=dict(features=features,daily=daily,intraday=bars,retrieved_at=clock(),
               source='腾讯上证指数日线与新浪上证指数30分钟线；symbol=sh000001')
    write_json(cache,value);write_json(output/'market-context.json',value)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True);parser.add_argument('--data',required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--workers',type=int,default=4,choices=range(1,5))
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args();root=Path(args.report);base=json.loads((root/'report.json').read_text())
    if base.get('fee_schedule')!=FEE_SCHEDULE:raise ValueError('Fee schedule mismatch')
    output=Path(args.output);output.mkdir(parents=True,exist_ok=args.resume)
    matches=json.loads((root/'selections.json').read_text());signature=hashlib.sha256((root/'selections.json').read_bytes()).hexdigest()
    if args.resume and (output/'study.json').exists():
        old=json.loads((output/'study.json').read_text())
        if old['selections_sha256']!=signature:raise ValueError('Source scan changed')
    try:collect_market(args.data,base,output)
    except Exception as exc:
        write_json(output/'market-context.json',dict(features={},error=str(exc),source='指数数据不可用，不补造'))
    grouped=defaultdict(list)
    for row in matches:grouped[row['code']].append(row)
    manifest=dict(created=clock(),version=FEATURE_VERSION,status='running',start=base['start'],end=base['end'],
                  source_report=base['id'],selections_sha256=signature,universe_count=base['universe_count'],
                  coverage_pct=base['coverage_pct'],fee_schedule=FEE_SCHEDULE,sessions=base['sessions'],
                  strategy_names={r['id']:r['name'] for r in base['results']},sample_count=len(matches),processed=0,
                  assumptions=base['assumptions']+[
                      '每个候选独立用初始10万元账户、计划1万元建仓，仅用于统一标签；组合验证仍使用动态资金',
                      '未来5个交易日走势不含入选日；原最长持仓规则含买入日，两个窗口分别列示',
                      '未来上涨标签与实际止损可能重叠；价格高点不当作成交价；未成交与未满观察期不当作亏损',
                      '买入特征只使用14:30及此前的数据；历史主力资金不完整，本次不使用资金字段'])
    write_json(output/'study.json',manifest);counts=Counter();started=time.monotonic()
    with (output/'samples.jsonl').open('w') as stream,ProcessPoolExecutor(args.workers,initializer=initialize,
            initargs=(str(root),args.data,str(output))) as executor:
        futures=[executor.submit(one_stock,job) for job in sorted(grouped.items())]
        for completed,future in enumerate(as_completed(futures),1):
            samples=future.result()
            for sample in samples:stream.write(json.dumps(sample,ensure_ascii=False,allow_nan=False)+'\n');counts[sample['status']]+=1
            stream.flush();manifest['processed']+=len(samples);manifest['counts']=dict(counts)
            if completed%10==0 or completed==len(grouped):
                manifest['progress']=f"{manifest['processed']}/{len(matches)}候选，{completed}/{len(grouped)}只股票，耗时{int(time.monotonic()-started)}秒"
                write_json(output/'study.json',manifest);print(manifest['progress'],dict(counts),flush=True)
    manifest['status']='completed';write_json(output/'study.json',manifest)


if __name__=='__main__':main()
