import tempfile
import unittest
from datetime import datetime
from unittest.mock import Mock,patch

import pandas as pd
import requests
from flask import Flask

from terminal_engine import TZ,demo_history
from terminal_quotes import QuoteRouter
from terminal_strategy_sim import StrategyEngine,checked_config


class AdjustedQuoteTests(unittest.TestCase):
    def test_em_requests_qfq_with_sufficient_history(self):
        router=QuoteRouter()
        with patch('terminal_quotes.ak.stock_zh_a_hist',return_value=demo_history('000001')) as fetch:
            router._em('daily_qfq','600001')
        args=fetch.call_args.kwargs
        self.assertEqual(args['adjust'],'qfq')
        self.assertGreaterEqual((datetime.strptime(args['end_date'],'%Y%m%d')-datetime.strptime(args['start_date'],'%Y%m%d')).days,800)

    def test_tencent_explicit_adjusted_channel_never_uses_raw_series(self):
        response=Mock();response.json.return_value={'code':0,'data':{'sh600001':{'day':[['2026-01-01',20,20,21,19,200]],'qfqday':[['2026-01-01',10,10,10.5,9.5,200]]}}}
        with patch('terminal_quotes.requests.get',return_value=response) as fetch:
            frame=QuoteRouter()._tencent('daily_qfq','600001')
        self.assertEqual(frame.close.tolist(),[10]);self.assertEqual(frame.volume.tolist(),[200])
        self.assertTrue(fetch.call_args.args[0].endswith('/fqkline/get'))
        self.assertEqual(fetch.call_args.kwargs['params']['param'],'sh600001,day,,,500,qfq')
        response.json.return_value['data']['sh600001'].pop('qfqday')
        with patch('terminal_quotes.requests.get',return_value=response),self.assertRaises(ValueError):
            QuoteRouter()._tencent('daily_qfq','600001')

    def test_qfq_fallback_keeps_adjustment_and_skips_unadjusted_adapters(self):
        router=QuoteRouter();router.preferred='tdx'
        router._em=Mock(side_effect=requests.ReadTimeout());router._tencent=Mock(return_value=demo_history('000001'))
        router._tdx=Mock();router._tushare=Mock();router.tushare.configured=Mock(return_value=True)
        data=router.fetch('daily_qfq','600001')
        self.assertEqual(data.attrs['adjustment'],'qfq');self.assertIn('前复权',data.attrs['source'])
        self.assertTrue(data.attrs['source_fallback']);router._tdx.assert_not_called();router._tushare.assert_not_called()
        self.assertIn('当前适配未提供前复权',data.attrs['source_errors'][0])
        # An adjusted-channel timeout does not cool down the raw channel.
        router._em=Mock(return_value=demo_history('000001'));router.preferred='em'
        self.assertEqual(router.fetch('daily','600001').attrs['adjustment'],'none')

    def test_adjusted_outage_cannot_fall_back_to_raw_tdx(self):
        router=QuoteRouter();router._em=Mock(side_effect=RuntimeError());router._tencent=Mock(side_effect=RuntimeError());router._tdx=Mock(return_value=demo_history('000001'))
        with self.assertRaises(RuntimeError):router.fetch('daily_qfq','600001')
        router._tdx.assert_not_called()


class FormulaPriceSeparationTests(unittest.TestCase):
    def readers(self,tmp,offline=False):
        from terminal_api import register_terminal
        now=datetime(2026,11,13,15,2,tzinfo=TZ)
        dates=pd.bdate_range(end=now.date(),periods=500)
        raw=pd.DataFrame(dict(date=dates,open=18.,high=18.2,low=17.8,close=18.,volume=10000.,amount=18000000.))
        raw.loc[499,['open','high','low','close','amount']]=[9.5,10.1,9.4,10.,10000000.]
        raw.attrs.update(source='原始价格',adjustment='none')
        adjusted=raw.copy();adjusted.loc[:498,['open','high','low','close']]/=2
        adjusted.attrs.update(source='前复权价格',adjustment='qfq',retrieved_at=now.strftime('%Y-%m-%d %H:%M:%S'))
        stamp=dates[-1].strftime('%Y-%m-%d')
        def fetch(kind,code):
            if kind=='daily_qfq':
                if offline:raise RuntimeError('offline')
                return adjusted.copy()
            if kind=='daily':return raw.copy()
            if kind=='minute':return dict(points=[dict(time='15:00',price=10.,average=9.8,volume=1000)],as_of=stamp+' 15:00',date=stamp)
            raise AssertionError(kind)
        router=Mock();router.revision=0;router.fetch.side_effect=fetch
        with patch.dict('os.environ',{'TDX_NO_PAPER_WORKER':'1'}),patch('terminal_api.QuoteRouter',return_value=router),patch('terminal_api.StrategyEngine') as engine,patch('terminal_api.StrategyMarketScan') as scan:
            app=Flask('qfq_test',root_path=tmp);register_terminal(app,lambda:None,lambda _:None)
        return engine.call_args.args[1],scan.call_args.args[1],router,now

    def test_scan_and_requote_share_qfq_500_bars_but_execution_retains_actual_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            read,daily,router,now=self.readers(tmp)
            with patch('terminal_api.datetime',wraps=datetime) as dt:
                dt.now.return_value=now
                q=read('600001','live',dict(name='测试股票'))
            scanned=daily('600001','live',dict(name='测试股票'))
        self.assertTrue(q['valid']);self.assertEqual(q['price'],10.);self.assertEqual(q['prev_close'],18.)
        self.assertEqual(q['daily']['bars'][-2]['close'],18.)
        self.assertEqual(q['daily']['formula_bars'][-2]['close'],9.)
        self.assertEqual(q['daily']['formula_bars'],scanned['bars']);self.assertEqual(len(scanned['bars']),500)
        self.assertEqual(scanned['adjustment'],'qfq')
        self.assertEqual(router.fetch.call_args_list.count(unittest.mock.call('daily_qfq','600001')),1)
        item=dict(mode='live',config=checked_config(dict(name='测试',formula='C>REF(C,1)',pool='custom',codes=['600001'])))
        result=StrategyEngine(None,None,None)._assessment(dict(code='600001',name='测试股票'),q,item)
        self.assertTrue(result['matched']);self.assertEqual(result['source'],'前复权价格')
        self.assertEqual((result['adjustment'],result['bar_count']),('qfq',500))

    def test_adjusted_outage_preserves_monitoring_but_blocks_formula_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            read,daily,router,now=self.readers(tmp,offline=True)
            with patch('terminal_api.datetime',wraps=datetime) as dt:
                dt.now.return_value=now;q=read('600001','live',dict(name='测试股票'))
        self.assertTrue(q['valid']);self.assertEqual(q['price'],10.)
        self.assertTrue(q['daily']['bars']);self.assertEqual(q['daily']['formula_bars'],[])
        item=dict(mode='live',config=checked_config(dict(name='测试',formula='C>0',pool='custom',codes=['600001'])))
        result=StrategyEngine(None,None,None)._assessment(dict(code='600001'),q,item)
        self.assertFalse(result['data_ok']);self.assertFalse(result['matched']);self.assertIn('前复权',result['reason'])


if __name__=='__main__':unittest.main()
