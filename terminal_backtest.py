"""Historical formula selection at 14:30 and close-price portfolio replay."""
import bisect
import copy
import hashlib
import json
import math
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests

from terminal_formula import evaluate_formula, FormulaError
from terminal_holding import holding_deadline
from terminal_formula_prefilter import might_match
from terminal_paper import fees, lot, money, clock
from terminal_strategy_exit import exit_decision
from terminal_strategy_rank import strategy_rank
from terminal_strategy_sim import exit_lines
from terminal_universe import in_boards, StockUniverse

VERSION = 'historical-1430-close-v1'


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def daily_rows(values):
    return [dict(date=row[0], open=float(row[1]), close=float(row[2]),
                 high=float(row[3]), low=float(row[4]), volume=float(row[5])) for row in values]


def selection_bars(daily, intraday, day):
    raw = {row['date']: row for row in daily_rows(daily['raw'])}
    adjusted = daily_rows(daily['qfq'])
    current = next((bar for bar in adjusted if bar['date'] == day), None)
    bars = [row for row in intraday if row[0][:8] == day.replace('-', '')]
    expected = ['1000', '1030', '1100', '1130', '1330', '1400', '1430']
    early = [row for row in bars if row[0][8:] <= '1430']
    if [row[0][8:] for row in early] != expected:
        raise ValueError('14:30以前的30分钟K线不完整')
    if not current or day not in raw or raw[day]['close'] <= 0:
        raise ValueError('当日复权/实际价格缺失')
    factor = current['close'] / raw[day]['close']
    # Rebase the adjusted history to this historical day's nominal price scale.
    # The close ratio is used only as a corporate-action scale, never as a signal.
    history = [dict(row, **{key: row[key] / factor for key in ('open', 'high', 'low', 'close')})
               for row in adjusted if row['date'] < day][-499:]
    partial = dict(date=day, open=float(early[0][1]), close=float(early[-1][2]),
                   high=max(float(row[3]) for row in early), low=min(float(row[4]) for row in early),
                   volume=sum(float(row[5]) for row in early))
    if partial['volume'] > raw[day]['volume'] * 1.01:
        raise ValueError('盘中与日线成交量单位或来源不一致')
    return history + [partial]


class HistoricalData:
    def __init__(self, root, start, end):
        self.root = Path(root)
        self.start, self.end = start, end
        self.local = threading.local()

    def http(self):
        if not hasattr(self.local, 'session'):
            self.local.session = requests.Session()
            self.local.session.trust_env = False
        return self.local.session

    def daily(self, code):
        path = self.root / 'daily' / (code + '.json')
        if path.exists():
            data = json.loads(path.read_text())
            if data.get('raw') and (data['raw'][-1][0] >= self.end or data.get('retrieved_at','')[:10] >= self.end) and data['raw'][0][0] <= self.start:
                return data
        sym = ('sh' if code.startswith('6') else 'sz') + code
        url = 'https://web.ifzq.gtimg.cn/appstock/app/'
        adjusted = self.http().get(url + 'fqkline/get',
                                   params={'param': f'{sym},day,,,640,qfq'}, timeout=10).json()['data'][sym]
        if not adjusted.get('qfqday'):
            raise ValueError('数据源未返回明确的前复权日线')
        raw = self.http().get(url + 'kline/kline',
                             params={'param': f'{sym},day,,,640'}, timeout=10).json()['data'][sym]['day']
        data = dict(qfq=adjusted['qfqday'], raw=raw, source='腾讯明确前复权及不复权日线',
                    retrieved_at=datetime.now().isoformat(timespec='seconds'))
        write_json(path, data)
        return data

    def intraday(self, code):
        path = self.root / 'bars' / (code + '.json')
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                if data.get('requested_start', '9999') <= self.start and data.get('retrieved_at', '')[:10] >= self.end:
                    return data['rows']
                data = data['rows']
            if data and data[0][0][:8] <= self.start.replace('-', '') and data[-1][0][:8] >= self.end.replace('-', ''):
                return data
        sym = ('sh' if code.startswith('6') else 'sz') + code
        data = self.http().get('https://ifzq.gtimg.cn/appstock/app/kline/mkline',
                              params={'param': f'{sym},m30,,640'}, timeout=10).json()['data'][sym]['m30']
        data = [row[:6] for row in data]
        write_json(path, dict(rows=data,requested_start=self.start,retrieved_at=clock(),source='腾讯30分钟K线'))
        return data

    def ticks(self, code, day):
        path = self.root / 'ticks' / (code + '-' + day + '.json')
        if path.exists():
            return json.loads(path.read_text())
        from pytdx.hq import TdxHq_API
        errors = []
        for host, port in [('180.153.18.172', 80), ('60.191.117.167', 7709), ('218.75.126.9', 7709)]:
            try:
                with TdxHq_API(raise_exception=True) as client:
                    client.connect(host, port, time_out=4)
                    rows = []
                    for offset in range(0, 36000, 1800):
                        page = client.get_history_transaction_data(
                            int(code.startswith('6')), code, offset, 1800, int(day.replace('-', '')))
                        rows = page + rows
                        if len(page) < 1800:
                            break
                    else:
                        raise ValueError('历史分笔超过完整性核验上限')
                    if not rows:
                        raise ValueError('历史分笔为空')
                    data = dict(rows=rows, source='通达信历史分笔成交', host=host,
                                day=day, retrieved_at=datetime.now().isoformat(timespec='seconds'))
                    write_json(path, data)
                    return data
            except Exception as exc:
                errors.append(type(exc).__name__)
        raise ValueError('历史分笔不可用 ' + ','.join(errors))


