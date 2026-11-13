"""Bounded, cached Eastmoney fund-flow snapshots; amounts are always yuan."""
import math
import json
from pathlib import Path
from urllib.parse import urlsplit
from contextlib import nullcontext
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from terminal_engine import TZ, now_text, validate_code
from terminal_tushare import TushareError


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def missing(reason='资金源暂不可用'):
    return dict(status='unavailable', net=None, ratio=None, date=None,
                source='东方财富资金流', retrieved_at=None, reason=reason)


class FundsCooling(RuntimeError):
    """A shared source is cooling down; no network request was sent."""


def parse_stock(payload, code):
    data = payload.get('data') or {}
    if data.get('code') != code:
        raise ValueError('资金代码不匹配')
    rows = {}
    for raw in data.get('klines', []):
        fields = raw.split(',')
        if len(fields) < 11:
            continue
        try:
            datetime.strptime(fields[0], '%Y-%m-%d')
        except ValueError:
            continue
        if number(fields[1]) is not None:
            rows[fields[0]] = dict(date=fields[0], net=number(fields[1]),
                                   ratio=number(fields[6]), large=number(fields[4]),
                                   super_large=number(fields[5]))
    history = [rows[d] for d in sorted(rows)][-5:]
    if not history:
        raise ValueError('资金明细为空')
    last = history[-1]
    positive_days = 0
    for row in reversed(history):
        if row['net'] <= 0:
            break
        positive_days += 1
    return dict(last, status='ok', source='东方财富资金流', retrieved_at=now_text(),
                period='所示交易日累计', history=history, positive_days=positive_days,
                five_day_net=sum(r['net'] for r in history) if len(history) == 5 else None)


def parse_live_stock(payload, code):
    """List-quote field definitions; stock/get uses a different field namespace."""
    rows=(payload.get('data') or {}).get('diff') or []
    if len(rows)!=1 or rows[0].get('f12')!=code: raise ValueError('盘中资金代码不匹配')
    row=rows[0];net,ratio,stamp=map(number,(row.get('f62'),row.get('f184'),row.get('f124')))
    if net is None or ratio is None or not stamp: raise ValueError('盘中资金净额、占比或源时间戳缺失')
    dt=datetime.fromtimestamp(stamp,TZ);now=datetime.now(TZ);age=(now-dt).total_seconds()
    if age < -120 or age>7*86400: raise ValueError('盘中资金源时间无效')
    trading='09:31'<=now.strftime('%H:%M')<='11:30' or '13:01'<=now.strftime('%H:%M')<='15:00'
    stale=dt.date()!=now.date() or trading and age>180
    return dict(status='stale' if stale else 'ok',net=net,ratio=ratio,date=dt.strftime('%Y-%m-%d'),
                as_of=dt.strftime('%H:%M:%S'),source='东方财富盘中主力资金',retrieved_at=now_text(),
                period='源时间所示交易日累计；非分钟增量',
                reason='盘中资金源时间已陈旧，等待更新' if stale else '')


