"""AKShare-backed money data; preserve upstream dates, units and completeness."""
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from terminal_engine import TZ, now_text, validate_code
from terminal_funds import FundFlowService, missing, number
from terminal_mainline import merge_history

SOURCE = 'AKShare · 东方财富'
SLOTS = threading.BoundedSemaphore(2)


class AKShareBusy(TimeoutError):
    pass


def call_akshare(method, **params):
    if not SLOTS.acquire(timeout=5):
        raise AKShareBusy('AKShare采集繁忙，稍后复检')
    try:
        process = subprocess.run([sys.executable, str(Path(__file__).with_name('terminal_akshare_worker.py'))],
            input=json.dumps({'method': method, 'params': params}), capture_output=True,
            text=True, timeout=40, check=True)
        result = json.loads(process.stdout)
        if not result.get('ok'):
            raise ValueError('AKShare：'+result.get('error', '接口失败'))
        return result['data']
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError('AKShare采集超时') from exc
    finally:
        SLOTS.release()


def source_time(stamp):
    stamp = number(stamp)
    if not stamp:
        raise ValueError('AKShare源时间戳缺失')
    dt, now = datetime.fromtimestamp(stamp, TZ), datetime.now(TZ)
    age = (now-dt).total_seconds()
    trading = now.weekday() < 5 and ('09:31' <= now.strftime('%H:%M') <= '11:30' or '13:01' <= now.strftime('%H:%M') <= '15:00')
    if age < -120 or age > 7*86400:
        raise ValueError('AKShare源时间戳无效')
    return dict(date=dt.strftime('%Y-%m-%d'), as_of=dt.strftime('%H:%M:%S'),
                status='stale' if dt.date()!=now.date() or trading and age>180 else 'ok')


def snapshot(data, scope):
    found = [s for s in data.get('snapshots', []) if scope(s['scope'])]
    if len(found) != 1:
        raise ValueError('AKShare来源范围未核验')
    item = found[0]
    if len(item['rows']) != item['total'] or len({str(r['f12']) for r in item['rows']}) != item['total']:
        raise ValueError('AKShare分页不完整')
    return item['rows']


def money_row(row, raw, net_key, ratio_key):
    net, ratio = number(row.get(net_key)), number(row.get(ratio_key))
    if net is None or ratio is None or net != number(raw.get('f62')) or ratio != number(raw.get('f184')):
        raise ValueError('AKShare资金字段与来源元数据不一致')
    return dict(source_time(raw.get('f124')), net=net, ratio=ratio,
                large=number(row.get('今日大单净流入-净额')),
                super_large=number(row.get('今日超大单净流入-净额')),
                source=SOURCE+'主力资金', retrieved_at=now_text(), period='源时间所示交易日累计；金额元，净占比%')


def history_rows(data, code):
    if not any(str(h.get('code'))==code and str(h.get('requested', '')).endswith('.'+code) for h in data.get('histories', [])):
        raise ValueError('AKShare资金历史代码不匹配')
    rows = {}
    today = datetime.now(TZ).date()
    for row in data['rows']:
        date = str(row.get('日期', ''))[:10]
        parsed = datetime.strptime(date, '%Y-%m-%d').date()
        net, ratio = number(row.get('主力净流入-净额')), number(row.get('主力净流入-净占比'))
        if parsed > today or net is None or ratio is None or date in rows:
            raise ValueError('AKShare资金历史日期或金额无效')
        rows[date] = dict(date=date, net=net, ratio=ratio,
                          large=number(row.get('大单净流入-净额')),
                          super_large=number(row.get('超大单净流入-净额')))
    records = [rows[d] for d in sorted(rows)][-5:]
    if not records:
        raise ValueError('AKShare资金历史为空')
    positive = 0
    for row in reversed(records):
        if row['net']<=0: break
        positive += 1
    return dict(records[-1], status='ok', history=records, positive_days=positive,
                three_day_net=sum(r['net'] for r in records[-3:]) if len(records)>=3 else None,
                five_day_net=sum(r['net'] for r in records) if len(records)==5 else None,
                source=SOURCE+'资金历史', retrieved_at=now_text(), period='源日期所示交易日累计；金额元')