def minute_points(tick_data, raw):
    ticks = [row for row in tick_data['rows'] if '09:25' <= row['time'] <= '15:00' and row['vol'] > 0]
    if not ticks or any(b['time'] < a['time'] for a, b in zip(ticks, ticks[1:])):
        raise ValueError('历史分笔为空或顺序异常')
    total = sum(row['vol'] for row in ticks)
    if raw['volume'] <= 0 or abs(total / raw['volume'] - 1) > .005:
        raise ValueError('历史分笔成交量与日线不一致，无法验证分时均价')
    if abs(ticks[-1]['price'] - raw['close']) > .011:
        raise ValueError('历史分笔收盘与实际日线不一致')
    result, index, volume, amount = [], 0, 0, 0
    price = ticks[0]['price']
    for base in (datetime(2000, 1, 1, 9, 30), datetime(2000, 1, 1, 13)):
        for offset in range(1, 121):
            stamp = (base + timedelta(minutes=offset)).strftime('%H:%M')
            previous_volume, high = volume, 0.
            while index < len(ticks) and (ticks[index]['time'] < stamp or stamp == '15:00'):
                row = ticks[index]
                price = row['price']
                high = max(high, price)
                volume += row['vol']
                amount += price * row['vol']
                index += 1
            result.append(dict(time=stamp, price=price, volume=volume-previous_volume,
                               high=high or price, average=amount/volume if volume else None))
    return result


def scan_stock(meta, strategies, data, dates):
    code = meta['code']
    matches, errors = [], []
    eligible = [s for s in strategies if in_boards(code, s['config']['boards']) and
                (s['config']['pool'] == 'market' or code in s['config'].get('codes', []))]
    if not eligible:
        return matches, errors
    try:
        daily = data.daily(code)
        intraday = data.intraday(code)
    except Exception as exc:
        return [], [dict(code=code, date=None, reason=str(exc)[:160])]
    raw = {row[0]: row for row in daily['raw']}
    for day in dates:
        if day not in raw:
            errors.append(dict(code=code, date=day, reason='该日无个股日线，停牌或数据缺失待核验'))
            continue
        if float(raw[day][5]) <= 0:
            continue
        try:
            bars = selection_bars(daily, intraday, day)
        except ValueError as exc:
            errors.append(dict(code=code, date=day, reason=str(exc)))
            continue
        for strategy in eligible:
            cfg = strategy['config']
            try:
                if might_match(cfg['formula'], bars) and evaluate_formula(cfg['formula'], bars, code=code, name=meta['name']):
                    ranked = strategy_rank(cfg, bars, code, meta['name'])
                    matches.append(dict(strategy=strategy['id'], date=day, code=code, name=meta['name'],
                                        selected_at=day+' 14:30', **ranked))
            except FormulaError as exc:
                errors.append(dict(code=code, date=day, strategy=strategy['id'], reason=str(exc)))
    return matches, errors


