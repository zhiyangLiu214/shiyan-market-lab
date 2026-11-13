import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

import pandas as pd
import requests
from terminal_quotes import QuoteRouter
from terminal_engine import demo_history,demo_minutes,TZ
from terminal_source_market import MarketRouter,parse_dxx

class QuoteSourceTests(unittest.TestCase):
    def test_primary_failure_falls_back_and_circuit_avoids_repeated_timeout(self):
        r=QuoteRouter();r._em=Mock(side_effect=requests.ReadTimeout());r._tencent=Mock(side_effect=lambda *x:demo_history('000001'));r._tdx=Mock()
        first=r.fetch('daily','600001');second=r.fetch('daily','600002')
        self.assertEqual(first.attrs['provider'],'tencent');self.assertTrue(first.attrs['source_fallback'])
        self.assertIn('超时',first.attrs['source_errors'][0]);r._em.assert_called_once();r._tdx.assert_not_called()
        self.assertEqual(second.attrs['provider'],'tencent')

    def test_invalid_primary_data_uses_backup_and_never_demo_substitution(self):
        r=QuoteRouter();r._em=Mock(return_value=pd.DataFrame());r._tencent=Mock(side_effect=RuntimeError());r._tdx=Mock(side_effect=RuntimeError())
        with self.assertRaises(RuntimeError):r.fetch('daily','600001')
        self.assertTrue(all(p['status']=='unavailable' for p in r.snapshot()['providers']))

    def test_persisted_manual_priority_still_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'sources.json';r=QuoteRouter(path);r.configure('tdx','ths');r=QuoteRouter(path)
            self.assertEqual(r.funds_preferred,'ths');self.assertEqual(r.preferred,'tdx')
            r._tdx=Mock(side_effect=RuntimeError());r._em=Mock(side_effect=lambda *x:demo_history('000001'))
            data=r.fetch('daily','600001');self.assertEqual(data.attrs['provider'],'em');self.assertTrue(data.attrs['source_fallback'])

    def test_tencent_cumulative_minute_volume_and_amount_become_deltas(self):
        r=QuoteRouter();response=Mock();response.json.return_value={'code':0,'data':{'sh600001':{'data':{'date':'20261112','data':['0930 10 100 100000','0931 11 120 122000']}}}}
        with patch('terminal_quotes.requests.get',return_value=response):frame=r._tencent('minute','600001')
        self.assertEqual(frame.volume.tolist(),[100,20]);self.assertEqual(frame.amount.tolist(),[100000,22000]);self.assertNotIn('low',frame)
        response.json.return_value['data']['sh600001']['data']['data'].append('0932 10 119 130000')
        with patch('terminal_quotes.requests.get',return_value=response),self.assertRaises(ValueError):r._tencent('minute','600001')

    def test_wrong_symbol_cannot_silently_supply_another_stock(self):
        r=QuoteRouter();response=Mock();response.json.return_value={'code':0,'data':{'sz000001':{'day':[]}}}
        with patch('terminal_quotes.requests.get',return_value=response),self.assertRaises(ValueError):r._tencent('daily','600001')

    def test_fund_backup_is_whole_snapshot_with_own_provenance(self):
        primary=Mock();primary.build.side_effect=RuntimeError();quotes=QuoteRouter();r=MarketRouter(primary,quotes)
        r.dxx=Mock(side_effect=RuntimeError());r.ths=Mock(return_value=dict(groups=[],readonly=True,source='同花顺',errors=['日期未核验']))
        data=r.build();self.assertTrue(data['source_fallback']);self.assertTrue(data['readonly']);self.assertEqual(data['funds_provider'],'ths')
        self.assertEqual(quotes.snapshot()['providers'][-1]['status'],'observation')

    def test_shortline_money_schema_units_and_missing_fields_are_honest(self):
        from datetime import datetime
        today=datetime.now(TZ).strftime('%Y-%m-%d')
        data=parse_dxx(dict(date=today,plates=[['801719','腾讯',71,2609088496,None,-1.97],['801001','芯片',3064,-2597261722,'6',-1.57]]))
        self.assertTrue(data['readonly']);self.assertEqual(data['groups'][0]['funds']['net'],2609088496)
        self.assertIsNone(data['groups'][0]['funds']['five_day_net']);self.assertIsNone(data['groups'][0]['limit_count'])
        self.assertEqual(data['flow_universe'][1]['limit_count'],6);self.assertIsNone(data['groups'][0]['strength'])
        self.assertNotEqual(data['groups'][0]['board_type'],'main')
        duplicate=['801719','腾讯',71,2609088496,None,-1.97]
        self.assertEqual(len(parse_dxx(dict(date=today,plates=[duplicate,duplicate]))['flow_universe']),1)
        with self.assertRaises(ValueError):parse_dxx(dict(date=today,plates=[duplicate,['801719','腾讯',71,1,None,-1.97]]))
        with self.assertRaises(ValueError):parse_dxx(dict(date='2099-01-01',plates=[['801001','芯片',1,1,1,1]]))
        with self.assertRaises(ValueError):parse_dxx(dict(date=today,plates=[['801001','芯片',1]]))

    def test_configuration_endpoint_rejects_unknown_provider(self):
        import os
        from flask import Flask
        from terminal_api import register_terminal
        with patch.dict(os.environ,{'TDX_NO_PAPER_WORKER':'1'}):
            app=Flask('source_test');register_terminal(app,lambda:None,lambda _:None)
        c=app.test_client();self.assertEqual(c.post('/api/terminal/sources',json={'preferred':'fake'}).status_code,400)
        data=c.post('/api/terminal/sources',json={'preferred':'tencent','funds_preferred':'em'}).get_json()['data']
        self.assertEqual(data['preferred'],'tencent');self.assertEqual(data['funds_preferred'],'em')
