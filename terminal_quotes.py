"""Independent price providers with bounded retries, circuit breaking and source metadata."""
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd
import requests

from terminal_engine import TZ, normalize_bars, intraday_analysis, validate_code, now_text
from terminal_tushare import TushareData, TushareError

LABELS = {'em':'东方财富', 'tencent':'腾讯行情', 'tushare':'Tushare', 'tdx':'通达信普通行情'}


def symbol(code):
    validate_code(code)
    return ('sh' if code.startswith('6') else 'bj' if code[0] in '489' else 'sz')+code


def failure(exc):
    if isinstance(exc,TushareError):return str(exc)
    if isinstance(exc, requests.HTTPError):
        return 'HTTP '+str(exc.response.status_code if exc.response is not None else '错误')
    if isinstance(exc, (requests.Timeout, TimeoutError)): return '连接或读取超时'
    if isinstance(exc, requests.ConnectionError): return '连接不可用'
    if isinstance(exc, ImportError): return '客户端未安装'
    return '返回数据无效或节点不可用（'+type(exc).__name__+'）'


class QuoteRouter:
    def __init__(self, config_path=None):
        self.path=Path(config_path) if config_path else None
        self.tushare=TushareData(self.path.parent/'tushare-token' if self.path else None)
        self.lock=threading.RLock();self.states={};self.until={};self.preferred='auto';self.revision=0;self.funds_preferred='auto'
        if self.path:
            try:
                p=json.loads(self.path.read_text());self.funds_preferred=p.get('funds_preferred','auto') if p.get('funds_preferred','auto') in ('auto','em','dxx','ths') else 'auto';self.preferred=p['preferred'] if p['preferred'] in ('auto',*LABELS) else 'auto'
            except (OSError,ValueError,KeyError):pass

    def configure(self, preferred, funds_preferred='auto'):
        if preferred not in ('auto',*LABELS): raise ValueError('无效行情源')
        if preferred=='tushare' and not self.tushare.configured():raise ValueError('请先在服务器配置Tushare Token，再验证行情权限')
        if funds_preferred not in ('auto','em','dxx','ths'):raise ValueError('无效资金源')
        with self.lock:
            if self.path:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps({'preferred':preferred,'funds_preferred':funds_preferred}));tmp.replace(self.path)
            self.preferred=preferred;self.funds_preferred=funds_preferred;self.until.clear();self.revision+=1
        return self.snapshot()

    def snapshot(self):
        connection=self.tushare.connection()
        with self.lock:return dict(preferred=self.preferred,funds_preferred=self.funds_preferred, revision=self.revision, providers=[dict(channel=k[0],provider=k[1],name=LABELS.get(k[1],{'dxx':'短线侠','ths':'同花顺'}.get(k[1],k[1])),**v) for k,v in self.states.items()],
                                   connections=[connection,dict(provider='qmt',name='QMT／迅投',status='unconfigured',configured=False,reason='待确认客户端与数据权限；尚未接入采集端')])

    def fetch(self, kind, code):
        validate_code(code)
        if kind not in ('daily','daily_qfq','minute'):raise ValueError('无效行情类型')
        daily=kind in ('daily','daily_qfq')
        with self.lock:preferred=self.preferred
        order=[p for p in LABELS if p!='tushare' or self.tushare.configured()]
        # Only these adapters have a verified forward-adjusted daily request.
        # Never feed raw bars from the ordinary TDX/Tushare adapters to a formula.
        if kind=='daily_qfq':order=[p for p in order if p in ('em','tencent')]
        if preferred in order: order.remove(preferred);order.insert(0,preferred)
        errors=([LABELS[preferred]+'：当前适配未提供前复权，已回退前复权来源'] if kind=='daily_qfq' and preferred in ('tdx','tushare') else
                ['Tushare：未配置，已回退其他来源'] if preferred=='tushare' and preferred not in order else [])
        for provider in order:
            key=(kind,provider)
            with self.lock:
                if max(self.until.get(key,0),self.until.get((kind,provider,code),0))>time.monotonic():
                    errors.append(LABELS[provider]+'：暂不可用，冷却中');continue
            try:
                frame=getattr(self, '_'+provider)(kind,code)
                if daily:
                    checked=normalize_bars(frame)
                    if len(checked)<35:raise ValueError('日线不足35根')
                    if checked.date.max().date()>datetime.now(TZ).date():raise ValueError('未来日期')
                    data=frame
                else:
                    data=intraday_analysis(frame,code)
                    today=datetime.now(TZ)
                    if today.weekday()<5 and today.strftime('%H:%M')>='09:35' and data['date']!=today.strftime('%Y-%m-%d'):
                        raise ValueError('分钟数据不是当前交易日')
                meta=dict(source=LABELS[provider]+('日线 · 前复权' if kind=='daily_qfq' else '日线 · 不复权' if daily else '分钟行情'),provider=provider,
                          source_fallback=bool(errors),source_errors=errors,source_note=frame.attrs.get('source_note',''),retrieved_at=now_text())
                if daily:data.attrs.update(meta,adjustment='qfq' if kind=='daily_qfq' else 'none')
                else:data.update(meta)
                with self.lock:
                    self.until.pop(key,None);self.states[key]=dict(status='ok',at=now_text(),code=code,reason='已切至备用' if errors else '正常')
                return data
            except Exception as exc:
                reason=failure(exc);errors.append(LABELS[provider]+'：'+reason)
                with self.lock:
                    cooldown_key=key if isinstance(exc,(requests.Timeout,requests.ConnectionError,ImportError,TushareError)) or isinstance(exc,requests.HTTPError) and exc.response is not None and exc.response.status_code>=500 or provider=='tdx' and code[0] not in '489' else (kind,provider,code)
                    self.until[cooldown_key]=time.monotonic()+120;self.states[key]=dict(status='unavailable',at=now_text(),code=code,reason=reason)
        raise RuntimeError('行情源均不可用；'+'；'.join(errors))

    def _tushare(self,kind,code):
        return self.tushare.prices(kind,code)

    def _em(self,kind,code):
        if kind in ('daily','daily_qfq'):
            end=datetime.now(TZ)
            frame=ak.stock_zh_a_hist(symbol=code,period='daily',start_date=(end-timedelta(days=900 if kind=='daily_qfq' else 400)).strftime('%Y%m%d'),end_date=end.strftime('%Y%m%d'),adjust='qfq' if kind=='daily_qfq' else '',timeout=8)
            return frame.tail(500) if kind=='daily_qfq' else frame
        return ak.stock_zh_a_hist_min_em(symbol=code,period='1',adjust='')

    def _tencent(self,kind,code):
        sym=symbol(code)
        url='https://web.ifzq.gtimg.cn/appstock/app/'+('fqkline/get' if kind=='daily_qfq' else 'kline/kline' if kind=='daily' else 'minute/query')
        params={'param':sym+',day,,,500,qfq'} if kind=='daily_qfq' else {'param':sym+',day,,,260'} if kind=='daily' else {'code':sym}
        r=requests.get(url,params=params,timeout=(4,8))
        r.raise_for_status();payload=r.json()
        if payload.get('code')!=0:raise ValueError('接口错误')
        data=payload.get('data',{}).get(sym)
        if not data:raise ValueError('返回代码不匹配')
        if kind in ('daily','daily_qfq'):
            rows=data.get('qfqday' if kind=='daily_qfq' else 'day',[])
            if not rows:raise ValueError('缺少前复权日线' if kind=='daily_qfq' else '缺少日线')
            frame=pd.DataFrame([x[:6] for x in rows],columns=['date','open','close','high','low','volume'])
            frame.attrs['source_note']='成交量单位：手；源未提供日成交额，五日VWAP留空，不以收盘价估造。'
            return frame
        raw=data.get('data',{});date=datetime.strptime(raw['date'],'%Y%m%d').strftime('%Y-%m-%d')
        rows=[];prev_volume=prev_amount=0.;last_time=''
        for text in raw.get('data',[]):
            parts=text.split()
            if len(parts)!=4:raise ValueError('分时缺少成交额')
            t,price,volume,amount=parts;volume=float(volume);amount=float(amount)
            if t<=last_time or volume<prev_volume or amount<prev_amount:raise ValueError('分时累计数据逆序')
            rows.append(dict(date=date+' '+t[:2]+':'+t[2:],close=float(price),volume=volume-prev_volume,amount=amount-prev_amount))
            prev_volume,prev_amount,last_time=volume,amount,t
        frame=pd.DataFrame(rows)
        frame.attrs['source_note']='累计成交量（手）及成交额（元）转为分钟增量；未提供分钟高低价，不生成依赖最低价的承接信号。'
        return frame

    def _tdx(self,kind,code):
        from pytdx.hq import TdxHq_API
        from pytdx.config.hosts import hq_hosts
        if code[0] in '489':raise ValueError('普通节点未核验北交所映射')
        # Public standard nodes; no brokerage account and no trading interface.
        for host,port in [('119.147.212.81',7709),('180.153.18.170',7709),('180.153.18.172',80)]:
            api=TdxHq_API(raise_exception=True)
            try:
                if not api.connect(host,port,time_out=2):continue
                rows=api.get_security_bars(9 if kind=='daily' else 8,1 if code.startswith('6') else 0,code,0,260 if kind=='daily' else 800)
                if not rows:continue
                frame=pd.DataFrame(rows).rename(columns={'datetime':'date','vol':'volume'})
                # Normalize only after checking amount/volume against the actual OHLC range.
                active=frame[frame.volume>0]
                if not active.empty:
                    avg=active.amount/active.volume
                    shares=((avg>=active.low*.99)&(avg<=active.high*1.01)).all()
                    lots=((avg/100>=active.low*.99)&(avg/100<=active.high*1.01)).all()
                    if shares==lots:raise ValueError('无法确认通达信成交量单位')
                    if shares:frame['volume']=frame.volume/100
                frame.attrs['source_note']='通达信普通行情；成交量已按成交额与价格校验并归一为手。不包含Level-2主力资金。'
                return frame
            except Exception:continue
            finally:
                try:api.disconnect()
                except Exception:pass
                if api.client:
                    api.client.close();api.client=None
        raise ValueError('通达信普通节点均不可用')
