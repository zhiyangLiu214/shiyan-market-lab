"""User formulas, scheduled entries and independent forward-only paper accounts."""
import json
import math
import re
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime,timedelta
from pathlib import Path

from terminal_formula import compile_formula,evaluate_formula,FormulaError
from terminal_paper import PaperLedger,clock,session,lot,money,number
from terminal_strategy_exit import exit_decision,PATTERNS
from terminal_universe import BOARDS,in_boards
from terminal_strategy_rank import strategy_rank
from terminal_holding import holding_deadline
from terminal_entry_filter import model_id,entry_eligible

EXAMPLE='MA5:=MA(C,5);\nMA20:=MA(C,20);\n选股:C>MA20 AND MA5>MA20 AND V>MA(V,5)*1.2;'


def checked_config(body):
    if not isinstance(body,dict):raise ValueError('策略格式无效')
    name=str(body.get('name','')).strip();description=str(body.get('description','')).strip()
    formula=body.get('formula','');ranking=body.get('ranking','C/REF(C,1)-1')
    if not 1<=len(name)<=60 or len(description)>4000:raise ValueError('请填写策略名称；说明最多4000字')
    compile_formula(formula)
    ranking_mode=body.get('ranking_mode','composite')
    entry_model=model_id(body.get('entry_model',''))
    if ranking_mode not in ('composite','formula'):raise ValueError('排序方式无效')
    if ranking_mode=='formula':compile_formula(ranking)
    else:ranking='C/REF(C,1)-1'
    pool=body.get('pool','custom')
    if pool not in ('custom','main','scan','market'):raise ValueError('股票池无效')
    boards=body.get('boards',list(BOARDS))
    if not isinstance(boards,list) or not boards or any(b not in BOARDS for b in boards):raise ValueError('请至少选择沪深主板、创业板或科创板中的一个市场')
    boards=list(dict.fromkeys(boards))
    codes=body.get('codes',[])
    if isinstance(codes,str):codes=[s for s in re.split(r'[\s,，;；]+',codes.strip()) if s]
    if not isinstance(codes,list) or any(not isinstance(c,str) or not re.fullmatch(r'[036]\d{5}',c) for c in codes):raise ValueError('股票池请输入沪深A股六位代码')
    codes=list(dict.fromkeys(codes))
    if pool=='custom' and any(not in_boards(c,boards) for c in codes):raise ValueError('自定义股票代码超出已选市场范围')
    if len(codes)>50 or pool=='custom' and not codes:raise ValueError('自定义股票池须有1至50只股票')
    buy_time=body.get('buy_time','14:30')
    if not isinstance(buy_time,str) or not re.fullmatch(r'\d{2}:\d{2}',buy_time):raise ValueError('买入时间格式应为HH:MM')
    try:datetime.strptime(buy_time,'%H:%M')
    except ValueError:raise ValueError('买入时间无效') from None
    if not ('09:31'<=buy_time<='11:27' or '13:01'<=buy_time<='14:54'):raise ValueError('买入时间须在09:31–11:27或13:01–14:54，预留检查与成交时间')
    if entry_model and (buy_time!='14:30' or boards!=['main']):raise ValueError('当前冻结量价模型仅适用于14:30选股及沪深主板范围')
    values={}
    for key,default,low,high in [('take_profit',10,.1,200),('stop_loss',5,.1,50),('position_pct',10,1,70),('max_exposure',70,10,100),('max_positions',5,1,5)]:
        value=body.get(key,default)
        if not number(value) or not low<=value<=high:raise ValueError(key+'超出允许范围')
        if key=='max_positions' and int(value)!=value:raise ValueError('持仓数量须为整数')
        values[key]=value
    if values['position_pct']>values['max_exposure']:raise ValueError('单股仓位不能超过总仓位')
    if type(body.get('holding_limit',True)) is not bool:raise ValueError('最长持仓开关须为布尔值')
    patterns=body.get('exit_patterns',['vwap_cross'])
    if not isinstance(patterns,list) or any(p not in PATTERNS for p in patterns):raise ValueError('辅助卖出条件无效')
    return dict(name=name,description=description,formula=formula,ranking=ranking,ranking_mode=ranking_mode,entry_model=entry_model,pool=pool,codes=codes,boards=boards,buy_time=buy_time,exit_patterns=list(dict.fromkeys(patterns)),
                **values,holding_limit=body.get('holding_limit',True),limit_up_trail=True,initial_cash=100000,scan_trigger_seconds=120)