def replay_strategy(strategy, matches, data, dates, progress=None):
    cfg = strategy['config']
    cash, realized, cost_fees, peak, drawdown = 100000., 0., 0., 100000., 0.
    positions, trades, equity, skipped, errors, selected = {}, [], [], [], [], []
    daily_cache = {}
    def bars_for(code):
        if code not in daily_cache:
            daily_cache[code] = daily_rows(data.daily(code)['raw'])
        return daily_cache[code]
    for day in dates:
        sold = set()
        for code, position in list(positions.items()):
            rows = bars_for(code)
            today = next((row for row in rows if row['date'] == day), None)
            if not today:
                errors.append(dict(date=day, code=code, reason='持仓日线缺失，停止本套收益计算'))
                break
            past = [row for row in rows if row['date'] < day]
            if not past:
                errors.append(dict(date=day, code=code, reason='缺少昨收'))
                break
            try:
                points = minute_points(data.ticks(code, day), today)
            except Exception as exc:
                errors.append(dict(date=day, code=code, reason=str(exc)))
                break
            quote = dict(date=day, prev_close=past[-1]['close'], daily={'bars': past}, sessions=sorted({bar['date'] for bar in rows if bar['date']<=day} | set(dates)))
            position['value'] = position['qty'] * today['close']
            # Daily corporate actions require explicit cash/share accounting.
            adj = {row[0]: float(row[2]) for row in data.daily(code)['qfq']}
            entry_raw = next(row for row in rows if row['date'] == position['opened'])
            entry_scale = adj[position['opened']] / entry_raw['close']
            current_scale = adj[day] / today['close']
            if abs(current_scale/entry_scale-1) > .001:
                errors.append(dict(date=day, code=code, reason='持有期发生复权因子变化，须补充权益分派后计算收益'))
                break
            pending = position.get('pending_exit')
            for i, point in enumerate(points):
                quote.update(price=point['price'], as_of=day+' '+point['time'], points=points[:i+1])
                if pending:
                    rate = .2 if code.startswith(('30','688')) else .1
                    lower = money(quote['prev_close'] * (1-rate))
                    if point['price'] > lower and position['qty'] <= point['volume']*100*.05:
                        price = max(lower, money(point['price']*.9995))
                        gross = money(price*position['qty'])
                        fee = money(sum(fees(gross,'sell').values()))
                        pnl = money(gross-fee-position['cost'])
                        cash = money(cash+gross-fee)
                        realized = money(realized+pnl)
                        cost_fees += fee
                        trades.append(dict(date=day, time=point['time'], code=code, name=position['name'],
                                           side='sell', qty=position['qty'], price=price, fees=fee, pnl=pnl, reason=pending))
                        sold.add(code)
                        del positions[code]
                        break
                lines = exit_lines(position, quote, cfg, position.get('lines'))
                decision = exit_decision(position, quote, cfg, lines, position.get('exit_state'))
                position['lines'], position['exit_state'] = decision['lines'], decision['state']
                deadline = holding_deadline(position, quote, cfg.get('holding_limit', True))
                if decision['sell']:
                    pending = decision['reason']
                elif deadline['sell']:
                    pending = deadline['reason']
            if code in positions:
                position['pending_exit'] = pending
        if errors:
            break
        candidates = sorted([row for row in matches if row['date'] == day and row['strategy'] == strategy['id']],
                            key=lambda row: (-row['rank'], row['code']))
        selected.extend(candidates)
        value = sum(position['value'] for position in positions.values())
        for row in candidates:
            code = row['code']
            reason = ''
            if code in positions or code in sold:
                reason = '已持有或当日已卖出'
            elif len(positions) >= cfg['max_positions']:
                reason = '持仓名额已满'
            elif 'ST' in row['name'].upper():
                reason = '当前模拟执行规则不买入ST'
            if reason:
                skipped.append(dict(row, reason=reason))
                continue
            raw = bars_for(code)
            today = next(bar for bar in raw if bar['date']==day)
            prior = [bar for bar in raw if bar['date'] < day]
            price = today['close']
            if not prior or price >= money(prior[-1]['close']*(1.2 if code.startswith(('30','688')) else 1.1)):
                skipped.append(dict(row, reason='收盘触及涨停，无法确认排队成交'))
                continue
            nav = cash+value
            budget = min(nav*cfg['position_pct']/100, nav*cfg['max_exposure']/100-value, cash)
            minimum, step = lot(code)
            qty = math.floor((budget-6)/price/step)*step
            while qty >= minimum and (qty*price+sum(fees(qty*price,'buy').values()) > cash or
                    value+qty*price > (nav-sum(fees(qty*price,'buy').values()))*cfg['max_exposure']/100):
                qty -= step
            if qty < minimum:
                skipped.append(dict(row, reason='仓位或资金不足一个最小申报单位'))
                continue
            try:
                closing = minute_points(data.ticks(code, day), today)[-1]
            except Exception as exc:
                errors.append(dict(date=day, code=code, reason='买入成交数据核验失败 '+str(exc)))
                break
            if qty > closing['volume']*100*.05:
                skipped.append(dict(row, reason='超过收盘分钟成交量5%，不假定全额买入'))
                continue
            gross = money(price*qty)
            fee = money(sum(fees(gross,'buy').values()))
            cash = money(cash-gross-fee)
            cost_fees += fee
            position = dict(code=code, name=row['name'], qty=qty, opened=day, cost=money(gross+fee),
                            average_cost=(gross+fee)/qty, value=gross,
                            plan={'_opened_at':day+' 15:00:00'}, exit_state={})
            positions[code] = position
            value += gross
            trades.append(dict(date=day,time='15:00',code=code,name=row['name'],side='buy',qty=qty,
                               price=price,fees=fee,pnl=None,score=row['rank'],reason='14:30公式入选，按当日收盘价模拟买入'))
        if errors:
            break
        nav = money(cash+sum(position['value'] for position in positions.values()))
        peak = max(peak, nav)
        drawdown = max(drawdown,(peak-nav)/peak*100)
        equity.append(dict(date=day,nav=nav,return_pct=(nav/100000-1)*100,cash=cash,positions=len(positions)))
        if progress:
            progress(day)
    final = equity[-1] if equity else dict(nav=100000,return_pct=0)
    sells = [row for row in trades if row['side']=='sell']
    return dict(id=strategy['id'],name=cfg['name'],config=cfg,complete=not errors,
                return_pct=final['return_pct'] if not errors else None,nav=final['nav'] if not errors else None,
                realized=realized,fees=money(cost_fees),drawdown=drawdown,
                win_rate=sum(row['pnl']>0 for row in sells)/len(sells)*100 if sells else None,
                trades=trades,equity=equity,positions=list(positions.values()),
                selections=selected,skipped=skipped,errors=errors)


