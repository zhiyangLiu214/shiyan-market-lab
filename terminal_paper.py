"""Persistent forward-only paper ledger. No brokerage connection or historical fills."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
INITIAL = 100000.0
VERSION = 'paper-forward-v2-dynamic'


def clock():
    return datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')


def money(v):
    return float(Decimal(str(v)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def session(stamp):
    d = datetime.strptime(stamp[:19], '%Y-%m-%d %H:%M:%S')
    t = d.strftime('%H:%M:%S')
    return d.weekday() < 5 and ('09:31:00' <= t < '11:30:00' or '13:01:00' <= t < '14:57:00')


def lot(code):
    return (200, 1) if code.startswith('688') else (100, 100)


def fees(gross, side):
    return dict(commission=max(5., money(gross * .00025)),
                transfer=money(gross * .00001), stamp=money(gross * .0005) if side == 'sell' else 0.)


class PaperLedger:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path, self.lock = str(path), threading.RLock()
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS runs(mode TEXT PRIMARY KEY, started TEXT, enabled INTEGER DEFAULT 1, status TEXT);
            CREATE TABLE IF NOT EXISTS accounts(mode TEXT, id TEXT, cash REAL, realized REAL DEFAULT 0,
              peak REAL, drawdown REAL DEFAULT 0, PRIMARY KEY(mode,id));
            CREATE TABLE IF NOT EXISTS lots(mode TEXT, account TEXT, code TEXT, name TEXT, qty INTEGER,
              cost REAL, day TEXT, plan TEXT, price REAL, quote_at TEXT);
            CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, mode TEXT, account TEXT, code TEXT,
              name TEXT, side TEXT, qty INTEGER, created TEXT, observed TEXT, status TEXT, reason TEXT,
              plan TEXT, reference REAL, source TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY, mode TEXT, account TEXT, code TEXT,
              name TEXT, side TEXT, qty INTEGER, price REAL, gross REAL, fees REAL, realized REAL,
              at TEXT, order_id TEXT UNIQUE, reason TEXT, source TEXT, fee_detail TEXT);
            CREATE TABLE IF NOT EXISTS equity(mode TEXT, account TEXT, at TEXT, nav REAL,
              PRIMARY KEY(mode,account,at));
            CREATE TABLE IF NOT EXISTS candidates(mode TEXT, account TEXT, at TEXT, payload TEXT,
              PRIMARY KEY(mode,account));
            CREATE TABLE IF NOT EXISTS rotation_watch(mode TEXT, account TEXT, code TEXT,
              first_missing TEXT, last_snapshot TEXT, misses INTEGER, PRIMARY KEY(mode,account,code));
            CREATE TABLE IF NOT EXISTS pool_diagnostics(mode TEXT, account TEXT, at TEXT, state TEXT, reason TEXT, count INTEGER);
            CREATE TABLE IF NOT EXISTS paper_events(mode TEXT, at TEXT, text TEXT);
            ''')
            if 'cap' not in [r[1] for r in db.execute('PRAGMA table_info(accounts)')]:
                db.execute('ALTER TABLE accounts ADD COLUMN cap REAL DEFAULT 70')

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=20)
            db.row_factory = sqlite3.Row
            try:
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def start(self, mode, now=None, accounts=('main','scan')):
        now = now or clock()
        if not accounts or len(set(accounts))!=len(accounts) or any(a not in ('main','scan') for a in accounts):
            raise ValueError('账户配置无效')
        with self.db() as db:
            if db.execute('SELECT 1 FROM runs WHERE mode=?', (mode,)).fetchone():
                return
            db.execute('INSERT INTO runs VALUES(?,?,1,?)', (mode, now, '已启动，等待新行情'))
            for account in accounts:
                db.execute('INSERT INTO accounts(mode,id,cash,realized,peak,drawdown) VALUES(?,?,?,0,?,0)', (mode, account, INITIAL, INITIAL))
                db.execute('INSERT INTO equity VALUES(?,?,?,?)', (mode, account, now, INITIAL))

    def toggle(self, mode, enabled):
        with self.db() as db:
            db.execute('UPDATE runs SET enabled=?,status=? WHERE mode=?',
                       (int(enabled), '自动策略已开启' if enabled else '自动策略已暂停；待成交委托继续核对', mode))

    def set_cap(self, mode, account, cap):
        if account not in ('main', 'scan') or not number(cap) or not 10 <= cap <= 100:
            raise ValueError('仓位上限须在10%至100%之间')
        with self.db() as db:
            db.execute('UPDATE accounts SET cap=? WHERE mode=? AND id=?', (cap, mode, account))
            db.execute('INSERT INTO paper_events VALUES(?,?,?)', (mode, clock(), f'{account}仓位上限调整为{cap}%'))

    def status(self, mode, text):
        with self.db() as db:
            db.execute('UPDATE runs SET status=? WHERE mode=?', (text[:300], mode))

    def capture(self, mode, account, rows, at=None):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,?,?)',
                       (mode, account, at or clock(), json.dumps(rows, ensure_ascii=False)))

    def pool_status(self, mode, account, state, reason, count=0):
        with self.db() as db:
            db.execute('INSERT INTO pool_diagnostics VALUES(?,?,?,?,?,?)', (mode, account, clock(), state, reason[:600], count))
            db.execute("DELETE FROM pool_diagnostics WHERE at < ?", ((datetime.strptime(clock(), '%Y-%m-%d %H:%M:%S')-timedelta(days=14)).strftime('%Y-%m-%d %H:%M:%S'),))

    def pool_absence(self, mode, account, positions, codes, snapshot):
        """Only distinct, successfully completed source snapshots count as absence."""
        result = {}
        with self.db() as db:
            for code in positions:
                if code in codes:
                    db.execute('DELETE FROM rotation_watch WHERE mode=? AND account=? AND code=?',(mode,account,code))
                    continue
                row = db.execute('SELECT * FROM rotation_watch WHERE mode=? AND account=? AND code=?',(mode,account,code)).fetchone()
                if not row:
                    db.execute('INSERT INTO rotation_watch VALUES(?,?,?,?,?,1)',(mode,account,code,snapshot,snapshot))
                elif snapshot > row['last_snapshot']:
                    db.execute('UPDATE rotation_watch SET last_snapshot=?,misses=misses+1 WHERE mode=? AND account=? AND code=?',(snapshot,mode,account,code))
                row = db.execute('SELECT * FROM rotation_watch WHERE mode=? AND account=? AND code=?',(mode,account,code)).fetchone()
                span = (datetime.strptime(row['last_snapshot'],'%Y-%m-%d %H:%M:%S')-datetime.strptime(row['first_missing'],'%Y-%m-%d %H:%M:%S')).total_seconds()
                result[code] = dict(row, mature=row['misses']>=3 and span>=120)
            for row in db.execute('SELECT code FROM rotation_watch WHERE mode=? AND account=?',(mode,account)).fetchall():
                if row['code'] not in positions:
                    db.execute('DELETE FROM rotation_watch WHERE mode=? AND account=? AND code=?',(mode,account,row['code']))
        return result

    def _positions(self, db, mode, account):
        lots = db.execute('SELECT rowid,* FROM lots WHERE mode=? AND account=? AND qty>0', (mode, account)).fetchall()
        result = {}
        for row in lots:
            p = result.setdefault(row['code'], dict(code=row['code'], name=row['name'], qty=0, cost=0., value=0., lots=[]))
            p['qty'] += row['qty']; p['cost'] += row['cost']; p['value'] += row['qty'] * row['price']
            p['lots'].append(dict(row))
        for p in result.values():
            p['average_cost'] = p['cost']/p['qty']
            p['unrealized'] = money(p['value']-p['cost'])
        return result

    def _exposure(self, db, mode, account):
        a = db.execute('SELECT * FROM accounts WHERE mode=? AND id=?', (mode, account)).fetchone()
        positions = self._positions(db, mode, account)
        value = sum(p['value'] for p in positions.values())
        pending = db.execute("SELECT code,qty,reference FROM orders WHERE mode=? AND account=? AND side='buy' AND status='pending'", (mode, account)).fetchall()
        return a, positions, value, list(pending)

    def submit(self, mode, account, code, name, side, qty, reference, observed, plan, reason, source='manual', now=None):
        now = now or clock()
        if account not in ('main', 'scan') or side not in ('buy', 'sell'):
            raise ValueError('账户或买卖方向无效')
        if not isinstance(code, str) or len(code) != 6 or not code.isdigit() or code[0] not in '036':
            raise ValueError('本期仅支持沪深A股，不含北交所')
        if 'ST' in name.upper() or not number(reference) or reference <= 0:
            raise ValueError('ST或无有效行情标的不参与当前模拟')
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise ValueError('股数须为正整数')
        minimum, step = lot(code)
        with self.db() as db:
            run = db.execute('SELECT * FROM runs WHERE mode=?', (mode,)).fetchone()
            if not run:
                raise ValueError('请先同时启动两个模拟账户')
            if now < run['started'] or not session(now):
                raise ValueError('仅在连续竞价模拟时段接受委托；收盘后不使用旧价成交')
            if not observed or observed[:10] != now[:10] or now[:16] < observed[:16]:
                raise ValueError('行情日期或时间不匹配')
            if (datetime.strptime(now, '%Y-%m-%d %H:%M:%S')-datetime.strptime(observed[:16], '%Y-%m-%d %H:%M')).total_seconds() > 180:
                raise ValueError('行情已过期，不能提交委托')
            if db.execute("SELECT 1 FROM orders WHERE mode=? AND account=? AND code=? AND status='pending'", (mode, account, code)).fetchone():
                raise ValueError('该股已有待成交委托')
            a, positions, value, pending = self._exposure(db, mode, account)
            if side == 'buy':
                if qty < minimum or (qty-minimum) % step:
                    raise ValueError('买入股数不符合最小申报单位')
                codes = set(positions) | {p['code'] for p in pending} | {code}
                if len(codes) > 5:
                    raise ValueError('每户最多持仓5只（含待买入）')
                estimate = money(qty*reference*1.0005)
                reserved = sum(p['qty']*p['reference']*1.0005+sum(fees(p['qty']*p['reference']*1.0005, 'buy').values()) for p in pending)
                fee = sum(fees(estimate, side).values())
                nav = a['cash']+value
                if value+sum(p['qty']*p['reference']*1.0005 for p in pending)+estimate > (a['cap']/100)*(nav-fee)+.001:
                    raise ValueError('买入后预计总仓位将超过当前仓位上限')
                if estimate+fee+reserved > a['cash']:
                    raise ValueError('可用资金不足')
            else:
                p = positions.get(code)
                available = sum(l['qty'] for l in p['lots'] if l['day'] < now[:10]) if p else 0
                if qty > available:
                    raise ValueError('可卖数量不足：当日买入部分受T+1限制')
                if not (qty == available or qty >= minimum and (qty-minimum) % step == 0):
                    raise ValueError('卖出股数不符合申报单位，零股须一次卖出')
            plan = dict(plan or {})
            if side == 'buy' and code not in positions:
                plan['_opened_at'] = now
            oid = uuid.uuid4().hex
            db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (oid, mode, account, code, name[:40], side, qty, now, observed, 'pending', reason[:500], json.dumps(plan), reference, source, '等待委托后的新分钟收盘；不保证成交'))
            return oid

    def cancel(self, mode, oid, reason='已撤销'):
        with self.db() as db:
            db.execute("UPDATE orders SET status='cancelled',result=? WHERE mode=? AND id=? AND status='pending'", (reason[:300], mode, oid))

    def update(self, mode, quotes, now=None, buy_checks=None):
        now = now or clock()
        with self.db() as db:
            for code, quote in quotes.items():
                if not quote.get('valid') or not number(quote.get('price')) or quote['price'] <= 0:
                    continue
                db.execute('UPDATE lots SET price=?,quote_at=? WHERE mode=? AND code=? AND qty>0', (quote['price'], quote['as_of'], mode, code))
            pending = db.execute("SELECT * FROM orders WHERE mode=? AND status='pending' ORDER BY created,id", (mode,)).fetchall()
            for order in pending:
                if order['created'][:10] < now[:10] or now[11:19] >= '15:00:00':
                    db.execute("UPDATE orders SET status='expired',result='当日未成交，已到期' WHERE id=?", (order['id'],)); continue
                if not session(now):
                    continue
                if order['side']=='buy' and buy_checks is not None and not buy_checks.get(order['id'],False):
                    db.execute('UPDATE orders SET result=? WHERE id=?', ('筛选或入场条件尚未复核，暂停成交',order['id']))
                    continue
                q = quotes.get(order['code'], {})
                if not q.get('valid'):
                    continue
                # Use the latest newly observed completed minute, never backfill missed execution.
                points = [p for p in q.get('points', []) if q['date']+' '+p['time']+':00' > order['created'] and q['date']+' '+p['time']+':00' <= now and session(q['date']+' '+p['time']+':00')]
                if not points:
                    continue
                point = points[-1]
                # Conservative: reject limit-price touches without order-book evidence.
                raw = point['price']; side = order['side']; qty = order['qty']
                if not number(q.get('prev_close')) or q['prev_close'] <= 0:
                    db.execute('UPDATE orders SET result=? WHERE id=?', ('昨收缺失，无法核对涨跌停，等待', order['id'])); continue
                limit = .2 if order['code'].startswith(('30', '688')) else .1
                upper, lower = money(q['prev_close']*(1+limit)), money(q['prev_close']*(1-limit))
                if (side == 'buy' and raw >= upper) or (side == 'sell' and raw <= lower):
                    db.execute('UPDATE orders SET result=? WHERE id=?', ('触及涨跌停，缺委托队列，不假定能成交', order['id'])); continue
                if not number(point.get('volume')) or qty > point['volume']*100*.05:
                    db.execute('UPDATE orders SET result=? WHERE id=?', ('超过该分钟成交量5%，不假定全额成交', order['id'])); continue
                price = money(raw*(1.0005 if side == 'buy' else .9995))
                price = min(price, upper) if side == 'buy' else max(price, lower)
                gross = money(qty*price); fee_detail = fees(gross, side); fee = money(sum(fee_detail.values()))
                a, positions, value, _ = self._exposure(db, mode, order['account'])
                stamp = q['date']+' '+point['time']+':00'
                realized = 0.
                if side == 'buy':
                    nav = a['cash']+value
                    if (len(set(positions)|{order['code']}) > 5 or gross+fee > a['cash'] or value+gross > (a['cap']/100)*(nav-fee)+.001):
                        db.execute("UPDATE orders SET status='rejected',result='成交前再次检查：资金、5只或仓位上限约束未通过' WHERE id=?", (order['id'],)); continue
                    # A later attempt must not use a future mark already loaded by this update.
                    db.execute('UPDATE accounts SET cash=cash-? WHERE mode=? AND id=?', (gross+fee, mode, order['account']))
                    db.execute('INSERT INTO lots VALUES(?,?,?,?,?,?,?,?,?,?)', (mode, order['account'], order['code'], order['name'], qty, gross+fee, q['date'], order['plan'], price, stamp))
                else:
                    p = positions.get(order['code'])
                    available = sum(l['qty'] for l in p['lots'] if l['day'] < q['date']) if p else 0
                    if qty > available:
                        db.execute("UPDATE orders SET status='rejected',result='T+1可卖数量不足' WHERE id=?", (order['id'],)); continue
                    remain, basis = qty, 0.
                    for l in sorted(p['lots'], key=lambda l: (l['day'], l['rowid'])):
                        if l['day'] >= q['date'] or not remain: continue
                        take = min(remain, l['qty']); used = l['cost']*take/l['qty']; basis += used; remain -= take
                        db.execute('UPDATE lots SET qty=qty-?,cost=cost-? WHERE rowid=?', (take, used, l['rowid']))
                    realized = money(gross-fee-basis)
                    db.execute('UPDATE accounts SET cash=cash+?,realized=realized+? WHERE mode=? AND id=?', (gross-fee, realized, mode, order['account']))
                db.execute('INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, mode, order['account'], order['code'], order['name'], side, qty, price, gross, fee, realized, stamp, order['id'], order['reason'], order['source'], json.dumps(fee_detail)))
                db.execute("UPDATE orders SET status='filled',result=? WHERE id=?", (f'{stamp} 模拟成交 {price}', order['id']))
            # Mark all positions at the newest available price after processing fills.
            for code, q in quotes.items():
                if q.get('valid'):
                    db.execute('UPDATE lots SET price=?,quote_at=? WHERE mode=? AND code=? AND qty>0', (q['price'], q['as_of'], mode, code))
            for a in db.execute('SELECT * FROM accounts WHERE mode=?', (mode,)).fetchall():
                value = sum(p['value'] for p in self._positions(db, mode, a['id']).values()); nav = money(a['cash']+value)
                peak = max(a['peak'], nav); dd = max(a['drawdown'], (peak-nav)/peak*100)
                db.execute('UPDATE accounts SET peak=?,drawdown=? WHERE mode=? AND id=?', (peak, dd, mode, a['id']))
                db.execute('INSERT OR REPLACE INTO equity VALUES(?,?,?,?)', (mode, a['id'], now, nav))

    def snapshot(self, mode, now=None):
        now = now or clock()
        with self.db() as db:
            run = db.execute('SELECT * FROM runs WHERE mode=?', (mode,)).fetchone()
            candidates = {}
            for row in db.execute('SELECT * FROM candidates WHERE mode=?', (mode,)):
                candidates[row['account']] = dict(at=row['at'], items=json.loads(row['payload']))
            accounts = []
            for a in db.execute('SELECT * FROM accounts WHERE mode=? ORDER BY id', (mode,)):
                positions = list(self._positions(db, mode, a['id']).values())
                for p in positions:
                    p['available'] = sum(l['qty'] for l in p['lots'] if l['day'] < now[:10])
                    p['quote_at'] = min(l['quote_at'] for l in p['lots'])
                    observation=db.execute('SELECT misses,last_snapshot FROM rotation_watch WHERE mode=? AND account=? AND code=?',(mode,a['id'],p['code'])).fetchone()
                    p['pool_observation']=dict(observation) if observation else None
                    p['plan'] = json.loads(p['lots'][0]['plan'])
                    p['last_buy_observed'] = db.execute("SELECT MAX(observed) FROM orders WHERE mode=? AND account=? AND code=? AND side='buy' AND status='filled'",(mode,a['id'],p['code'])).fetchone()[0]
                    p['profit_taken'] = bool(db.execute("SELECT 1 FROM trades WHERE mode=? AND account=? AND code=? AND side='sell' AND at>=? AND reason LIKE '%首压力%' LIMIT 1",(mode,a['id'],p['code'],p['plan'].get('_opened_at','9999'))).fetchone())
                    p.pop('lots')
                value = money(sum(p['value'] for p in positions)); nav = money(a['cash']+value)
                rows = [dict(t) for t in db.execute('SELECT * FROM trades WHERE mode=? AND account=? ORDER BY at DESC,rowid DESC LIMIT 100', (mode, a['id']))]
                orders = [dict(o) for o in db.execute('SELECT * FROM orders WHERE mode=? AND account=? ORDER BY created DESC,rowid DESC LIMIT 100', (mode, a['id']))]
                closed = list(db.execute("SELECT realized FROM trades WHERE mode=? AND account=? AND side='sell'", (mode,a['id'])))
                accounts.append(dict(a, positions=positions, value=value, nav=nav, pnl=money(nav-INITIAL), return_pct=(nav/INITIAL-1)*100,
                                     exposure=value/nav*100 if nav>0 else 0, trades=rows, orders=orders,
                                     unrealized=money(sum(p['unrealized'] for p in positions)),
                                     fees=money(db.execute('SELECT COALESCE(SUM(fees),0) FROM trades WHERE mode=? AND account=?', (mode,a['id'])).fetchone()[0]),
                                     sell_win_rate=sum(t['realized']>0 for t in closed)/len(closed)*100 if closed else None,
                                     equity=[dict(e) for e in db.execute('SELECT at,nav FROM equity WHERE mode=? AND account=? ORDER BY at DESC LIMIT 500', (mode,a['id']))][::-1]))
            return dict(pool_diagnostics=[dict(r) for r in db.execute('SELECT * FROM pool_diagnostics WHERE mode=? ORDER BY rowid DESC LIMIT 40',(mode,))], events=[dict(r) for r in db.execute('SELECT at,text FROM paper_events WHERE mode=? ORDER BY at DESC LIMIT 50',(mode,))], run=dict(run) if run else None, accounts=accounts, candidates=candidates, version=VERSION, as_of=now)