class AKShareFundFlowService(FundFlowService):
    source_label = SOURCE+'行业、成分股与资金流'

    def _fetch(self, method, ttl=60, **params):
        def load():
            host = 'akshare/'+method
            with self.lock:
                state = self.host_states.setdefault(host, {})
                if state.get('until', 0)>time.monotonic():
                    raise TimeoutError('AKShare接口退避中')
            try:
                data = call_akshare(method, **params)
            except AKShareBusy:
                # Local capacity is not an upstream outage; do not trip the
                # endpoint circuit for other symbols or channels.
                raise
            except Exception as exc:
                with self.lock:
                    failures = state.get('failures', 0)+1
                    state.update(status='unavailable', failures=failures, last_failure=now_text(),
                                 failures_total=state.get('failures_total',0)+1,
                                 outage_since=state.get('outage_since') or now_text(),
                                 reason=str(exc) if str(exc).startswith('AKShare') else 'AKShare调用失败（'+type(exc).__name__+'）',
                                 until=time.monotonic()+min(300, 30*2**min(failures-1, 4)))
                    self._save_health()
                raise
            with self.lock:
                if state.get('status')=='unavailable':
                    state.update(recovered_at=now_text(),last_outage_since=state.pop('outage_since',None),recoveries=state.get('recoveries',0)+1)
                state.update(status='ok', failures=0, until=0, last_success=now_text(), reason='AKShare返回成功；日期与覆盖另行校验')
                self._save_health()
            return dict(status='ok', data=data)
        key = 'akshare:'+method+json.dumps(params, sort_keys=True)
        saved = self._cached(key, load, ttl=ttl)
        if saved['status'] != 'ok':
            raise ValueError('AKShare数据不可用：'+saved.get('reason', '等待复检'))
        return saved['data']

    def sectors(self):
        def load():
            data = self._fetch('stock_sector_fund_flow_rank', indicator='今日', sector_type='行业资金流')
            raw = {r['f14']: r for r in snapshot(data, lambda s: 'm:90' in s and 't:2' in s)}
            boards = self._fetch('stock_board_industry_name_em', ttl=300)
            names = {r['板块名称']: r for r in boards['rows']}
            board_meta = {r['f12']:r for r in snapshot(boards, lambda s: 'm:90' in s and 't:2' in s)}
            if len(data['rows']) != len(raw) or len({r['名称'] for r in data['rows']})!=len(raw):
                raise ValueError('AKShare行业排名不完整或重复')
            result = {}
            for row in data['rows']:
                name = row['名称'];meta = raw[name];board = names.get(name, {})
                if board.get('板块代码') != meta['f12']:
                    raise ValueError('AKShare行业名称与代码不匹配')
                f = money_row(row, meta, '今日主力净流入-净额', '今日主力净流入-净占比')
                if f['status'] != 'ok': continue
                board_time = source_time(board_meta[meta['f12']].get('f124'))
                if board_time['status'] != 'ok' or board_time['date'] != f['date']: continue
                # Breadth is filled only after complete dated members are fetched.
                result[name] = dict(f, name=name, sector_code=meta['f12'], pct=number(row.get('今日涨跌幅')),
                    rise_count=number(board.get('上涨家数')), fall_count=number(board.get('下跌家数')), flat_count=None)
            if not result: raise ValueError('AKShare行业资金没有新鲜数据')
            return dict(status='ok', items=result)
        return self._cached('akshare-sectors', load, ttl=60)

    def sector_history(self, code):
        def load():
            sector = next((r for r in self.sectors().get('items', {}).values() if r['sector_code']==code), None)
            if not sector: raise ValueError('AKShare行业代码未确认')
            return history_rows(self._fetch('stock_sector_fund_flow_hist', ttl=600, symbol=sector['name']), code)
        return self._cached('akshare-sector-history:'+code, load, ttl=600)

    def stock(self, code):
        validate_code(code)
        def load_live():
            data = self._fetch('stock_individual_fund_flow_rank', indicator='今日')
            raw = {str(r['f12']):r for r in snapshot(data, lambda s: 'm:0' in s or 'm:1' in s)}
            rows = {str(r['代码']).zfill(6):r for r in data['rows']}
            if len(data['rows'])!=len(rows) or len(rows)!=len(raw) or code not in rows or code not in raw:
                raise ValueError('AKShare个股排名不完整或代码缺失')
            return money_row(rows[code], raw[code], '今日主力净流入-净额', '今日主力净流入-净占比')
        live = self._cached('akshare-stock-live:'+code, load_live, ttl=60)
        def load_history():
            data = self._fetch('stock_individual_fund_flow', ttl=600, stock=code,
                market='sh' if code.startswith('6') else 'bj' if code[0] in '489' else 'sz')
            return history_rows(data, code)
        past = self._cached('akshare-stock-history:'+code, load_history, ttl=600)
        if live.get('status') != 'ok' and past.get('status') == 'ok':
            return dict(past, status='unverified', as_of=None, history_status='ok',
                        history_source=past.get('source'), history_period=past.get('period'),
                        reason='仅有AKShare日期级资金历史，盘中资金源时间未核验：'+live.get('reason','等待复检'))
        merged = merge_history(live, past)
        merged.update(history_status=past['status'], history_source=past.get('source'), history_period=past.get('period'), history_reason=past.get('reason',''))
        return merged

    def members(self, code):
        sector = next((r for r in self.sectors().get('items', {}).values() if r['sector_code']==code), None)
        if not sector: raise ValueError('AKShare行业代码未确认')
        data = self._fetch('stock_sector_fund_flow_summary', symbol=sector['name'], indicator='今日')
        raw = {str(r['f12']):r for r in snapshot(data, lambda s: s=='b:'+code)}
        quotes = self._fetch('stock_board_industry_cons_em', symbol=code)
        quote_meta = {str(r['f12']):r for r in snapshot(quotes, lambda s: s=='b:'+code)}
        quote_rows = {str(r['代码']).zfill(6):r for r in quotes['rows']}
        if len(quotes['rows'])!=len(quote_rows) or set(quote_rows)!=set(raw) or set(quote_meta)!=set(raw):
            raise ValueError('AKShare成分行情与资金范围不一致')
        if len(data['rows'])!=len(raw): raise ValueError('AKShare成分股不完整')
        result=[];seen=set()
        for row in data['rows']:
            stock=str(row['代码']).zfill(6)
            if stock in seen or stock not in raw: raise ValueError('AKShare成分股代码重复或错配')
            seen.add(stock)
            f=money_row(row, raw[stock], '今日主力净流入-净额', '今日主力净流入-净占比')
            pct, price=number(row.get('今天涨跌幅')),number(row.get('最新价'))
            if f['status']!='ok' or f['date']!=sector['date'] or pct is None or not price:
                raise ValueError('AKShare成分股日期、资金或价格不完整')
            quote_time = source_time(quote_meta[stock].get('f124'))
            if quote_time['status']!='ok' or quote_time['date']!=f['date']:
                raise ValueError('AKShare成分行情时间未确认')
            q=quote_rows[stock];amount=number(q.get('成交额'))
            result.append(dict(code=stock,name=row['名称'],price=price,pct=pct,quote_date=f['date'],funds=f,
                               quote_as_of=quote_time['as_of'],quote_source=SOURCE+'成分行情',
                               amount=amount/1e8 if amount is not None else None,
                               volume_ratio=number(q.get('量比')),amplitude=number(q.get('振幅'))))
        return result

    def limit_pool(self, date):
        def load():
            data=self._fetch('stock_zt_pool_em', date=date.replace('-',''))
            if len(data.get('pools',[]))!=1:raise ValueError('AKShare涨停池源日期缺失')
            meta=data['pools'][0]
            if meta['date']!=date.replace('-','') or meta['count']!=len(data['rows']) or meta['total'] is not None and meta['total']!=len(data['rows']):
                raise ValueError('AKShare涨停池日期或数量不匹配')
            items={}
            for row in data['rows']:
                code=str(row['代码']).zfill(6)
                if code in items:raise ValueError('AKShare涨停池代码重复')
                items[code]=dict(streak=int(row['连板数']),breaks=int(row['炸板次数']),first_seal=int(row['首次封板时间']))
            return dict(status='ok',date=date,items=items)
        return self._cached('akshare-limit-pool:'+date,load)
