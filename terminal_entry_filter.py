"""Private frozen entry models; missing evidence never receives a buy score."""
import hashlib
import json
import re
import threading
import time
from datetime import datetime,timedelta
from pathlib import Path

from terminal_entry_features import entry_features,volume_price_features,predict_entry,finite
from terminal_paper import clock


def model_id(value):
    if not isinstance(value,str) or value and not re.fullmatch('[a-z][a-z0-9-]{0,79}',value):
        raise ValueError('买入过滤模型编号无效')
    return value


def entry_eligible(row):
    return row.get('entry_pass',True) is True


class EntryFilterService:
    def __init__(self,root,reader=None,now=clock):
        self.root=Path(root);self.reader=reader;self.now=now
        self.lock=threading.RLock();self.cache={};self.market_cache={}

    def load(self,identifier):
        identifier=model_id(identifier)
        if not identifier:raise ValueError('未指定过滤模型')
        raw=(self.root/'models'/(identifier+'.json')).read_bytes();record=json.loads(raw)
        model=record['model']
        if record.get('id')!=identifier or model.get('version')!='entry-model-v1':raise ValueError('过滤模型版本无效')
        if not 1<=len(model['features'])<=3 or len(model['return_coefficients'])!=len(model['features'])+1:
            raise ValueError('过滤模型维度无效')
        for f in model['features']:
            if not all(finite(f.get(k)) for k in ('lower','upper','center','scale')) or f['scale']<=0 or f['upper']<f['lower']:
                raise ValueError('过滤模型参数无效')
        if not all(len(model[k])==len(model['features'])+1 and all(finite(v) for v in model[k]) for k in ('return_coefficients','stop_coefficients','rise_coefficients')):
            raise ValueError('过滤模型系数无效')
        record['sha256']=hashlib.sha256(raw).hexdigest();return record

    def evaluate(self,config,code):
        identifier=config.get('entry_model','')
        if not identifier:return {}
        now=self.now();day=now[:10]
        evidence=dict(entry_model=identifier,entry_pass=False,entry_status='missing',rank=None,
                      entry_as_of=day+' 14:30:00',entry_checked_at=now)
        try:
            if now[11:16]<'14:30':raise ValueError('14:30量价快照尚未形成')
            record=self.load(identifier)
            key=(day,code,record['sha256'])
            with self.lock:cached=self.cache.get(key)
            if cached and time.monotonic()-cached[0]<86400:return dict(cached[1])
            snapshot=(self.reader(code,day,record['model']) if self.reader else self.snapshot(code,day,record['model']))
            if snapshot['as_of']!=day+' 14:30:00':raise ValueError('量价快照时点不符合模型要求')
            prediction=predict_entry(record['model'],snapshot['features'])
            if prediction is None:raise ValueError('模型所需量价特征缺失')
            expected=prediction['expected_net_pct']
            evidence.update(entry_pass=expected>0,entry_status='passed' if expected>0 else 'filtered',
                rank=expected,ranking_mode='entry',entry_label=record['label'],entry_model_sha256=record['sha256'],
                entry_source=snapshot['source'],entry_retrieved_at=snapshot.get('retrieved_at'),entry_features=[dict(f,value=snapshot['features'][f['feature']],coefficient=record['model']['return_coefficients'][i+1]) for i,f in enumerate(record['model']['features'])],
                entry_intercept=record['model']['return_coefficients'][0],entry_prediction=prediction,
                entry_reason=('通过买入门槛' if expected>0 else '预测净收益未大于0，保留公式入选但不买入'))
            with self.lock:self.cache[key]=(time.monotonic(),dict(evidence))
        except (ValueError,KeyError,TypeError,OSError,ArithmeticError) as exc:
            evidence['entry_reason']='过滤数据待确认：'+str(exc)[:180]
        except Exception:
            evidence['entry_reason']='过滤数据获取失败，暂不买入'
        return evidence

    def snapshot(self,code,day,model):
        try:return self._snapshot(code,day,model)
        except Exception:
            # An initially unpublished 14:30 bar must not poison the day's cache.
            # A later scheduler retry must be allowed to fetch confirmed data.
            for kind in ('daily','bars'):
                (self.root/'data'/day/kind/(code+'.json')).unlink(missing_ok=True)
            raise

    def _snapshot(self,code,day,model):
        # Import lazily to avoid a module cycle with the historical executor.
        from terminal_backtest import HistoricalData,selection_bars,sina_intraday_rows
        start=(datetime.strptime(day,'%Y-%m-%d')-timedelta(days=60)).strftime('%Y-%m-%d')
        data=HistoricalData(self.root/'data'/day,start,day)
        daily=data.daily(code);intraday=data.intraday(code)
        if daily['raw'][-1][0]!=day or daily['qfq'][-1][0]!=day:
            raise ValueError('日线日期缺失或不属于当前交易日')
        # Current-day qfq anchor has no future corporate actions. The existing
        # affine validator still verifies raw and adjusted OHLC agree.
        bars=selection_bars(daily,intraday,day,[])
        market=None
        if any(f['feature'].startswith(('relative_','market_')) for f in model['features']):
            with self.lock:
                market=self.market_cache.get(day)
                if market is None:
                    h=data.http();rows=h.get('https://web.ifzq.gtimg.cn/appstock/app/kline/kline',params={'param':'sh000001,day,,,640'},timeout=10).json()['data']['sh000001']['day']
                    payload=h.get('https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData',params={'symbol':'sh000001','scale':30,'ma':'no','datalen':1970},timeout=15).text
                    early=[r for r in sina_intraday_rows(payload) if r[0][:8]==day.replace('-','') and r[0][8:]<='1430']
                    past=[r for r in rows if r[0]<day]
                    if len(past)<5 or [r[0][8:] for r in early]!=['1000','1030','1100','1130','1330','1400','1430']:
                        raise ValueError('上证指数14:30快照或历史不足')
                    price=float(early[-1][2]);market=dict(change_today=(price/float(past[-1][2])-1)*100,change_5=(price/float(past[-5][2])-1)*100)
                    self.market_cache[day]=market
        features=entry_features(bars,market);features.update(volume_price_features(daily,intraday,day))
        return dict(features=features,as_of=day+' 14:30:00',retrieved_at=clock(),source='腾讯前复权/原始日线；新浪30分钟14:30累计量价；相对涨幅使用上证指数sh000001')
