"""Two portfolios use separate candidate pools and identical forward execution rules."""
from terminal_holding import holding_deadline

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from terminal_paper import PaperLedger, clock, session, lot, number, money, TZ


class PaperRunner:
    def __init__(self, ledger, market, scan, detail, minute, calendar=None):
        self.ledger, self.market, self.scan, self.detail, self.minute = ledger, market, scan, detail, minute
        self.calendar = calendar
        self.guard, self.thread = threading.Lock(), None
        self.wakeup = threading.Event()
        self.cache, self.last_pool = {}, {}

    def ensure(self):
        with self.guard:
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self.loop, daemon=True, name='paper-portfolios')
                self.thread.start()

    def loop(self):
        while True:
            for mode in ('live', 'demo'):
                try:
                    snap = self.ledger.snapshot(mode)
                    if snap['run']:
                        self.tick(mode, snap)
                except Exception:
                    self.ledger.status(mode, '本轮数据核对失败；保留账本，不补造交易')
            waiting = session(clock()) and any(not all(p[1].get('_ready', {}).values()) for p in self.last_pool.values())
            self.wakeup.wait(5 if waiting else 30)
            self.wakeup.clear()

    def read(self, code, mode, meta=None):
        demo = mode == 'demo'; meta = meta or {}
        minute = self.minute(code, demo)
        k = (code, mode)
        cached = self.cache.get(k)
        if not cached or time.time()-cached[0] > 60:
            daily = self.detail(code, demo, meta.get('name', ''), meta.get('board', ''))
            self.cache[k] = (time.time(), daily)
        daily = self.cache[k][1]
        points = minute.get('points', [])
        as_of = minute.get('as_of', '')
        now = clock()
        age = (datetime.strptime(now, '%Y-%m-%d %H:%M:%S')-datetime.strptime(as_of, '%Y-%m-%d %H:%M')).total_seconds() if as_of else math.inf
        same_day = minute.get('date') == now[:10]
        after_hours = now[11:16] >= '15:00' and as_of[11:16] >= '14:59'
        lunch = '11:30' <= now[11:16] < '13:01' and '11:29' <= as_of[11:16] <= '11:30'
        valid = bool(len(daily.get('bars',[]))>=10 and points and same_day and 0 <= age and (age <= 180 or after_hours or lunch))
        bars = [b for b in daily.get('bars', []) if b['date'] < minute.get('date', '')]
        prev = bars[-1]['close'] if bars else None
        return dict(price=points[-1]['price'] if points else None, as_of=as_of, date=minute.get('date'), points=points,
                    valid=valid, prev_close=prev, minute=minute, daily=daily)

    def _pool(self, mode, cadence=60):
        cached=self.last_pool.get(mode)
        if cached and session(clock()) and not all(cached[1].get('_ready',{}).values()): cadence=min(cadence,5)
        if cached and time.time()-cached[0]<cadence: return cached[1]
        result={'main':[],'scan':[],'_ready':{'main':False,'scan':False},'_safe':{'main':False,'scan':False},'_at':{}}
        try:
            main=self.market(mode=='demo')
            if main.get('stale') or main.get('loading') or main.get('readonly'):raise ValueError('主线只读快照不可作为新交易依据')
            result['main']=[dict(s,board=g['name']) for g in main['groups'] if g.get('board_type') in ('main','secondary','trend') for s in g['leaders']][:20]
            result['_ready']['main']=True
            known_empty=main.get('errors')==['当前无正涨幅且主力净流入及净占比均为正的行业']
            full_coverage=all(g.get('score_coverage',100)==100 and g.get('limit_count',0) is not None for g in main['groups'])
            result['_safe']['main']=(not main.get('errors') and full_coverage) or known_empty
            result['_at']['main']=main.get('updated_at') or clock()
            self.ledger.capture(mode,'main',result['main'],result['_at']['main'])
            self.ledger.pool_status(mode,'main','ready', '主线与角色候选已更新' if result['main'] else '暂无已确认主线/资金趋势的可交易角色；潜在观察不自动开仓；'+('；'.join(g.get('board_label','')+'：'+g['name'] for g in main['groups'])) ,len(result['main']))
        except Exception:
            self.ledger.pool_status(mode,'main','unavailable', (main.get('last_error') or '；'.join(main.get('errors',[])) or '主线数据正在加载或已陈旧') if 'main' in locals() else '主线数据读取失败；暂停新开仓')
        try:
            scan=self.scan(mode=='demo')
            if scan is not None:
                rows=scan['items'] if isinstance(scan,dict) else scan
                result['scan']=[s for s in rows if s.get('score',0)>=85][:20]
                result['_ready']['scan']=True;result['_safe']['scan']=scan.get('complete',True) if isinstance(scan,dict) else True;result['_at']['scan']=scan['as_of'] if isinstance(scan,dict) else clock()
                self.ledger.capture(mode,'scan',result['scan'],result['_at']['scan'])
                self.ledger.pool_status(mode,'scan','ready' if result['_safe']['scan'] else 'partial',(scan.get('reason') if isinstance(scan,dict) else None) or ('本轮有达标候选' if rows else '本轮暂无达标候选'),len(result['scan']))
            else:
                self.ledger.pool_status(mode,'scan','loading','三维扫描未完成或上一轮已过期；等待新一轮，不以空池推定无机会')
        except Exception:
            self.ledger.pool_status(mode,'scan','unavailable','三维数据读取失败；暂停新开仓')
        self.last_pool[mode]=(time.time(),result)
        return result

    def entry(self,q,aid,plan=None):
        if not q or not q.get('valid'):
            return dict(known=False,good=False,rr=None,signal=None,data_error=True,
                        reasons=['数据故障：缺少同日有效分钟或足够日线，等待行情恢复'])
        d=q['daily'];f=d.get('funds',{});plan=plan or d['plan'];price=q['price'];last=q['points'][-1]
        signals=[x for x in q['minute'].get('signals',[]) if x['kind'] in ('弱转强','均价附近承接') and 0<=self._mins(last['time'])-self._mins(x['time'])<=2]
        valid_plan=plan.get('date') and plan['date']<=q['date'] and (datetime.strptime(q['date'],'%Y-%m-%d')-datetime.strptime(plan['date'],'%Y-%m-%d')).days<=7
        stop,target=plan.get('stop'),plan.get('target1')
        touched=any((number(stop) and p['price']<=stop) or (number(target) and p['price']>=target) for p in q['points'] if q['date']>plan.get('date','') or p['time']>plan.get('as_of',''))
        rr=(target-price)/(price-stop) if number(stop) and number(target) and stop<price<target and not touched else None
        known=f.get('status')=='ok' and f.get('date')==q['date'] and number(f.get('net')) is not None and number(f.get('ratio')) is not None
        good=bool(known and valid_plan and signals and number(last.get('average')) and price>=last['average'] and rr is not None and rr>=1.5 and f['net']>0 and f['ratio']>0 and (aid!='scan' or d.get('score',0)>=85))
        reasons=[]
        if f.get('status')!='ok': reasons.append('数据故障：个股资金'+('为旧快照' if f.get('status')=='stale' else '不可用')+'；'+f.get('reason','等待资金源恢复'))
        elif f.get('date')!=q['date']: reasons.append('数据故障：资金日期'+str(f.get('date') or '未知')+'与分时交易日'+q['date']+'不一致')
        elif not known: reasons.append('数据故障：缺少主力净额或净占比，不能确认正向资金')
        elif f['net']<=0 or f['ratio']<=0: reasons.append('主力净额或净占比未同时为正')
        if not valid_plan: reasons.append('压力位/止损计划已过期或日期无效')
        if touched: reasons.append('原计划止损或首压力已触及，等待重新评估')
        elif rr is None: reasons.append('缺少有效压力位/止损，无法计算剩余盈亏比')
        elif rr<1.5: reasons.append('剩余盈亏比'+format(rr,'.2f')+'低于1.50')
        if not signals: reasons.append('最近两分钟未触发弱转强或均价承接')
        if not number(last.get('average')): reasons.append('分时均价缺失，承接无法确认')
        elif price<last['average']: reasons.append('价格低于分时均价，承接未确认')
        if aid=='scan' and d.get('score',0)<85: reasons.append('三维复核低于85分')
        return dict(known=bool(known),good=good,rr=rr,signal=signals[-1] if signals else None,
                    data_error=not bool(known),reasons=reasons)

    @staticmethod
    def weak(q):
        if not q or not q.get('valid'): return False
        f=q['daily'].get('funds',{});tail=q['points'][-2:]
        return bool(len(tail)==2 and all(number(p.get('average')) and p['price']<p['average'] for p in tail) and f.get('status')=='ok' and f.get('date')==q['date'] and number(f.get('net')) and f['net']<0)

    def tick(self, mode, snap=None):
        now = clock(); snap = snap or self.ledger.snapshot(mode)
        if not snap['run']: return
        # Outside execution hours, value existing positions and expire yesterday's orders; never trade old candles.
        active = session(now)
        pools = self._pool(mode,60 if active else 600) if active or snap['run']['enabled'] else {'main': [], 'scan': []}
        metas = {s['code']: s for aid in ('main','scan') for s in pools[aid]} if active else {}
        for a in snap['accounts']:
            for p in a['positions']: metas.setdefault(p['code'], p)
            for o in a['orders']:
                if o['status'] == 'pending': metas.setdefault(o['code'], o)
        quotes = {}
        def load(item):
            code, meta = item
            try: return code, self.read(code, mode, meta)
            except Exception: return code, None
        with ThreadPoolExecutor(max_workers=4) as pool:
            for code, quote in pool.map(load, metas.items()):
                if quote: quotes[code] = quote
        # Revalidate every pending buy before fills; failures are not evidence of exclusion.
        buy_checks={}
        for a in snap['accounts']:
            aid=a['id'];ready=pools.get('_ready',{}).get(aid,False)
            codes={s['code'] for s in pools[aid]}
            for o in a['orders']:
                if o['status']!='pending' or o['side']!='buy': continue
                if not active or (not snap['run']['enabled'] and o['source']=='auto'):
                    continue
                if not ready: continue
                if o['code'] not in codes:
                    if not pools.get('_safe',{}).get(aid,False): continue
                    self.ledger.cancel(mode,o['id'],'最新完整筛选池已移出该标的，撤销待买单')
                    continue
                check=self.entry(quotes.get(o['code']),aid,json.loads(o['plan']))
                if check['known'] and not check['good']:
                    self.ledger.cancel(mode,o['id'],'成交前复核：入场信号、资金或盈亏比已不再达标')
                else: buy_checks[o['id']]=check['good']
        self.ledger.update(mode, quotes, clock(),buy_checks=buy_checks)
        if not active or not snap['run']['enabled']:
            self.ledger.status(mode, '非连续竞价时段：等待下一交易时段，不补单' if not active else '自动策略暂停；已核对待成交单与持仓估值')
            return
        snap = self.ledger.snapshot(mode)
        calendar = self.calendar(now[:10]) if self.calendar and any(a['positions'] and a.get('holding_limit',1) for a in snap['accounts']) else []
        outcomes = []
        for account in snap['accounts']:
            aid = account['id']; positions = {p['code']:p for p in account['positions']}
            pending = {o['code'] for o in account['orders'] if o['status']=='pending'}
            ready=pools.get('_ready',{}).get(aid,False)
            pool_at=pools.get('_at',{}).get(aid,now)
            absence=self.ledger.pool_absence(mode,aid,positions,{s['code'] for s in pools[aid]},pool_at) if ready and pools.get('_safe',{}).get(aid,False) else {}
            blocked=len(positions)>=5 or account['exposure']+10>account['cap'] or account['cash']<account['nav']*.1+10
            replacements=[]
            for i,candidate in enumerate(pools[aid]):
                code=candidate['code'];q=quotes.get(code);check=self.entry(q,aid)
                if code not in positions and code not in pending and check['good'] and check['rr']>=2:
                    minimum,_=lot(code)
                    if minimum*q['price']*1.0005+10<=account['nav']*.1:
                        replacements.append((check['rr'], -i, candidate))
            replacement=max(replacements,key=lambda x:x[:2])[2] if replacements else None
            weak_holds=[p for p in positions.values() if p['code'] not in pending and absence.get(p['code'],{}).get('mature') and self.weak(quotes.get(p['code'])) and p['available']==p['qty']]
            rotate=min(weak_holds,key=lambda p:quotes[p['code']]['daily']['funds']['net'])['code'] if blocked and replacement and weak_holds else None
            # Manage holdings before considering any entries.
            for code, p in positions.items():
                q = quotes.get(code)
                if not q or not q['valid'] or code in pending: continue
                price = q['price']; plan = p['plan']; reason = None; fraction = 1.
                if number(plan.get('stop')) and price <= plan['stop']:
                    reason = '技术失效位触发，退出观察'
                elif number(plan.get('target2')) and price >= plan['target2']:
                    reason = '到达持仓时固定的第二压力，退出剩余持仓'
                elif number(plan.get('target1')) and price >= plan['target1'] and not p.get('profit_taken'):
                    reason, fraction = '到达持仓时固定的首压力，分批止盈', .5
                elif code==rotate:
                    reason=f"动态腾仓：连续3次完整快照（≥2分钟）离池、价格与资金同步转弱；候选{replacement['name']}剩余盈亏比≥2，先卖后重新评估买入"
                else:
                    tail = q['points'][-2:]; funds = q['daily'].get('funds', {})
                    if len(tail)==2 and all(number(x.get('average')) and x['price']<x['average'] for x in tail) and funds.get('date')==q['date'] and number(funds.get('net')) and funds['net']<0:
                        reason, fraction = '两根分钟弱于均价且同日主力流出，减仓', .5
                    elif account['exposure'] > account['cap']+.5:
                        reason, fraction = '市值上涨导致被动超出仓位上限，回收风险', min(1.,(account['value']-account['nav']*account['cap']/100)/max(p['value'],1))
                deadline = holding_deadline(p, dict(q,sessions=calendar or q.get('sessions',[])), bool(account.get('holding_limit', 1)))
                if deadline['sell']:
                    self.ledger.mark_holding_exit(mode,aid,code,deadline['reason'])
                    reason, fraction = deadline['reason'], 1.
                if reason:
                    available = p['available']; minimum, step = lot(code)
                    qty = available if fraction == 1 else max(minimum, math.floor(available*fraction/step)*step)
                    qty = min(qty, available)
                    if qty:
                        try: self.ledger.submit(mode,aid,code,p['name'],'sell',qty,price,q['as_of'],plan,reason,'auto',clock()); pending.add(code)
                        except ValueError: pass
                    else: outcomes.append(aid+': T+1锁定，风险退出等待可卖')
            diagnostics = []
            ranked=sorted(enumerate(pools[aid]),key=lambda pair:(not self.entry(quotes.get(pair[1]['code']),aid)['good'],pair[1]['code'] in positions,-(self.entry(quotes.get(pair[1]['code']),aid)['rr'] or 0),pair[0]))
            for _,s in ranked:
                code=s['code']; q=quotes.get(code); why='等待最新有效分钟与日线'
                check=self.entry(q,aid)
                why='；'.join(check['reasons']) or why
                if q and q['valid']:
                    d=q['daily']; plan=d['plan']; price=q['price']; f=d.get('funds',{})
                    check=self.entry(q,aid);good=check['good'];rr=check['rr'];signals=[check['signal']] if check['signal'] else []
                    why='；'.join(check['reasons']) or '满足执行条件'
                    recently_sold=any(t['code']==code and t['side']=='sell' and (datetime.strptime(now,'%Y-%m-%d %H:%M:%S')-datetime.strptime(t['at'],'%Y-%m-%d %H:%M:%S')).total_seconds()<1800 for t in account['trades'])
                    if code not in positions and recently_sold:
                        good=False;why='卖出后30分钟冷静期，避免短时反复换手'
                    if good and code not in pending:
                        p=positions.get(code)
                        if p and (price<=p['average_cost'] or p['value']>=account['nav']*.2 or q['date']+' '+signals[-1]['time'] <= (p.get('last_buy_observed') or '')):
                            why='持仓未浮盈、单股已达20%或同一分时信号已使用，不加仓'
                        else:
                            budget=min(account['nav']*(.05 if p else .1),account['nav']*.2-(p['value'] if p else 0)); minimum,step=lot(code)
                            qty=math.floor((budget/(price*1.0005)-minimum)/step)*step+minimum
                            why='最小申报数量超过本次10%/5%资金预算' if qty<minimum else '满足执行条件'
                            if qty>=minimum:
                                try:
                                    hold_plan=p['plan'] if p else dict(plan,support=d.get('support'))
                                    self.ledger.submit(mode,aid,code,d['name'],'buy',qty,price,q['as_of'],hold_plan,('浮盈后加仓' if p else '首次试仓')+f'；{signals[-1]["kind"]}，同日资金为正，剩余盈亏比{rr:.2f}', 'auto',clock());pending.add(code);why='已提交，等待后续新分钟模拟成交'
                                except ValueError as exc: why=('动态腾仓卖出尚未成交，名额与资金未释放；' if rotate else '')+str(exc)
                    elif code in pending: why='已有待成交委托'
                diagnostics.append(dict(code=code,name=s.get('name',code),board=s.get('board',''),role=s.get('role','三维≥85分'),score=s.get('score'),decision=why,data_error=check['data_error']))
            self.ledger.capture(mode,aid,diagnostics,pool_at)
        self.ledger.status(mode, '已核对两个账户；'+('；'.join(outcomes[:2]) if outcomes else '满足条件才下单，委托后等待新分钟'))

    @staticmethod
    def _mins(t):
        return int(t[:2])*60+int(t[3:5])