class FundFlowService:
    _network_slots=threading.BoundedSemaphore(4)
    _network_local=threading.local()
    def __init__(self, health_path=None, history_backup=None):
        self.cache = {}
        self.lock = threading.Lock()
        self.key_locks = {}
        self.host_locks = {}
        self.host_states = {}
        self.list_last_start = {}
        self.history_backup=history_backup
        self.health_path = Path(health_path) if health_path else None
        if self.health_path:
            try:
                self.host_states = json.loads(self.health_path.read_text()).get('hosts', {})
                self.host_states = {k:v for k,v in self.host_states.items() if '/' in k}
                # Monotonic deadlines cannot survive a process restart; probe once again.
                for state in self.host_states.values(): state['until'] = 0
            except (OSError, ValueError, TypeError):
                self.host_states = {}

    def health(self):
        with self.lock:
            return [dict(host=host, **{k:v for k,v in state.items() if k!='until'},
                         retry_in=max(0, math.ceil(state.get('until',0)-time.monotonic())))
                    for host,state in sorted(self.host_states.items())]

    def _save_health(self):
        if not self.health_path: return
        try:
            self.health_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.health_path.with_suffix('.tmp')
            temp.write_text(json.dumps({'hosts':self.host_states}, ensure_ascii=False))
            temp.replace(self.health_path)
        except OSError:
            pass  # Diagnostics must never stop the trading worker.

    def _cached(self, key, loader, ttl=25):
        with self.lock:
            key_lock = self.key_locks.setdefault(key, threading.Lock())
        with key_lock:
            stamp, previous = self.cache.get(key, (0, None))
            lifetime = ttl if previous and previous.get('status')=='ok' else min(ttl,25)
            if previous is not None and time.monotonic() - stamp < lifetime:
                return previous
            try:
                result = loader()
            except Exception as exc:
                if isinstance(exc, FundsCooling):
                    reason = str(exc)
                elif isinstance(exc,TushareError):reason=str(exc)
                elif isinstance(exc, requests.HTTPError):
                    reason = '资金接口 HTTP '+str(exc.response.status_code if exc.response is not None else '错误')
                elif isinstance(exc, requests.Timeout): reason = '资金接口连接或读取超时'
                elif isinstance(exc, requests.ConnectionError): reason = '资金接口连接被关闭或不可达'
                elif isinstance(exc, ValueError): reason = '资金响应无效：'+str(exc)[:100]
                else: reason = '资金接口处理失败（'+type(exc).__name__+'）'
                if previous and previous.get('status') in ('ok', 'stale'):
                    result = dict(previous, status='stale', reason=reason+'；保留上次资金快照')
                else:
                    result = missing(reason)
            self.cache[key] = (time.monotonic(), result)
            return result

    def _json(self, url, params):
        parsed=urlsplit(url)
        # Gate by endpoint: an unavailable industry-list route must not disable a
        # working individual live-money route on the same host.
        host = parsed.hostname+parsed.path
        paced = parsed.path.endswith('/clist/get')
        with self.lock:
            host_lock = self.host_locks.setdefault(host, threading.Lock())
            gate = nullcontext() if not paced and self.host_states.get(host,{}).get('status')=='ok' else host_lock
        # One host probe at a time: simultaneous new symbols cannot stampede a failed source.
        with gate:
            with self.lock:
                state = self.host_states.setdefault(host, {})
                remaining = state.get('until',0)-time.monotonic()
                if remaining>0:
                    raise FundsCooling('资金源故障退避中，约'+str(math.ceil(remaining))+'秒后自动复检')
            if paced:
                previous=self.list_last_start.get(host)
                delay=1.2-(time.monotonic()-previous) if previous is not None else 0
                if delay>0:time.sleep(delay)
                self.list_last_start[host]=time.monotonic()
            try:
                payload = self._request_json(url, params)
            except Exception as exc:
                if isinstance(exc, requests.HTTPError):
                    reason='HTTP '+str(exc.response.status_code if exc.response is not None else '错误')
                elif isinstance(exc, requests.Timeout): reason='连接或读取超时'
                elif isinstance(exc, requests.ConnectionError): reason='连接被关闭或不可达'
                else: reason='响应无效（'+type(exc).__name__+'）'
                with self.lock:
                    failures=state.get('failures',0)+1
                    state.update(status='unavailable',failures=failures,
                                 failures_total=state.get('failures_total',0)+1,
                                 outage_since=state.get('outage_since') or now_text(),
                                 last_failure=now_text(),reason=reason,
                                 until=time.monotonic()+min(300,30*2**min(failures-1,4)))
                    self._save_health()
                raise
            with self.lock:
                recovered=state.get('status')=='unavailable'
                state.update(status='ok',failures=0,until=0,last_success=now_text(),reason='响应成功；交易日期仍按各条数据校验')
                if recovered:
                    state['last_outage_since']=state.pop('outage_since',None)
                    state['recovered_at']=now_text()
                    state['recoveries']=state.get('recoveries',0)+1
                self._save_health()
            return payload

    @staticmethod
    def _request_json(url, params):
        if not hasattr(FundFlowService._network_local,'session'):
            FundFlowService._network_local.session=requests.Session()
        for attempt in range(2):
            try:
                with FundFlowService._network_slots:
                    response=FundFlowService._network_local.session.get(url,params=params,timeout=(5,12),
                                headers={'User-Agent':'Mozilla/5.0'})
                    response.raise_for_status()
                    return response.json()
            except (requests.Timeout,requests.ConnectionError):
                if attempt:raise
                time.sleep(.4)

    def stock(self, code):
        validate_code(code)
        def history():
            payload = self._json('https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get',
                                 dict(lmt=5, klt=101, secid=f"{1 if code.startswith('6') else 0}.{code}",
                                      fields1='f1,f2,f3,f7',
                                      fields2='f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65'))
            return parse_stock(payload, code)
        def live():
            payload=self._json('https://push2.eastmoney.com/api/qt/ulist.np/get',
                               dict(secids=f"{1 if code.startswith('6') else 0}.{code}",fltt=2,invt=2,
                                    fields='f12,f14,f62,f184,f124',_=int(time.time()*1000)))
            return parse_live_stock(payload,code)
        current=self._cached('stock-live:'+code,live)
        past=self._history('stock-history:'+code,history,code)
        rows=[dict(r) for r in past.get('history',[]) if r['date']<str(current.get('date') or '')]
        if current.get('status')=='ok':
            rows.append({k:current[k] for k in ('date','net','ratio')})
        rows=rows[-5:];positive=0
        for row in reversed(rows):
            if row['net']<=0:break
            positive+=1
        return dict(current,history=rows,positive_days=positive if past.get('status')=='ok' and current.get('status')=='ok' else None,
                    five_day_net=sum(r['net'] for r in rows) if len(rows)==5 and past.get('status')=='ok' and current.get('status')=='ok' else None,
                    history_status=past.get('status'),history_reason=past.get('reason',''),
                    history_source=past.get('source'),history_period=past.get('period'))

    def _history(self,key,loader,code,sector=False):
        primary=self._cached(key,loader,ttl=600)
        if primary.get('status')=='ok' or not self.history_backup or not self.history_backup.configured():return primary
        backup=self._cached(key+':tushare',lambda:self.history_backup.money_history(code,sector),ttl=900)
        if backup.get('status')=='ok':return backup
        return dict(primary,backup_reason=backup.get('reason','Tushare历史资金未确认'))

    def sectors(self):
        def page(pn):
            data = self._json('https://push2.eastmoney.com/api/qt/clist/get',
                              dict(pn=pn, pz=20, po=1, np=1, fltt=2, invt=2, fid='f3',
                                   fs='m:90+t:2', fields='f12,f14,f3,f6,f62,f184,f66,f72,f104,f105,f106,f124')).get('data')
            if not data or not isinstance(data.get('diff'), list):
                raise ValueError('行业资金为空')
            return data
        def load():
            first = page(1)
            pages = (int(first['total']) + 19) // 20
            if not 1 <= pages <= 50:
                raise ValueError('行业资金分页异常')
            rows = first['diff'][:]
            if pages > 1:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    for data in pool.map(page, range(2, pages + 1)):
                        rows.extend(data['diff'])
            if len({r.get('f12') for r in rows}) != int(first['total']):
                raise ValueError('行业资金分页不完整')
            result = {}
            for row in rows:
                net, stamp = number(row.get('f62')), number(row.get('f124'))
                if net is None or not stamp or not row.get('f14'):
                    continue
                dt = datetime.fromtimestamp(stamp, TZ)
                result[row['f14']] = dict(status='ok', net=net, ratio=number(row.get('f184')),
                                          date=dt.strftime('%Y-%m-%d'), as_of=dt.strftime('%H:%M:%S'),
                                          large=number(row.get('f72')), super_large=number(row.get('f66')),
                                          source='东方财富同名行业资金', period='所示交易日累计',
                                          retrieved_at=now_text(), sector_code=row['f12'],
                                          name=row['f14'], pct=number(row.get('f3')), amount=number(row.get('f6')),
                                          rise_count=number(row.get('f104')), fall_count=number(row.get('f105')),
                                          flat_count=number(row.get('f106')))
            if not result:
                raise ValueError('行业资金明细为空')
            return dict(status='ok', items=result)
        return self._cached('sectors', load, ttl=60)

    def sector_history(self, code):
        if not code.startswith('BK') or not code[2:].isdigit():
            return missing('行业代码无效')
        def load():
            payload = self._json('https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get',
                                 dict(lmt=5, klt=101, secid='90.'+code, fields1='f1,f2,f3,f7',
                                      fields2='f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65'))
            return parse_stock(payload, code)
        return self._history('sector-history:'+code,load,code,sector=True)

    def amplitudes(self, codes):
        """Tencent quote high/low relative to previous close, never minute range."""
        result = {}
        try:
            symbols = [('sh' if c.startswith('6') else 'bj' if c[0] in '489' else 'sz') + c for c in codes]
            response = requests.get('https://qt.gtimg.cn/q=' + ','.join(symbols), timeout=5)
            response.raise_for_status()
            for line in response.content.decode('gbk').split(';'):
                fields = line.split('~')
                if len(fields) <= 34 or fields[2] not in codes:
                    continue
                previous, high, low = map(number, (fields[4], fields[33], fields[34]))
                if previous and high is not None and low is not None and high >= low > 0:
                    result[fields[2]] = dict(amplitude=round((high-low)/previous*100, 2),
                                              quote_date=datetime.strptime(fields[30], '%Y%m%d%H%M%S').strftime('%Y-%m-%d'))
        except Exception:
            pass
        return result


def demo_funds(code='000001'):
    net = (int(code) % 7 - 2) * 18000000.0
    days = ['2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06', '2026-11-09']
    payload = {'data': {'code': code, 'klines': [f'{date},{net},0,0,{net*.4},{net*.6},2,0,0,0,0' for date in days]}}
    return dict(parse_stock(payload, code), source='合成演示资金 · 非真实行情')
