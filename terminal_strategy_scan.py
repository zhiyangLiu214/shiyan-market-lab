"""Full selected-market daily-formula scan, separate from held-position monitoring."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime

from terminal_formula import evaluate_formula,FormulaError
from terminal_paper import clock
from terminal_universe import BOARDS
from terminal_strategy_rank import strategy_rank


class StrategyMarketScan:
    def __init__(self,universe,reader):
        self.universe,self.reader=universe,reader;self.lock=threading.RLock();self.jobs={};self.active=None

    def snapshot(self,sid):
        with self.lock:
            job=self.jobs.get(sid)
            return {k:v for k,v in job.items() if k not in ('thread','finished_mono')} if job else None

    def invalidate(self, sid):
        with self.lock:
            self.jobs.pop(sid, None)

    def request(self,item,scheduled_for=None):
        sid=item['id'];now=clock();cfg=item['config']
        with self.lock:
            old=self.jobs.get(sid)
            same_run = old and (scheduled_for is None or old.get('scheduled_for') == scheduled_for)
            if same_run and old['status'] == 'running':
                return self._pool(sid)
            if same_run and old.get('day') == now[:10]:
                age = time.monotonic() - old.get('finished_mono', 0)
                if scheduled_for and old['status'] == 'complete' and not old['failed']:
                    return self._pool(sid)
                if age < (300 if old['status'] == 'complete' else 120):
                    return self._pool(sid)
            if self.active is not None:return dict(ready=False,items=[],reason='正在扫描另一套市场策略，稍后重试',market_scan=self.snapshot(sid))
            job=dict(status='running',scheduled_for=scheduled_for,day=now[:10],started=now,checked=now,total=0,done=0,failed=0,excluded=0,matched=0,rows=[],source='上交所股票名录 + 深交所A股名录',reason='正在读取完整市场名录',boards=cfg['boards'])
            self.jobs[sid]=job;self.active=sid
            thread=threading.Thread(target=self._run,args=(item,job),daemon=True,name='strategy-market-scan');job['thread']=thread;thread.start()
            return self._pool(sid)

    def _pool(self,sid):
        job=self.snapshot(sid)
        if not job:return dict(ready=False,items=[],reason='市场扫描尚未开始')
        ready=job['status']=='complete' and job['failed']==0 and job['day']==clock()[:10]
        # All matches remain visible; only the top 50 are requoted for execution.
        matches=sorted([r for r in job['rows'] if r.get('matched') and r.get('data_ok')],key=lambda r:(-r['rank'],r['code']))
        return dict(ready=ready,items=matches[:50] if ready else [],reason=job['reason'],market_scan=job)

    def _run(self,item,job):
        sid=item['id'];cfg=item['config'];day=clock()[:10]
        try:
            items=self.universe.load(cfg['boards'])
            with self.lock:job.update(total=len(items),reason='逐只检查日线公式；扫描期间继续独立监控已有持仓')
            def check(meta):
                if self.jobs.get(sid) is not job:
                    return None
                row=dict(meta,matched=False,rank=None,data_ok=False)
                try:
                    quote=self.reader(meta['code'],item['mode'],meta)
                    bars=quote['bars'];row.update(source=quote.get('source'),as_of=quote.get('as_of'),adjustment=quote.get('adjustment'),bar_count=len(bars))
                    if not bars or bars[-1]['date']!=day:raise ValueError('缺少扫描当日的日线快照')
                    row['matched']=bool(evaluate_formula(cfg['formula'],bars,code=meta['code'],name=meta['name']))
                    if row['matched']:row.update(strategy_rank(cfg,bars,meta['code'],meta['name']))
                    row.update(data_ok=True,reason='公式入选' if row['matched'] else '公式不满足')
                except FormulaError as exc:
                    # Insufficient listing history is an explicit exclusion,
                    # unavailable feeds/missing fields remain data failures.
                    row.update(reason=str(exc),excluded=('历史不足' in str(exc) or '日线不足' in str(exc)))
                except Exception:row['reason']='行情读取失败或日线日期不符合要求'
                return row
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures=[executor.submit(check,meta) for meta in items]
                for future in as_completed(futures):
                    row=future.result()
                    with self.lock:
                        if self.jobs.get(sid) is not job:return
                        job['rows']=job['rows']+[row];job['done']+=1;job['checked']=clock()
                        if row.get('excluded'):job['excluded']+=1
                        elif not row['data_ok']:job['failed']+=1
                        if row['matched'] and row['data_ok']:job['matched']+=1
                        job['reason']=f"市场扫描 {job['done']}/{job['total']} · 入选{job['matched']} · 历史不足排除{job['excluded']} · 数据失败{job['failed']}"
            with self.lock:
                job.update(status='complete',finished_mono=time.monotonic(),finished=clock())
                if clock()[:10]!=day:job.update(status='error',reason='扫描跨交易日，结果不能用于买入')
                elif job['failed']:job['reason']+='；结果不完整，不据此自动买入'
        except Exception:
            with self.lock:job.update(status='error',reason='市场名录获取失败；不能确认完整选股范围',finished_mono=time.monotonic(),finished=clock())
        finally:
            with self.lock:self.active=None
