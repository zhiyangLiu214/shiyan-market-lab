"""Full selected-market daily-formula scan, separate from held-position monitoring."""
import threading
import time
from concurrent.futures import FIRST_COMPLETED,ThreadPoolExecutor,wait
from datetime import datetime

from terminal_formula import evaluate_formula,FormulaError
from terminal_paper import clock
from terminal_universe import BOARDS,in_boards
from terminal_strategy_rank import strategy_rank
from terminal_entry_filter import entry_eligible
from terminal_quotes import InsufficientDailyError,NoCurrentDailyError


class StrategyMarketScan:
    def __init__(self,universe,reader,entry_reader=None,cohort_reader=None):
        self.universe,self.reader=universe,reader;self.lock=threading.RLock();self.jobs={};self.active={}
        self.entry_reader=entry_reader
        self.cohort_reader=cohort_reader

    def snapshot(self,sid):
        with self.lock:
            job=self.jobs.get(sid)
            return {k:v for k,v in job.items() if k not in ('thread','finished_mono')} if job else None

    def invalidate(self, sid):
        with self.lock:
            self.jobs.pop(sid, None)

    def request(self,item,scheduled_for=None):
        sid=item['id'];now=clock();cfg=item['config']
        cohort=[item]
        if scheduled_for and self.cohort_reader:
            cohort=[s for s in self.cohort_reader() if s['enabled'] and s['mode']==item['mode']
                    and s['config']['pool']=='market' and s['config']['buy_time']==cfg['buy_time']]
            cohort=list({s['id']:s for s in [*cohort,item]}.values())
        with self.lock:
            old=self.jobs.get(sid)
            same_run = old and (scheduled_for is None or old.get('scheduled_for') == scheduled_for)
            if same_run and old['status'] == 'running':
                return self._pool(sid)
            if same_run and old.get('day') == now[:10]:
                age = time.monotonic() - old.get('finished_mono', 0)
                if scheduled_for and old['status'] == 'complete' and not old['failed']:
                    return self._pool(sid)
                if age < (300 if old['status'] == 'complete' and not old['failed'] else 60):
                    return self._pool(sid)
            if self.active and (not scheduled_for or any(not key.startswith('preview:') for key in self.active)):
                return dict(ready=False,items=[],reason='市场扫描正在运行，稍后重试',market_scan=self.snapshot(sid))
            run_key=scheduled_for or 'preview:'+sid
            jobs={}
            for strategy in cohort:
                config=strategy['config'];strategy_id=strategy['id']
                job=dict(status='running',scheduled_for=scheduled_for,day=now[:10],started=now,checked=now,total=0,done=0,failed=0,excluded=0,matched=0,rows=[],source='上交所股票名录 + 深交所A股名录',reason='正在读取完整市场名录',boards=config['boards'])
                jobs[strategy_id]=job;self.jobs[strategy_id]=job
            self.active[run_key]=jobs
            thread=threading.Thread(target=self._run,args=(cohort,jobs,run_key),daemon=True,name='strategy-market-scan')
            for job in jobs.values():job['thread']=thread
            thread.start()
            return self._pool(sid)

    def _pool(self,sid):
        job=self.snapshot(sid)
        if not job:return dict(ready=False,items=[],reason='市场扫描尚未开始')
        ready=job['status']=='complete' and job['failed']==0 and job['day']==clock()[:10]
        # All matches remain visible; only the top 50 are requoted for execution.
        matches=sorted([r for r in job['rows'] if r.get('matched') and r.get('data_ok') and entry_eligible(r)],key=lambda r:(-r['rank'],r['code']))
        return dict(ready=ready,items=matches[:50] if ready else [],reason=job['reason'],market_scan=job)

    def _run(self,cohort,jobs,run_key):
        day=clock()[:10]
        try:
            boards=list({board for item in cohort for board in item['config']['boards']})
            try:items=self.universe.load(boards)
            except Exception:
                time.sleep(1)
                items=self.universe.load(boards)
            with self.lock:
                for item in cohort:
                    job=jobs[item['id']]
                    job.update(total=sum(in_boards(meta['code'],item['config']['boards']) for meta in items),reason='逐只检查日线公式；扫描期间继续独立监控已有持仓')
            def check(meta):
                relevant=[item for item in cohort if in_boards(meta['code'],item['config']['boards'])
                          and self.jobs.get(item['id']) is jobs[item['id']]]
                if not relevant:return {}
                try:quote=self.reader(meta['code'],relevant[0]['mode'],meta);error=None
                except Exception as exc:quote=None;error=exc
                rows={}
                for item in relevant:
                    sid=item['id'];cfg=item['config']
                    if self.jobs.get(sid) is not jobs[sid]:continue
                    row=dict(meta,matched=False,rank=None,data_ok=False)
                    try:
                        if error:raise error
                        bars=quote['bars'];row.update(source=quote.get('source'),as_of=quote.get('as_of'),adjustment=quote.get('adjustment'),bar_count=len(bars))
                        if not bars or bars[-1]['date']!=day:raise ValueError('缺少扫描当日的日线快照')
                        row['matched']=bool(evaluate_formula(cfg['formula'],bars,code=meta['code'],name=meta['name']))
                        if row['matched']:row.update(strategy_rank(cfg,bars,meta['code'],meta['name'],self.entry_reader))
                        row.update(data_ok=True,reason=row.get('entry_reason') or ('公式入选' if row['matched'] else '公式不满足'))
                        if row.get('entry_status')=='missing':row['data_ok']=False
                    except FormulaError as exc:
                        row.update(reason=str(exc),excluded=('历史不足' in str(exc) or '日线不足' in str(exc)))
                    except InsufficientDailyError as exc:row.update(reason=str(exc),excluded=True)
                    except NoCurrentDailyError as exc:row.update(reason=str(exc),pending_no_trade=True)
                    except (RuntimeError,ValueError) as exc:row['reason']=str(exc)
                    except Exception:row['reason']='行情读取失败或日线日期不符合要求'
                    rows[sid]=row
                return rows
            workers=12
            with ThreadPoolExecutor(max_workers=workers) as executor:
                pending=set()
                for meta in items[:workers*2]:pending.add(executor.submit(check,meta))
                remaining=iter(items[workers*2:])
                while pending:
                    completed,pending=wait(pending,return_when=FIRST_COMPLETED)
                    for future in completed:
                        rows=future.result()
                        try:pending.add(executor.submit(check,next(remaining)))
                        except StopIteration:pass
                        for sid,row in rows.items():
                            job=jobs[sid]
                            with self.lock:
                                if self.jobs.get(sid) is not job:continue
                                job['rows'].append(row);job['done']+=1;job['checked']=clock()
                                if row.get('excluded'):job['excluded']+=1
                                elif not row['data_ok']:job['failed']+=1
                                if row['matched'] and row['data_ok']:job['matched']+=1
                                job['reason']=f"市场扫描 {job['done']}/{job['total']} · 入选{job['matched']} · 历史不足排除{job['excluded']} · 数据失败{job['failed']}"
            with self.lock:
                for sid,job in jobs.items():
                    if self.jobs.get(sid) is not job:continue
                    current=sum(bool(row['data_ok']) for row in job['rows'])
                    if job['total'] and current/job['total']>=0.95:
                        for row in job['rows']:
                            if row.pop('pending_no_trade',False):
                                row['excluded']=True
                                row['reason']='双源一致缺当日K线，按未交易排除；'+row['reason']
                                job['excluded']+=1;job['failed']-=1
                        job['reason']=f"市场扫描 {job['done']}/{job['total']} · 入选{job['matched']} · 未交易/历史不足排除{job['excluded']} · 数据失败{job['failed']}"
                    job.update(status='complete',finished_mono=time.monotonic(),finished=clock())
                    if clock()[:10]!=day:job.update(status='error',reason='扫描跨交易日，结果不能用于买入')
                    elif job['failed']:job['reason']+='；结果不完整，不据此自动买入'
        except Exception as exc:
            with self.lock:
                for sid,job in jobs.items():
                    if self.jobs.get(sid) is job:job.update(status='error',reason='市场名录或扫描任务失败：'+str(exc)[:180]+'；不能确认完整选股范围',finished_mono=time.monotonic(),finished=clock())
        finally:
            with self.lock:self.active.pop(run_key,None)
