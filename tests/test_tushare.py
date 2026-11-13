import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock,patch

import pandas as pd

from terminal_engine import TZ,demo_history
from terminal_funds import FundFlowService
from terminal_quotes import QuoteRouter
from terminal_tushare import TushareData,TushareError
from tools.configure_tushare import save_token

NOW=datetime(2026,11,13,14,20,tzinfo=TZ)
TOKEN='test-token-not-a-real-credential-000000'


class TushareTests(unittest.TestCase):
    def test_status_endpoint_reports_unconfigured_without_attempting_paid_calls(self):
        from flask import Flask
        from terminal_api import register_terminal
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'TDX_NO_PAPER_WORKER':'1','TUSHARE_TOKEN':''}), \
             patch('terminal_tushare.requests.post') as post:
            app=Flask('optional_sources',root_path=tmp);register_terminal(app,lambda:None,lambda _:None)
            c=app.test_client();data=c.get('/api/terminal/sources').get_json()['data']
            self.assertTrue(all(not item['configured'] for item in data['connections']))
            self.assertEqual(c.post('/api/terminal/sources',json={'preferred':'tushare'}).status_code,400)
            post.assert_not_called()

    def test_no_token_never_sends_a_request_and_priority_cannot_be_enabled(self):
        with patch.dict(os.environ,{'TUSHARE_TOKEN':''}),patch('terminal_tushare.requests.post') as post:
            client=TushareData()
            with self.assertRaises(TushareError):client.query('daily',{},'')
            self.assertEqual(client.connection()['status'],'unconfigured');post.assert_not_called()
            router=QuoteRouter()
            with self.assertRaises(ValueError):router.configure('tushare')
            router.preferred='tushare';router._em=Mock(return_value=demo_history('000001'))
            self.assertEqual(router.fetch('daily','000001').attrs['provider'],'em')
            self.assertTrue(router.fetch('daily','000001').attrs['source_fallback'])

    def test_hidden_setup_creates_private_file_and_api_never_exposes_token(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'TUSHARE_TOKEN':''}):
            path=Path(tmp)/'token';save_token(path,TOKEN)
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            client=TushareData(path);self.assertTrue(client.configured())
            self.assertNotIn(TOKEN,json.dumps(client.connection()))
            path.chmod(0o644);self.assertFalse(client.configured())
            with self.assertRaises(TushareError):client.query('daily',{},'')

    def test_permission_error_is_redacted_and_redirects_do_not_forward_token(self):
        response=Mock(status_code=200);response.json.return_value={'code':2002,'msg':'remote echo '+TOKEN}
        with patch.dict(os.environ,{'TUSHARE_TOKEN':TOKEN}),patch('terminal_tushare.requests.post',return_value=response) as post:
            client=TushareData()
            with self.assertRaisesRegex(TushareError,'2002'):client.query('daily',{},'')
            with self.assertRaisesRegex(TushareError,'冷却'):client.query('daily',{},'')
            self.assertEqual(post.call_count,1)
            self.assertNotIn(TOKEN,json.dumps(client.connection()))
            self.assertEqual(post.call_args.args[0],'https://api.tushare.pro')
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
            response.status_code=302
            with self.assertRaisesRegex(TushareError,'重定向'):client.query('rt_min_daily',{},'')

    def test_minute_shares_to_lots_and_wrong_symbol_rejected(self):
        client=TushareData()
        data=pd.DataFrame([dict(ts_code='000001.SZ',time='2026-11-13 14:18:00',open=10,high=11,low=9,close=10,vol=10000,amount=100000)])
        with patch.object(client,'query',return_value=data):
            bars=client.prices('minute','000001');self.assertEqual(bars.volume.iloc[0],100)
            self.assertEqual(bars.amount.iloc[0],100000);self.assertEqual(bars.low.iloc[0],9)
            with self.assertRaisesRegex(TushareError,'代码'):client.prices('minute','600001')
        data.loc[0,'high']=1
        with patch.object(client,'query',return_value=data),self.assertRaises(TushareError):client.prices('minute','000001')

    def test_daily_historical_and_live_units_and_source_time(self):
        client=TushareData()
        history=pd.DataFrame([dict(ts_code='000001.SZ',trade_date=d.strftime('%Y%m%d'),open=10,high=11,low=9,close=10,vol=100,amount=100) for d in pd.bdate_range(end='2026-11-12',periods=35)])
        live=pd.DataFrame([dict(ts_code='000001.SZ',trade_time='2026-11-13 14:19:00',open=10,high=10.5,low=9.8,close=10.2,vol=100000,amount=1000000)])
        with patch('terminal_tushare.datetime',wraps=datetime) as dt,patch.object(client,'query',side_effect=[history,live]):
            dt.now.return_value=NOW
            data=client.prices('daily','000001')
            self.assertEqual(len(data),36);self.assertEqual(data.amount.iloc[0],100000)
            self.assertEqual(data.volume.iloc[-1],1000);self.assertEqual(data.amount.iloc[-1],1000000)
        live.loc[0,'trade_time']='2026-11-12 14:19:00'
        with patch('terminal_tushare.datetime',wraps=datetime) as dt,patch.object(client,'query',side_effect=[history,live]):
            dt.now.return_value=NOW
            with self.assertRaisesRegex(TushareError,'日期'):client.prices('daily','000001')
        live.loc[0,'trade_time']='14:19:00'
        with patch('terminal_tushare.datetime',wraps=datetime) as dt,patch.object(client,'query',side_effect=[history,live]):
            dt.now.return_value=NOW
            with self.assertRaisesRegex(TushareError,'源日期'):client.prices('daily','000001')

    def test_stock_money_is_wan_yuan_sector_money_is_yuan_and_today_is_excluded(self):
        client=TushareData()
        fields=dict(trade_date='20261112',net_amount=2.5,net_amount_rate=1.2)
        stock=pd.DataFrame([dict(fields,ts_code='000001.SZ'),dict(fields,ts_code='000001.SZ',trade_date='20261113',net_amount=999)])
        sector=pd.DataFrame([dict(fields,ts_code='BK0475.DC')])
        with patch('terminal_tushare.datetime',wraps=datetime) as dt,patch.object(client,'query',side_effect=[stock,sector]):
            dt.now.return_value=NOW
            a=client.money_history('000001');b=client.money_history('BK0475',sector=True)
            self.assertEqual(a['net'],25000);self.assertEqual(b['net'],2.5)
            self.assertEqual(a['date'],'2026-11-12');self.assertEqual(len(a['history']),1)
            self.assertTrue(a['historical_only']);self.assertIsNone(a['positive_days'])

    def test_history_backup_never_supplies_live_money(self):
        backup=Mock();backup.configured.return_value=True
        backup.money_history.return_value=dict(status='ok',date='2026-11-12',net=20,ratio=1,
             history=[dict(date='2026-11-12',net=20,ratio=1)],source='Tushare转供东方财富盘后资金',period='盘后')
        service=FundFlowService(history_backup=backup)
        with patch.object(service,'_json',side_effect=RuntimeError()):
            self.assertEqual(service.sector_history('BK0475')['source'],'Tushare转供东方财富盘后资金')
            stock=service.stock('000001')
            self.assertEqual(stock['status'],'unavailable');self.assertIsNone(stock['net'])
            self.assertEqual(stock['history_status'],'ok');self.assertIsNone(stock['positive_days'])


if __name__=='__main__':unittest.main()
