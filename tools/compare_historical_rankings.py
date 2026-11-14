"""Isolated, reproducible ordering comparisons over one saved historical scan."""
import argparse
import copy
import hashlib
import json
import sys
import time
import uuid
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from terminal_backtest import HistoricalData, replay_strategy, write_json
from terminal_paper import FEE_SCHEDULE, clock, fees, money


def ranking_key(mode, strategy_id, seed=0):
    if mode == 'composite_desc':
        return lambda row: (-row['rank'], row['code'])
    if mode == 'composite_asc':
        return lambda row: (row['rank'], row['code'])
    if mode != 'random':
        raise ValueError('Unknown ordering')
    def key(row):
        value = f"{seed}|{strategy_id}|{row['date']}|{row['code']}"
        return hashlib.sha256(value.encode()).hexdigest(), row['code']
    return key


class CachedHistory(HistoricalData):
    @lru_cache(maxsize=128)
    def daily(self, code):
        return super().daily(code)

    @lru_cache(maxsize=128)
    def actions(self, code):
        return super().actions(code)

    @lru_cache(maxsize=24)
    def ticks(self, code, day):
        return super().ticks(code, day)

    @lru_cache(maxsize=128)
    def cash_dividend(self, code, day):
        return super().cash_dividend(code, day)


def validate_ledger(result, data):
    if not result['complete']:
        return
    cash, held, cost_fees, realized = 100000., {}, 0., 0.
    byday = defaultdict(list)
    for trade in result['trades']:
        byday[trade['date']].append(trade)
    for equity in result['equity']:
        day = equity['date']
        for trade in byday[day]:
            gross = money(trade['qty']*trade['price'])
            fee = money(sum(fees(gross,trade['side']).values()))
            assert fee == trade['fees']
            cost_fees += fee
            if trade['side'] == 'buy':
                assert trade['code'] not in held
                held[trade['code']] = trade
                cash = money(cash-gross-fee)
                assert len(held) <= result['config']['max_positions']
            else:
                entry = held.pop(trade['code'])
                assert entry['date'] < day and entry['qty'] == trade['qty']
                pnl = money(gross-fee-entry['qty']*entry['price']-entry['fees']+trade.get('dividends',0))
                assert pnl == trade['pnl']
                realized += pnl
                cash = money(cash+gross-fee)
            assert cash >= 0
        value = sum(next(float(r[2]) for r in data.daily(code)['raw'] if r[0]==day)*entry['qty']
                    for code,entry in held.items())
        income = money(sum(e['net'] if e.get('sold','9999') <= day else money(e['gross']*.8)
                           for e in result.get('income_events',[]) if e['date'] <= day))
        assert cash == equity['cash']
        assert money(cash+value+income) == equity['nav']
        assert value <= equity['nav']*result['config']['max_exposure']/100+.01
    assert set(held) == {p['code'] for p in result['positions']}
    assert money(cost_fees) == result['fees'] and money(realized) == result['realized']


def metrics(result, split_date):
    sells = [t for t in result['trades'] if t['side']=='sell']
    winners = [t['pnl'] for t in sells if t['pnl']>0]
    losers = [-t['pnl'] for t in sells if t['pnl']<=0]
    average_win = sum(winners)/len(winners) if winners else None
    average_loss = sum(losers)/len(losers) if losers else None
    first = next((e['nav'] for e in result['equity'] if e['date']==split_date), None)
    return dict(nav=result['nav'],return_pct=result['return_pct'],drawdown=result['drawdown'],
                fees=result['fees'],win_rate=result['win_rate'],buys=sum(t['side']=='buy' for t in result['trades']),
                sells=len(sells),positions=len(result['positions']),average_win=average_win,average_loss=average_loss,
                payoff=average_win/average_loss if average_win is not None and average_loss else None,
                first_period_return_pct=(first/100000-1)*100 if first and result['complete'] else None,
                second_period_return_pct=(result['nav']/first-1)*100 if first and result['complete'] else None)


_DATA = _BASE = _MATCHES = _OUTPUT = None

def initialize(report_path, data_root, output):
    global _DATA, _BASE, _MATCHES, _OUTPUT
    _BASE = json.loads((Path(report_path)/'report.json').read_text())
    _MATCHES = json.loads((Path(report_path)/'selections.json').read_text())
    _DATA = CachedHistory(data_root,_BASE['start'],_BASE['end'])
    _OUTPUT = Path(output)


