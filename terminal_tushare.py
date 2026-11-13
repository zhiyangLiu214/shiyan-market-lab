"""Optional official Tushare transport. No credentials or paid access are created."""
import json
import hashlib
import math
import os
import re
import stat
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from terminal_engine import TZ, now_text, validate_code, normalize_bars

URL = 'https://api.tushare.pro'


class TushareError(RuntimeError):
    """Safe, locally generated message; never expose remote messages or tokens."""


def ts_code(code):
    validate_code(code)
    return code + ('.SH' if code.startswith('6') else '.BJ' if code[0] in '489' else '.SZ')


class TushareData:
    def __init__(self, token_path=None):
        self.token_path=Path(token_path) if token_path else None
        self.lock=threading.RLock();self.status_lock=threading.Lock();self.cache={};self.states={};self.until={};self.last_start=None;self.identity=None

    def _token(self):
        token=os.environ.get('TUSHARE_TOKEN','').strip()
        if not token and self.token_path and self.token_path.exists():
            if stat.S_IMODE(self.token_path.stat().st_mode)&0o077:
                raise TushareError('Tushare密钥文件权限应为600')
            token=self.token_path.read_text().strip()
        return token

    def configured(self):
        try:return bool(self._token())
        except (OSError,TushareError):return False

    def connection(self):
        with self.status_lock:
            configured=self.configured()
            return dict(provider='tushare',name='Tushare',configured=configured,
                        status='pending' if configured else 'unconfigured',
                        reason='已配置凭证；各项权限需分别验证' if configured else '尚未配置Token，未参与行情切换',
                        capabilities=['日线与盘中日线','当日完整分钟线','个股/板块盘后资金历史'],
                        checks=[dict(api=k,**v) for k,v in self.states.items()])

    def query(self, api_name, params, fields, ttl=25):
        # Requests are paced and single-flight, including shared all-sector history.
        key=json.dumps([api_name,params,fields],sort_keys=True)
        with self.lock:
            token=self._token()
            if not token:raise TushareError('Tushare未配置Token')
            identity=hashlib.sha256(token.encode()).digest()
            if identity!=self.identity:
                self.cache.clear();self.until.clear()
                with self.status_lock:self.states.clear()
                self.identity=identity
            cached=self.cache.get(key)
            if cached and time.monotonic()-cached[0]<ttl:return cached[1].copy()
            if self.until.get(api_name,0)>time.monotonic():raise TushareError('Tushare该项接口冷却中，稍后自动复检')
            if self.last_start is not None:
                delay=1.3-(time.monotonic()-self.last_start)
                if delay>0:time.sleep(delay)
            self.last_start=time.monotonic()
            try:
                response=requests.post(URL,json=dict(api_name=api_name,token=token,params=params,fields=fields),
                                       timeout=(4,10),allow_redirects=False)
                if response.status_code in (301,302,303,307,308):raise TushareError('Tushare接口重定向，未转发凭证')
                response.raise_for_status();payload=response.json()
                if payload.get('code')!=0:
                    code=payload.get('code')
                    raise TushareError('Tushare权限或凭证未通过（2002）' if code==2002 else 'Tushare请求未通过，需核验权限和调用额度')
                data=payload.get('data') or {};columns=data.get('fields');rows=data.get('items')
                if not isinstance(columns,list) or len(set(columns))!=len(columns) or not isinstance(rows,list) or not rows:
                    raise TushareError('Tushare返回数据为空或结构无效')
                if any(not isinstance(r,list) or len(r)!=len(columns) for r in rows):raise TushareError('Tushare数据列不完整')
                frame=pd.DataFrame(rows,columns=columns)
            except Exception as exc:
                reason=str(exc) if isinstance(exc,TushareError) else 'Tushare网络或响应异常，未获取有效数据'
                with self.status_lock:self.states[api_name]=dict(status='unavailable',at=now_text(),reason=reason)
                self.until[api_name]=time.monotonic()+120
                raise TushareError(reason) from None
            with self.status_lock:self.states[api_name]=dict(status='ok',at=now_text(),reason='响应成功；仍需按字段和源日期校验')
            if len(self.cache)>=256:self.cache.clear()
            self.cache[key]=(time.monotonic(),frame)
            return frame.copy()

    @staticmethod
    def _symbol_rows(frame, code):
        expected=ts_code(code)
        if 'ts_code' not in frame or set(frame.ts_code)!={expected}:raise TushareError('Tushare返回股票代码不匹配')
        return frame.copy()

    def prices(self, kind, code):
        expected=ts_code(code);now=datetime.now(TZ)
        if kind=='minute':
            data=self._symbol_rows(self.query('rt_min_daily',dict(ts_code=expected,freq='1MIN'),
                  'ts_code,time,open,high,low,close,vol,amount'),code)
            required=['time','open','high','low','close','vol','amount']
            if any(k not in data for k in required):raise TushareError('Tushare分钟数据缺少OHLCV或成交额')
            if not data.time.astype(str).str.match(r'^\d{4}-\d{2}-\d{2}[ T]').all():raise TushareError('Tushare分钟数据缺少明确的源日期')
            data=data.rename(columns={'time':'date','vol':'volume'})
            data['volume']=pd.to_numeric(data.volume,errors='raise')/100  # shares -> lots
            data['date']=pd.to_datetime(data.date,errors='raise')
            if data.date.dt.tz is not None:data['date']=data.date.dt.tz_convert(TZ).dt.tz_localize(None)
            checked=normalize_bars(data)
            if len(checked)!=len(data) or checked.amount.isna().any() or (checked.amount<0).any():raise TushareError('Tushare分钟OHLCV或成交额无效')
            data=checked
            data.attrs['source_note']='Tushare当日完整1分钟OHLC；成交量由股转为手，成交额为元；只使用已闭合分钟。'
            return data
        data=self._symbol_rows(self.query('daily',dict(ts_code=expected,
                 start_date=(now-timedelta(days=600)).strftime('%Y%m%d'),end_date=now.strftime('%Y%m%d')),
                 'ts_code,trade_date,open,high,low,close,vol,amount',ttl=600),code)
        if any(k not in data for k in ('trade_date','open','high','low','close','vol','amount')):
            raise TushareError('Tushare日线字段不完整')
        data=data.rename(columns={'trade_date':'date','vol':'volume'})
        data['date']=pd.to_datetime(data.date,format='%Y%m%d',errors='raise')
        data['amount']=pd.to_numeric(data.amount,errors='raise')*1000  # daily amount: thousand yuan
        if now.weekday()<5 and now.strftime('%H:%M')>='09:30':
            rt=self._symbol_rows(self.query('rt_k',dict(ts_code=expected),
                         'ts_code,trade_time,open,high,low,close,vol,amount'),code)
            if len(rt)!=1 or any(k not in rt for k in ('trade_time','open','high','low','close','vol','amount')):
                raise TushareError('Tushare盘中日线缺少源时间或OHLCV')
            row=rt.iloc[0];raw_time=str(row.trade_time)
            if not re.match(r'^\d{4}-\d{2}-\d{2}[ T]',raw_time) and not re.fullmatch(r'\d{14}',raw_time):
                raise TushareError('Tushare盘中日线缺少明确的源日期')
            stamp=pd.to_datetime(raw_time,errors='raise')
            if stamp.tzinfo:stamp=stamp.tz_convert(TZ).tz_localize(None)
            if stamp.date()!=now.date() or (now.replace(tzinfo=None)-stamp.to_pydatetime()).total_seconds() < -120:
                raise TushareError('Tushare盘中日线源日期或时间无效')
            if ('09:35'<=now.strftime('%H:%M')<='11:30' or '13:01'<=now.strftime('%H:%M')<='15:00') and (now.replace(tzinfo=None)-stamp.to_pydatetime()).total_seconds()>180:
                raise TushareError('Tushare盘中日线已陈旧')
            current=dict(date=stamp.normalize(),**{k:row[k] for k in ('open','high','low','close','amount')},volume=float(row.vol)/100)
            data=pd.concat([data[data.date.dt.date<now.date()],pd.DataFrame([current])],ignore_index=True)
        checked=normalize_bars(data)
        if len(checked)!=len(data) or checked.amount.isna().any() or (checked.amount<0).any():raise TushareError('Tushare日线OHLCV或成交额无效')
        data=checked
        data.attrs['source_note']='Tushare不复权日线；历史成交量为手、金额千元转为元；盘中日线量由股转为手，金额为元，按源时间确认。'
        return data

    def money_history(self, code, sector=False):
        now=datetime.now(TZ)
        params=dict(start_date=(now-timedelta(days=10)).strftime('%Y%m%d'),end_date=now.strftime('%Y%m%d'))
        if sector:
            if not re.fullmatch(r'BK\d+',code):raise ValueError('板块代码无效')
            params['content_type']='行业'
        else:params['ts_code']=ts_code(code)
        frame=self.query('moneyflow_ind_dc' if sector else 'moneyflow_dc',params,
                         'ts_code,trade_date,net_amount,net_amount_rate',ttl=900)
        if any(k not in frame for k in ('ts_code','trade_date','net_amount','net_amount_rate')):
            raise TushareError('Tushare资金历史字段不完整')
        if sector:
            if len(frame)>=5000:raise TushareError('Tushare行业资金历史达到行数上限，完整性未确认')
            frame=frame[frame.ts_code.astype(str).str.split('.').str[0]==code]
        else:frame=self._symbol_rows(frame,code)
        rows={}
        for _,r in frame.iterrows():
            date=datetime.strptime(str(r.trade_date),'%Y%m%d').strftime('%Y-%m-%d')
            # Current day is excluded even after close until next-day retrieval:
            # this endpoint is historical, never a substitute for live funds.
            if date>=now.strftime('%Y-%m-%d'):continue
            net=float(r.net_amount)*(1 if sector else 10000)
            ratio=float(r.net_amount_rate)
            if not math.isfinite(net) or not math.isfinite(ratio):raise TushareError('Tushare资金历史数值无效')
            item=dict(date=date,net=net,ratio=ratio)
            if date in rows and rows[date]!=item:raise TushareError('Tushare同日资金历史冲突')
            rows[date]=item
        history=[rows[d] for d in sorted(rows)][-5:]
        if not history:raise TushareError('Tushare缺少匹配代码的已完成资金历史')
        return dict(history[-1],status='ok',history=history,positive_days=None,five_day_net=None,
                    source='Tushare转供东方财富盘后资金',retrieved_at=now_text(),
                    period='已完成交易日历史；非实时资金',historical_only=True)