class StrategyStore:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.books={}
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS strategies(id TEXT PRIMARY KEY,mode TEXT,config TEXT,enabled INTEGER,started TEXT,created TEXT,status TEXT,checked TEXT,result TEXT);
            CREATE TABLE IF NOT EXISTS daily_runs(strategy TEXT,day TEXT,status TEXT,reason TEXT,PRIMARY KEY(strategy,day));
            CREATE TABLE IF NOT EXISTS risk_lines(strategy TEXT,code TEXT,opened TEXT,stop REAL,target REAL,anchor TEXT,state TEXT DEFAULT '{}',PRIMARY KEY(strategy,code));
            CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY,strategy TEXT,mode TEXT,code TEXT,name TEXT,kind TEXT,day TEXT,at TEXT,text TEXT,UNIQUE(strategy,code,kind,day));''')
            if 'state' not in [r[1] for r in db.execute('PRAGMA table_info(risk_lines)')]:db.execute("ALTER TABLE risk_lines ADD COLUMN state TEXT DEFAULT '{}'")

    @contextmanager
    def db(self):
        with self.lock:
            db=sqlite3.connect(str(self.root/'strategies.sqlite3'),timeout=15);db.row_factory=sqlite3.Row
            try:yield db;db.commit()
            except Exception:db.rollback();raise
            finally:db.close()

    def get(self,sid):
        if not isinstance(sid,str) or not re.fullmatch('[a-f0-9]{32}',sid):raise ValueError('策略编号无效')
        with self.db() as db:row=db.execute('SELECT * FROM strategies WHERE id=?',(sid,)).fetchone()
        if not row or row['mode'].startswith('archived'):raise ValueError('策略不存在或已删除')
        item=dict(row);item['config']=json.loads(item['config']);item['config'].setdefault('boards',list(BOARDS));item['config'].setdefault('ranking_mode','formula');item['config'].setdefault('entry_model','');item['config'].pop('buy_window_seconds',None);item['config'].setdefault('scan_trigger_seconds',120);item['result']=json.loads(item['result'] or '{}');return item

    def list(self,mode):
        with self.db() as db:ids=[r[0] for r in db.execute('SELECT id FROM strategies WHERE mode=? ORDER BY created DESC,rowid DESC',(mode,))]
        return [self.get(sid) for sid in ids]

    def create(self,config,mode):
        config=checked_config(config);sid=uuid.uuid4().hex
        with self.db() as db:
            if db.execute("SELECT COUNT(*) FROM strategies WHERE mode IN ('demo','live')").fetchone()[0]>=50:raise ValueError('最多保留50套策略')
            db.execute('INSERT INTO strategies VALUES(?,?,?,0,NULL,?,?,NULL,?)',
                       (sid,mode,json.dumps(config,ensure_ascii=False),clock(),'已保存，尚未启动','{}'))
        return self.get(sid)

    def book(self,sid):
        self.get(sid)
        with self.lock:
            if sid not in self.books:self.books[sid]=PaperLedger(self.root/(sid+'.sqlite3'))
            return self.books[sid]

    def record(self,sid,status,result=None):
        with self.db() as db:
            if result is None:db.execute('UPDATE strategies SET status=? WHERE id=?',(status[:500],sid))
            else:db.execute('UPDATE strategies SET status=?,checked=?,result=? WHERE id=?',
                            (status[:500],clock(),json.dumps(result,ensure_ascii=False),sid))

    def daily(self,sid,day,status=None,reason=''):
        with self.db() as db:
            if status:db.execute('INSERT OR REPLACE INTO daily_runs VALUES(?,?,?,?)',(sid,day,status,reason[:500]))
            row=db.execute('SELECT * FROM daily_runs WHERE strategy=? AND day=?',(sid,day)).fetchone()
            return dict(row) if row else None

    def snapshot(self,sid):
        item=self.get(sid);item['ledger']=self.book(sid).snapshot(item['mode'])
        with self.db() as db:
            item['daily_runs']=[dict(r) for r in db.execute('SELECT * FROM daily_runs WHERE strategy=? ORDER BY day DESC LIMIT 20',(sid,))]
            item['risk_lines']=[dict(r) for r in db.execute('SELECT * FROM risk_lines WHERE strategy=?',(sid,))]
        return item

    def notices(self,mode):
        with self.db() as db:return [dict(r) for r in db.execute('SELECT * FROM notices WHERE mode=? ORDER BY at DESC,rowid DESC LIMIT 30',(mode,))]


def exit_lines(position,quote,config,previous=None):
    """Only confirmed past daily closes can lift next-day protection."""
    cost=position['average_cost'];opened=position['plan'].get('_opened_at','9999')
    cash_adjustment=position.get('cash_dividend_per_share',0)
    stop=cost*(1-config['stop_loss']/100)-cash_adjustment;target=cost*(1+config['take_profit']/100)-cash_adjustment;anchor=None
    if previous and previous.get('opened')==opened:
        stop=max(stop,previous['stop']);target=max(target,previous['target']);anchor=previous['anchor']
    rows=sorted([b for b in quote['daily'].get('bars',[]) if b['date']<quote['date']],key=lambda b:b['date'])
    limit=.2 if position['code'].startswith(('30','688')) else .1
    for before,bar in zip(rows,rows[1:]):
        if bar['date']<opened[:10] or not number(before.get('close')) or not number(bar.get('close')):continue
        upper=money(before['close']*(1+limit))
        if upper>0 and abs(bar['close']-upper)<.005:
            stop=max(stop,upper);target=max(target,upper*(1+config['take_profit']/100));anchor=bar['date']
    return dict(opened=opened,stop=money(stop),target=money(target),anchor=anchor)


class StrategyEngine:
    def __init__(self,store,reader,pool_reader,market_scan=None,calendar=None,entry_reader=None):
        self.store,self.reader,self.pool_reader=store,reader,pool_reader
        self.market_scan=market_scan
        self.calendar=calendar
        self.entry_reader=entry_reader
        self.guards={};self.check_threads={};self.thread=None;self.wakeup=threading.Event()

    def ensure(self):
        with self.store.lock:
            if not self.thread or not self.thread.is_alive():
                self.thread=threading.Thread(target=self.loop,daemon=True,name='custom-strategy-simulation');self.thread.start()

    def loop(self):
        while True:
            def execute(item):
                try:self.tick(item['id'])
                except Exception:self.store.record(item['id'],'本轮行情或执行检查失败；未补造交易')
            with ThreadPoolExecutor(max_workers=3) as executor:list(executor.map(execute,[s for s in self.store.list('live') if s['enabled']]))
            self.wakeup.wait(30);self.wakeup.clear()

    @contextmanager
    def execution_lock(self,sid):
        with self.store.lock:guard=self.guards.setdefault(sid,threading.Lock())
        with guard:yield

    def update_config(self, sid, body):
        config = checked_config(body)
        with self.execution_lock(sid), self.store.lock:
            item = self.store.get(sid)
            if config != item['config']:
                with self.store.db() as db:
                    # Update the account cap and registry as one SQLite transaction.
                    db.execute('ATTACH DATABASE ? AS account_book', (str(self.store.book(sid).path),))
                    db.execute('UPDATE account_book.accounts SET cap=? WHERE mode=? AND id=?',
                               (config['max_exposure'], item['mode'], 'main'))
                    if not config['holding_limit']:
                        db.execute("UPDATE account_book.orders SET status='cancelled',result='最长持仓规则已关闭' WHERE mode=? AND side='sell' AND status='pending' AND source='auto' AND reason LIKE '最长持仓退出%'", (item['mode'],))
                        for lot_row in db.execute('SELECT rowid,plan FROM account_book.lots WHERE mode=? AND qty>0', (item['mode'],)).fetchall():
                            plan=json.loads(lot_row['plan'])
                            plan.pop('_holding_exit_reason',None)
                            db.execute('UPDATE account_book.lots SET plan=? WHERE rowid=?',(json.dumps(plan),lot_row['rowid']))
                    db.execute("UPDATE strategies SET config=?,result='{}',checked=NULL,status=? WHERE id=?",
                               (json.dumps(config, ensure_ascii=False), '规则已更新，等待下一次检查', sid))
                if self.market_scan:
                    self.market_scan.invalidate(sid)
            return self.store.get(sid)

    def archive(self, sid):
        with self.execution_lock(sid), self.store.lock:
            item = self.store.get(sid)
            if item['enabled']:
                self.toggle(sid, False)
            with self.store.db() as db:
                db.execute("UPDATE strategies SET mode=?,enabled=0,status=? WHERE id=?",
                           ('archived-' + item['mode'], '已删除并归档', sid))
            if self.market_scan:
                self.market_scan.invalidate(sid)
            return dict(id=sid, archived=True)

    def request_check(self,sid):
        self.store.get(sid)
        def work():
            try:self.tick(sid,preview=True)
            except Exception:self.store.record(sid,'公式检查失败；请核对公式和行情，未产生委托')
        with self.store.lock:
            current=self.check_threads.get(sid)
            if current and current.is_alive():raise ValueError('这套策略正在检查，请等待结果刷新')
            self.store.record(sid,'正在后台检查公式与行情；本次不会下单')
            self.check_threads[sid]=threading.Thread(target=work,daemon=True,name='strategy-formula-check');self.check_threads[sid].start()
        return self.store.snapshot(sid)

    def toggle(self,sid,enabled):
        if not isinstance(enabled,bool):raise ValueError('启停状态无效')
        with self.store.lock:
            item=self.store.get(sid)
            if enabled and item['mode']!='live':raise ValueError('演示样本仅用于公式检查，不能启动定时交易')
            with self.store.db() as db:
                if enabled and db.execute('SELECT COUNT(*) FROM strategies WHERE enabled=1 AND id!=?',(sid,)).fetchone()[0]>=3:raise ValueError('最多同时运行3套策略')
            book=self.store.book(sid);mode=item['mode']
            if enabled:
                book.start(mode,clock(),accounts=('main',));book.set_cap(mode,'main',item['config']['max_exposure']);book.toggle(mode,True)
            else:
                for account in book.snapshot(mode)['accounts']:
                    for order in account['orders']:
                        if order['status']=='pending':book.cancel(mode,order['id'],'策略暂停，撤销未成交单')
                book.toggle(mode,False)
            with self.store.db() as db:
                db.execute('UPDATE strategies SET enabled=?,started=COALESCE(started,?),status=? WHERE id=?',
                           (int(enabled),clock() if enabled else None,'已启动，等待指定时间及有效行情' if enabled else '已暂停；持仓保留，退出监控也暂停',sid))

    def _submit(self,sid,book,*args):
        with self.store.lock:
            if not self.store.get(sid)['enabled']:raise ValueError('策略已暂停，不再提交委托')
            return book.submit(*args)

    def _pool(self,item,scheduled_for=None):
        cfg=item['config']
        if cfg['pool']=='custom':return dict(ready=True,items=[dict(code=c,name=c) for c in cfg['codes'] if in_boards(c,cfg.get('boards',list(BOARDS)))],reason='自定义股票池')
        if cfg['pool']=='market':
            if item['mode']=='demo':return dict(ready=False,items=[],reason='市场全范围扫描仅支持真实行情，演示样本不冒充全市场')
            if not self.market_scan:return dict(ready=False,items=[],reason='市场扫描服务尚未配置')
            return self.market_scan.request(item,scheduled_for=scheduled_for)
        pool=self.pool_reader(cfg['pool'],item['mode']=='demo')
        return dict(pool,items=[r for r in pool.get('items',[]) if in_boards(r['code'],cfg.get('boards',list(BOARDS)))])

    def _assessment(self,meta,q,item):
        row=dict(code=meta['code'],name=meta.get('name',meta['code']),matched=False,rank=None,data_ok=False)
        try:
            if not q or (not q.get('valid') and item['mode']!='demo'):raise FormulaError('缺少同日有效分钟或日线')
            if q['daily'].get('formula_error'):raise FormulaError(q['daily']['formula_error'])
            bars=q['daily'].get('formula_bars',q['daily'].get('bars',[]))
            if not bars or item['mode']!='demo' and bars[-1]['date']!=q['date']:raise FormulaError('缺少当日日线快照，不能用昨日数据代替')
            row['name']=q['daily'].get('name') or row['name'];row['source']=q['daily'].get('formula_source',q['daily'].get('source'));row['as_of']=q['as_of']
            row['adjustment']=q['daily'].get('formula_adjustment');row['bar_count']=len(bars)
            row['matched']=bool(evaluate_formula(item['config']['formula'],bars,code=meta['code'],name=row['name']))
            if row['matched']:row.update(strategy_rank(item['config'],bars,meta['code'],row['name'],self.entry_reader))
            row['data_ok']=True;row['reason']=row.get('entry_reason') or ('公式入选' if row['matched'] else '公式不满足')
            if row.get('entry_status')=='missing':row['data_ok']=False
        except (ValueError,TypeError,KeyError,ArithmeticError) as exc:row['reason']=str(exc)[:200]
        return row

    def tick(self,sid,preview=False):
        with self.execution_lock(sid):
            item=self.store.get(sid);cfg=item['config'];mode=item['mode'];now=clock();book=self.store.book(sid)
            snap=book.snapshot(mode,now);account=snap['accounts'][0] if snap['accounts'] else None
            target=now[:10]+' '+cfg['buy_time']+':00'
            seconds=(datetime.strptime(now,'%Y-%m-%d %H:%M:%S')-datetime.strptime(target,'%Y-%m-%d %H:%M:%S')).total_seconds()
            previous=self.store.daily(sid,now[:10]);finished=bool(previous and previous['status'] in ('done','missed'))
            # Persist the trigger before requesting the shared scanner. A queued
            # strategy or a long scan can then continue across scheduler ticks.
            admitted = bool(previous and previous['status'] == 'scanning')
            auto = not preview and item['enabled'] and account is not None
            if auto and not finished and not admitted and session(now) and 0 <= seconds < cfg['scan_trigger_seconds']:
                self.store.daily(sid,now[:10],'scanning','已到选股时间，等待完成本轮选股')
                admitted = True
            buying = auto and admitted and not finished and seconds >= 0 and session(now)
            pending_buys = account and any(o['status']=='pending' and o['side']=='buy' for o in account['orders'])
            needs_pool = preview or buying or pending_buys
            scheduled_for = target if not preview and (buying or pending_buys) else None
            pool=self._pool(item,scheduled_for) if needs_pool else dict(ready=False,items=[],reason='等待每日选股时间')
            items=pool.get('items',[])[:50]
            metas={s['code']:s for s in items}
            if account:
                for p in account['positions']:metas.setdefault(p['code'],p)
                for o in account['orders']:
                    if o['status']=='pending':metas.setdefault(o['code'],o)
            def read(meta):
                try:return meta['code'],self.reader(meta['code'],mode,meta)
                except Exception:return meta['code'],None
            with ThreadPoolExecutor(max_workers=4) as executor:quotes=dict(executor.map(read,metas.values()))
            assessments=[self._assessment(meta,quotes.get(meta['code']),item) for meta in items]
            assessments.sort(key=lambda r:(not(r['data_ok'] and r['matched']),-(r['rank'] if r['rank'] is not None else 0),r['code']))
            valid=pool.get('ready',False) and all(r['data_ok'] for r in assessments)
            reasons=[];submitted=[];holding_checks=[]
            if not preview and item['enabled'] and account:
                # Pending buys are rechecked against their formula and pool.
                checks={};pool_codes={m['code'] for m in items}
                for order in account['orders']:
                    if order['status']!='pending' or order['side']!='buy':continue
                    q=quotes.get(order['code']);check=self._assessment(dict(code=order['code'],name=order['name']),q,item)
                    checks[order['id']]=bool(pool.get('ready') and order['code'] in pool_codes and check['data_ok'] and check['matched'] and entry_eligible(check))
                    if check['data_ok'] and not check['matched']:book.cancel(mode,order['id'],'成交前公式已不满足')
                    elif check['data_ok'] and not entry_eligible(check):book.cancel(mode,order['id'],'成交前未通过量价买入门槛：'+check.get('entry_reason','数据待确认'))
                    if needs_pool and pool.get('ready') and order['code'] not in pool_codes:book.cancel(mode,order['id'],'完整股票池已移出该标的')
                with self.store.lock:
                    if self.store.get(sid)['enabled']:book.update(mode,{c:q for c,q in quotes.items() if q},clock(),buy_checks=checks)
                account=book.snapshot(mode,clock())['accounts'][0]
                pending={o['code'] for o in account['orders'] if o['status']=='pending'}
                calendar=self.calendar(now[:10]) if self.calendar and account['positions'] and cfg.get('holding_limit',True) else []
                for position in account['positions']:
                    q=quotes.get(position['code'])
                    if not q or not q['valid']:
                        reasons.append(position['name']+'持仓行情未确认，不能判断退出')
                        holding_checks.append(dict(code=position['code'],name=position['name'],reason='行情未确认，保留原保护线，等待有效数据'))
                        continue
                    with self.store.db() as db:
                        old=db.execute('SELECT * FROM risk_lines WHERE strategy=? AND code=?',(sid,position['code'])).fetchone()
                        lines=exit_lines(position,q,cfg,dict(old) if old else None)
                        prior=json.loads(old['state'] or '{}') if old and old['opened']==lines['opened'] else {}
                        decision=exit_decision(position,q,cfg,lines,prior);lines=decision['lines']
                        db.execute('INSERT OR REPLACE INTO risk_lines VALUES(?,?,?,?,?,?,?)',(sid,position['code'],lines['opened'],lines['stop'],lines['target'],lines['anchor'],json.dumps(decision['state'])))
                        for notice in decision['notices']:
                            text=notice['text']+'；行情截至'+q['as_of']
                            if decision['sell'] and not position['available']:text+='；T+1暂不可卖，已记录待卖状态，尚未成交'
                            db.execute('INSERT OR IGNORE INTO notices VALUES(?,?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,sid,mode,position['code'],position['name'],notice['kind'],q['date'],clock(),text))
                    holding_checks.append(dict(code=position['code'],name=position['name'],reason=decision['reason'],speed=decision['metrics']['speed'],stop=lines['stop'],target=lines['target'],available=position['available']))
                    deadline=holding_deadline(position,dict(q,sessions=calendar or q.get('sessions',[])),cfg.get('holding_limit',True))
                    if deadline['sell']:book.mark_holding_exit(mode,'main',position['code'],deadline['reason'])
                    holding_checks[-1].update(holding_days=deadline['days'],holding_reason=deadline['reason'])
                    why=decision['reason'] if decision['sell'] else deadline['reason'] if deadline['sell'] else None
                    if why and position['code'] not in pending and session(clock()):
                        if not position['available']:reasons.append(position['name']+'触发退出但受T+1限制');continue
                        try:
                            oid=self._submit(sid,book,mode,'main',position['code'],position['name'],'sell',position['available'],q['price'],q['as_of'],dict(position['plan'],**lines),why,'auto',clock());submitted.append(oid);pending.add(position['code'])
                        except ValueError as exc:reasons.append(str(exc))
                current = clock()
                can_submit = current[:10] == now[:10] and current >= target and session(current)
                if buying and can_submit and valid:
                    ordered=sorted([r for r in assessments if r['matched'] and entry_eligible(r)],key=lambda r:(-r['rank'],r['code']))
                    positions={p['code']:p for p in account['positions']};reserved=set(positions)|pending
                    for row in ordered:
                        code=row['code']
                        if code in reserved or len(reserved)>=cfg['max_positions']:continue
                        q=quotes[code]
                        if not row['name'] or row['name']==code:row['reason']='名称未核验，暂不买入';continue
                        if any(t['code']==code and t['side']=='sell' and t['at'][:10]==now[:10] for t in account['trades']):row['reason']='当日已卖出，不重新买入';continue
                        minimum,step=lot(code);budget=account['nav']*cfg['position_pct']/100
                        qty=math.floor((budget-10)/(q['price']*1.0005)/step)*step
                        if qty<minimum:row['reason']='资金分配不足最小买入数量';continue
                        plan=dict(date=q['date'],stop=q['price']*(1-cfg['stop_loss']/100),target1=q['price']*(1+cfg['take_profit']/100))
                        try:
                            submitted_at = clock()
                            if submitted_at[:10] != now[:10] or not session(submitted_at):break
                            why=('量价门槛E='+str(round(row['rank'],6))+'，模型'+row['entry_model'] if cfg.get('entry_model') else '综合评分 '+str(row['rank'])+' 分优先' if cfg.get('ranking_mode')=='composite' else '自定义排序值 '+str(row['rank']))
                            if cfg.get('entry_model'):plan['entry_evidence']={k:v for k,v in row.items() if k.startswith('entry_')}
                            oid=self._submit(sid,book,mode,'main',code,row['name'],'buy',qty,q['price'],q['as_of'],plan,'定时买入；自定义选股公式成立；'+why,'auto',submitted_at)
                            submitted.append(oid);reserved.add(code);row['reason']='已提交，等待后续新分钟成交'
                        except ValueError as exc:row['reason']=str(exc)
                    self.store.daily(sid,now[:10],'done','已完成本日定时检查，不重复买入')
                elif buying and not valid:
                    reasons.append(pool.get('reason') if not pool.get('ready') else '候选行情待复核')
                if not finished and seconds>=0 and datetime.strptime(now[:10],'%Y-%m-%d').weekday()<5:
                    if now[11:16]>='14:57' or (not admitted and seconds>=cfg['scan_trigger_seconds']):
                        self.store.daily(sid,now[:10],'missed','本日选股未触发或未在连续竞价结束前完成')
            status='公式检查完成，不产生委托' if preview else '运行中，等待每日'+cfg['buy_time']+'开始选股'
            if not preview and (finished or buying and can_submit and valid):
                status='本日定时选股已结束，继续监控持仓'
            if preview and cfg['pool']=='market':status=pool.get('reason','市场扫描后台检查中')+'；本次不会下单'
            if not preview and not session(clock()):status='非连续竞价时段，等待交易时段，不补买单'
            if not preview and reasons:status='；'.join(reasons[:3])
            result=dict(items=assessments,holdings=holding_checks,pool_ready=pool.get('ready'),pool_reason=pool.get('reason'),preview=preview,submitted=len(submitted),as_of=clock(),market_scan=pool.get('market_scan'),scope=('已选市场完整名录逐只检查；全部入选可查看，排序前50名再取实时分钟核验买入' if cfg['pool']=='market' else '按已选市场过滤；自定义池最多50只，主线/三维仅各自候选池'))
            self.store.record(sid,status,result)
            return self.store.snapshot(sid)
