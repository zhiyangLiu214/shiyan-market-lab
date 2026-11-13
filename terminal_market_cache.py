"""Single-flight refresh with durable, explicitly stale display snapshots."""
import copy
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from terminal_engine import TZ


class MarketCache:
    def __init__(self, loader, directory=None, now=time.monotonic):
        self.loader,self.directory,self.now=loader,Path(directory) if directory else None,now
        self.lock=threading.Lock();self.states={}

    def ttl(self,demo):
        d=datetime.now(TZ);t=d.strftime('%H:%M')
        return 25 if demo or (d.weekday()<5 and '09:15'<=t<='15:05') else 600

    def _state(self,demo):
        if demo not in self.states:
            value=None
            if not demo and self.directory:
                try:
                    data=json.loads((self.directory/'market-live.json').read_text())
                    if isinstance(data,dict) and isinstance(data.get('groups'),list) and data.get('updated_at'):value=data
                except (OSError,ValueError):pass
            self.states[demo]=dict(value=value,stamp=None,attempt=None,running=None,error=None)
        return self.states[demo]

    def _refresh(self,demo):
        try:
            result=self.loader(demo)
            if not isinstance(result,dict) or not isinstance(result.get('groups'),list):raise ValueError('主线数据结构不完整')
            result={k:v for k,v in result.items() if k not in ('stale','loading','refreshing','last_error')}
            if not demo and self.directory and result.get('updated_at'):
                try:
                    self.directory.mkdir(parents=True,exist_ok=True)
                    path=self.directory/'market-live.json';temp=path.with_suffix('.json.tmp')
                    temp.write_text(json.dumps(result,ensure_ascii=False,allow_nan=False));temp.replace(path)
                except (OSError,ValueError):logging.getLogger(__name__).exception('Unable to persist market display snapshot')
            with self.lock:self._state(demo).update(value=result,stamp=self.now(),error=None)
        except Exception as e:
            logging.getLogger(__name__).warning('Market refresh failed: %s: %s',type(e).__name__,e)
            with self.lock:
                state=self._state(demo)
                state['error']=(str(e)[:200]+'；保留上次成功快照' if str(e).startswith(('行业资金不可用','候选成分股均不完整')) else '行情源暂时更新失败，保留上次成功快照' if state['value'] is not None else '行情源暂时不可用，等待自动重试')
        finally:
            with self.lock:self._state(demo)['running']=None

    def get(self,demo=False):
        with self.lock:
            state=self._state(demo);now=self.now()
            fresh=state['stamp'] is not None and now-state['stamp']<self.ttl(demo)
            if not fresh and not state['running'] and (state['attempt'] is None or now-state['attempt']>=15):
                state['attempt']=now
                thread=threading.Thread(target=self._refresh,args=(demo,),daemon=True,name='market-refresh')
                state['running']=thread;thread.start()
            thread=state['running']
        # Fast local/demo loaders can finish immediately; network work never holds the HTTP request.
        if thread:thread.join(20 if demo else .05)
        with self.lock:
            state=self._state(demo);value=state['value']
            if value is None:return dict(loading=True,groups=[],errors=[],last_error=state['error'],refreshing=bool(state['running']))
            result=copy.deepcopy(value)
            if result.get('version','').startswith('mainline-funds-'):
                from terminal_mainline import board_hierarchy
                result['groups']=board_hierarchy(result['groups'])
            if state['stamp'] is None or self.now()-state['stamp']>=self.ttl(demo):
                result.update(stale=True,refreshing=bool(state['running']),last_error=state['error'] or '正在后台获取新的主线快照')
            return result