def run_case(job):
    sid, mode, seed = job
    saved = next(r for r in _BASE['results'] if r['id']==sid)
    strategy = dict(id=sid,config=copy.deepcopy(saved['config']))
    label = mode if mode!='random' else f'random_{seed}'
    file_id = sid+'-'+label
    key = ranking_key(mode,sid,seed)
    def progress(day):
        write_json(_OUTPUT/(file_id+'.progress.json'),dict(strategy=saved['name'],mode=label,date=day))
    result = replay_strategy(strategy,_MATCHES,_DATA,_BASE['sessions'],progress,candidate_key=key)
    result['ordering'] = dict(mode=mode,seed=seed if mode=='random' else None)
    validate_ledger(result,_DATA)
    if mode == 'composite_desc':
        for field in ('nav','fees','trades','equity','positions','income_events'):
            assert result[field] == saved[field], 'Baseline changed: '+saved['name']+' '+field
    grouped = defaultdict(list)
    for row in result['selections']:
        grouped[row['date']].append(row)
    assert all(rows==sorted(rows,key=key) for rows in grouped.values())
    write_json(_OUTPUT/(file_id+'.json'),result)
    split = _BASE['sessions'][len(_BASE['sessions'])//2-1]
    return dict(strategy_id=sid,name=saved['name'],mode=mode,label=label,seed=seed if mode=='random' else None,
                complete=result['complete'],errors=result['errors'],file=file_id+'.json',**metrics(result,split))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True,help='Saved historical report directory')
    parser.add_argument('--data',required=True,help='Historical data cache')
    parser.add_argument('--output',help='New private comparison directory')
    parser.add_argument('--workers',type=int,default=3,choices=range(1,5))
    parser.add_argument('--seeds',default='1,2,3,4,5')
    args=parser.parse_args()
    source=Path(args.report);base=json.loads((source/'report.json').read_text())
    if not all(r['complete'] for r in base['results']):
        raise ValueError('All baseline accounts must have completed')
    if any(r['config'].get('ranking_mode') != 'composite' for r in base['results']):
        raise ValueError('This comparison requires composite-ranked baseline accounts')
    if base.get('fee_schedule') != FEE_SCHEDULE:
        raise ValueError('Baseline fee schedule differs from current execution engine')
    seeds=[int(v) for v in args.seeds.split(',')]
    if len(set(seeds))!=len(seeds) or not 1<=len(seeds)<=20:
        raise ValueError('Use 1 to 20 unique random seeds')
    output=Path(args.output or Path('.runtime/ranking-comparisons')/uuid.uuid4().hex)
    output.mkdir(parents=True,exist_ok=False)
    jobs=[(r['id'],mode,seed) for mode,seed in [('composite_desc',0),('composite_asc',0)]+[('random',s) for s in seeds]
          for r in base['results']]
    code_root=Path(__file__).resolve().parents[1]
    manifest=dict(created=clock(),status='running',source_report=base['id'],start=base['start'],end=base['end'],
                  sessions=base['sessions'],split_date=base['sessions'][len(base['sessions'])//2-1],
                  universe_count=base['universe_count'],universe_note=base['universe_note'],coverage_pct=base['coverage_pct'],
                  fee_schedule=FEE_SCHEDULE,seeds=seeds,case_count=len(jobs),results=[],
                  selections_sha256=hashlib.sha256((source/'selections.json').read_bytes()).hexdigest(),
                  execution_sha256=hashlib.sha256((code_root/'terminal_backtest.py').read_bytes()).hexdigest(),
                  assumptions=base['assumptions']+['仅改变候选排序，同一入选池、资金和退出规则；随机种子在运行前固定',
                      '两个时段沿用同一滚动账户，不在分界日强制平仓；相同区间比较不能视为未来收益保证'])
    write_json(output/'comparison.json',manifest)
    print('Output',str(output.resolve()),flush=True);started=time.monotonic()
    with ProcessPoolExecutor(args.workers,initializer=initialize,initargs=(str(source),args.data,str(output))) as executor:
        tasks={executor.submit(run_case,job):job for job in jobs}
        for future in as_completed(tasks):
            try:result=future.result()
            except Exception as exc:
                sid,mode,seed=tasks[future]
                result=dict(strategy_id=sid,mode=mode,seed=seed,complete=False,error=type(exc).__name__+': '+str(exc))
            manifest['results'].append(result)
            write_json(output/'comparison.json',manifest)
            print(len(manifest['results']),'/',len(jobs),json.dumps(result,ensure_ascii=False),'elapsed',round(time.monotonic()-started),flush=True)
    manifest['status']='completed' if all(r['complete'] for r in manifest['results']) else 'partial'
    write_json(output/'comparison.json',manifest)


if __name__=='__main__':
    main()