class BacktestStore:
    def __init__(self, root, strategy_store):
        self.root, self.strategies = Path(root), strategy_store
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.active = None
        for path in self.root.glob('*/report.json'):
            report = json.loads(path.read_text())
            if report.get('status') == 'running':
                report.update(status='interrupted', progress='服务重启中断了回溯，请重新开始')
                write_json(path, report)

    def list(self):
        rows = [json.loads(path.read_text()) for path in self.root.glob('*/report.json')]
        for row in rows:
            row.pop('results',None)
            row.pop('errors',None)
        return sorted(rows, key=lambda row:row['created'], reverse=True)

    def get(self, run_id):
        if not isinstance(run_id,str) or len(run_id)!=32 or any(c not in '0123456789abcdef' for c in run_id):
            raise ValueError('回溯编号无效')
        with self.lock:
            return json.loads((self.root/run_id/'report.json').read_text())

    def start(self, ids, start, end):
        try:
            left, right = datetime.strptime(start,'%Y-%m-%d'), datetime.strptime(end,'%Y-%m-%d')
        except (TypeError, ValueError):
            raise ValueError('请选择有效起止日期') from None
        if left>right or (right-left).days>90 or (end>clock()[:10] or end==clock()[:10] and clock()[11:16]<'16:00'):
            raise ValueError('区间最多90个自然日，截止日期须为已收盘日期')
        if not isinstance(ids,list) or not 1<=len(ids)<=3 or any(not isinstance(sid,str) for sid in ids) or len(set(ids))!=len(ids):
            raise ValueError('请选择1至3套策略')
        strategies = [copy.deepcopy(self.strategies.get(sid)) for sid in ids]
        for strategy in strategies:
            cfg=strategy['config']
            if strategy['mode']!='live' or cfg['pool'] not in ('market','custom'):
                raise ValueError('历史回溯支持真实模式的全市场或自定义股票池公式')
        with self.lock:
            if self.active:
                raise ValueError('已有回溯运行中，请等待完成')
            run_id=uuid.uuid4().hex
            self.active=run_id
            report=dict(id=run_id,created=clock(),start=start,end=end,
                        status='running',progress='正在读取历史股票池',version=VERSION,
                        strategy_names=[s['config']['name'] for s in strategies],
                        selection_time='14:30',buy_price='close',results=[],errors=[])
            write_json(self.root/run_id/'report.json',report)
            thread=threading.Thread(target=self.run,args=(report,strategies),daemon=True,name='historical-replay')
            thread.start()
        return report

    def run(self, report, strategies):
        path=self.root/report['id']/'report.json'
        def save(message):
            report['progress']=message
            with self.lock:
                write_json(path,report)
        try:
            data=HistoricalData(self.root/'data',report['start'],report['end'])
            roster_path=self.root/'data'/'roster.json'
            roster=json.loads(roster_path.read_text()) if roster_path.exists() else StockUniverse().load(['main','chinext','star'])
            if isinstance(roster,dict):roster=roster['items']
            known_codes = {row['code'] for row in roster}
            for strategy in strategies:
                if strategy['config']['pool'] == 'custom' and set(strategy['config']['codes']) - known_codes:
                    raise ValueError('自定义股票池中存在当前名录无法核验的代码')
            roster=[row for row in roster if any(in_boards(row['code'],s['config']['boards']) and
                    (s['config']['pool']=='market' or row['code'] in s['config'].get('codes',[])) for s in strategies)]
            if not roster:
                raise ValueError('选股范围为空')
            # The broad market index is the session calendar. Never use weekdays.
            r=data.http().get('https://web.ifzq.gtimg.cn/appstock/app/kline/kline',
                              params={'param':'sh000001,day,,,500'},timeout=10).json()['data']['sh000001']['day']
            dates=[row[0] for row in r if report['start']<=row[0]<=report['end']]
            if not dates:raise ValueError('所选日期没有可核验的交易日')
            report['sessions']=dates
            report['universe_count']=len(roster)
            report['universe_note']='当前交易所名录及当前名称回溯，未重建历史退市名单和ST名称变更'
            report['assumptions']=[
                '14:30由当日前7根30分钟K线构造未收盘日K，历史最多499根前复权日K',
                '采用获取时点前复权序列，并按历史当日比例还原价格尺度，复权因子来源差异仍可能影响边界选股',
                '买入按收盘价，资金、排名、最多持仓和仓位上限按保存配置，收盘涨停不假定买入',
                '卖出按历史分笔聚合的完整分钟信号，下一分钟价减0.05%滑点，成交量5%限制，T+1',
                '分时均价由历史分笔成交量加权计算，分笔量与日线量核验，分钟内回封仍有粒度误差',
                '费用沿用模拟账户，收益包含期末持仓市值，回撤按每日收盘权益计算',
                '5交易日开关沿用各套设置，买入日算第1日，第5日14:54起退出，连续封板延持',
                '持有期权益分派需补充股数与现金处理，遇到因子变化会停止该套收益计算']
            matches=[]
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures=[executor.submit(scan_stock,row,strategies,data,dates) for row in roster]
                for index,future in enumerate(as_completed(futures),1):
                    found,errors=future.result()
                    matches.extend(found)
                    report['errors'].extend(errors)
                    if index%50==0:save(f'14:30选股核验 {index}/{len(roster)}，已找到{len(matches)}条入选')
            write_json(self.root/report['id']/'selections.json',matches)
            report['selection_count']=len(matches)
            report['data_error_count']=len(report['errors'])
            unknown = {(error['code'], day) for error in report['errors']
                       for day in ([error['date']] if error['date'] else dates)}
            report['coverage_pct'] = 100 * (1-len(unknown)/(max(1,len(roster))*len(dates)))
            for strategy in strategies:
                save('正在回放 '+strategy['config']['name']+' 的持仓和退出')
                result=replay_strategy(strategy,matches,data,dates,
                                       lambda day:save(strategy['config']['name']+' 回放至 '+day))
                report['results'].append(result)
            report['status']='completed' if not report['errors'] and all(r['complete'] for r in report['results']) else 'partial'
            save('回溯已结束' if report['status']=='completed' else '回溯结束，存在数据缺口，收益仅适用于已覆盖样本')
        except Exception as exc:
            report['status']='failed'
            save('回溯未完成 '+str(exc)[:220])
        finally:
            with self.lock:self.active=None
