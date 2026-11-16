import copy
import json
import subprocess
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pandas as pd
import requests
from flask import Flask

from terminal_akshare_funds import AKShareBusy, AKShareFundFlowService, call_akshare, history_rows, source_time
from terminal_akshare_worker import execute
from terminal_engine import TZ
from terminal_mainline import MainlineService

NOW = datetime(2026, 11, 16, 14, 0, tzinfo=TZ)
STAMP = int(NOW.timestamp())
DAYS = ['2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13', '2026-11-16']


def history(code, values=(1, 2, -3, 4, 5)):
    return dict(rows=[{'日期':d,'主力净流入-净额':v,'主力净流入-净占比':9} for d,v in zip(DAYS, values)],
                histories=[dict(code=code,requested=('90.' if code.startswith('BK') else '1.')+code)])


def fixture():
    meta=dict(f12='BK0001',f14='行业',f124=STAMP,f62=500000000,f184=9)
    rank=dict(rows=[{'名称':'行业','今日涨跌幅':3,'今日主力净流入-净额':500000000,'今日主力净流入-净占比':9}],
              snapshots=[dict(scope='m:90 t:2',total=1,rows=[meta])])
    boards=dict(rows=[{'板块名称':'行业','板块代码':'BK0001','上涨家数':10,'下跌家数':0}],
                snapshots=[dict(scope='m:90 t:2 f:!50',total=1,rows=[dict(meta)])])
    members=[];raw=[];quotes=[]
    for i in range(10):
        code=str(600010+i);net=(i+1)*1000000
        members.append({'代码':code,'名称':'股票'+code,'最新价':10,'今天涨跌幅':10 if i<2 else 5,'今日主力净流入-净额':net,'今日主力净流入-净占比':9})
        raw.append(dict(f12=code,f14='股票'+code,f124=STAMP,f62=net,f184=9))
        quotes.append({'代码':code,'成交额':(i+1)*1e8,'量比':2,'振幅':8})
    return {'stock_sector_fund_flow_rank':rank,'stock_board_industry_name_em':boards,
            'stock_sector_fund_flow_summary':dict(rows=members,snapshots=[dict(scope='b:BK0001',total=10,rows=raw)]),
            'stock_board_industry_cons_em':dict(rows=quotes,snapshots=[dict(scope='b:BK0001',total=10,rows=copy.deepcopy(raw))]),
            'stock_zt_pool_em':dict(rows=[{'代码':'600010','连板数':2,'炸板次数':0,'首次封板时间':'093000'},
                                        {'代码':'600011','连板数':1,'炸板次数':0,'首次封板时间':'094000'}],
                                    pools=[dict(date='20261116',count=2,total=2)]),
            'stock_individual_fund_flow_rank':dict(rows=[{'代码':r['代码'],'今日主力净流入-净额':r['今日主力净流入-净额'],'今日主力净流入-净占比':9} for r in members],
                                                   snapshots=[dict(scope='m:0+t:6,m:1+t:2',total=10,rows=raw)])}


class AKShareFundTests(unittest.TestCase):
    def setUp(self):
        self.dt=patch('terminal_akshare_funds.datetime',wraps=datetime)
        self.clock=self.dt.start();self.clock.now.return_value=NOW;self.addCleanup(self.dt.stop)

    def test_history_preserves_signed_yuan_and_three_five_day_windows(self):
        result=history_rows(history('600010'),'600010')
        self.assertEqual(result['three_day_net'],6);self.assertEqual(result['five_day_net'],9)
        self.assertEqual(result['positive_days'],2);self.assertIn('AKShare',result['source'])
        self.assertEqual(result['date'],'2026-11-16')

    def test_history_rejects_wrong_code_future_and_duplicate_dates(self):
        for mutation in ('code','future','duplicate'):
            data=history('600010')
            if mutation=='code':data['histories'][0]['code']='600011'
            if mutation=='future':data['rows'][-1]['日期']='2099-01-01'
            if mutation=='duplicate':data['rows'].append(data['rows'][-1])
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):history_rows(data,'600010')

    def test_missing_timestamp_is_not_replaced_by_fetch_time(self):
        with self.assertRaises(ValueError):source_time(None)
        self.assertEqual(source_time(STAMP-240)['status'],'stale')
        self.assertEqual(source_time(STAMP-86400)['date'],'2026-11-15')

    def test_sectors_use_akshare_and_retain_actual_date(self):
        data=fixture();service=AKShareFundFlowService()
        with patch('terminal_akshare_funds.call_akshare',side_effect=lambda method,**kw:data[method]) as api,patch.object(service,'_json',side_effect=AssertionError('direct HTTP forbidden')):
            result=service.sectors()
        self.assertEqual(result['status'],'ok');row=result['items']['行业']
        self.assertEqual(row['sector_code'],'BK0001');self.assertEqual(row['net'],500000000)
        self.assertIsNone(row['flat_count']);self.assertEqual(row['date'],'2026-11-16')
        self.assertEqual([c.args[0] for c in api.call_args_list],['stock_sector_fund_flow_rank','stock_board_industry_name_em'])

    def test_incomplete_or_stale_sector_snapshot_does_not_rank(self):
        for mutation in ('missing_page','stale','mismatch'):
            data=fixture()
            if mutation=='missing_page':data['stock_sector_fund_flow_rank']['snapshots'][0]['total']=2
            if mutation=='stale':data['stock_sector_fund_flow_rank']['snapshots'][0]['rows'][0]['f124']=STAMP-86400
            if mutation=='mismatch':data['stock_sector_fund_flow_rank']['rows'][0]['今日主力净流入-净额']=1
            with self.subTest(mutation=mutation),patch('terminal_akshare_funds.call_akshare',side_effect=lambda method,**kw:data[method]):
                result=AKShareFundFlowService().sectors();self.assertEqual(result['status'],'unavailable');self.assertIsNone(result['net'])

    def test_members_retain_volume_ratio_amount_and_source_timestamp(self):
        data=fixture();s=AKShareFundFlowService()
        with patch('terminal_akshare_funds.call_akshare',side_effect=lambda method,**kw:data[method]):
            rows=s.members('BK0001')
        self.assertEqual(len(rows),10);self.assertEqual(rows[0]['amount'],1)
        self.assertEqual(rows[0]['volume_ratio'],2);self.assertEqual(rows[0]['quote_as_of'],'14:00:00')
        self.assertIn('AKShare',rows[0]['funds']['source'])

    def test_missing_member_quote_or_fund_date_blocks_complete_sector(self):
        for mutation in ('missing_quote','wrong_date'):
            data=fixture()
            if mutation=='missing_quote':data['stock_board_industry_cons_em']['rows'].pop()
            else:data['stock_sector_fund_flow_summary']['snapshots'][0]['rows'][0]['f124']=STAMP-86400
            with self.subTest(mutation=mutation),patch('terminal_akshare_funds.call_akshare',side_effect=lambda method,**kw:data[method]),self.assertRaises(ValueError):
                AKShareFundFlowService().members('BK0001')

    def test_stock_live_and_history_are_both_akshare(self):
        data=fixture()
        def fetch(method,**kw):
            return history(kw['stock']) if method=='stock_individual_fund_flow' else data[method]
        with patch('terminal_akshare_funds.call_akshare',side_effect=fetch) as api:
            r=AKShareFundFlowService().stock('600010')
        self.assertEqual(r['net'],1000000);self.assertEqual(r['three_day_net'],1000001)
        self.assertEqual(r['five_day_net'],1000004);self.assertEqual(r['positive_days'],2)
        self.assertEqual([c.args[0] for c in api.call_args_list],['stock_individual_fund_flow_rank','stock_individual_fund_flow'])

    def test_limit_pool_checks_actual_response_date_including_empty_pool(self):
        for wrong in (False,True):
            data=fixture()['stock_zt_pool_em'];data['pools'][0]['date']='20261113' if wrong else '20261116'
            with patch('terminal_akshare_funds.call_akshare',return_value=data):
                r=AKShareFundFlowService().limit_pool('2026-11-16')
            self.assertEqual(r['status'],'unavailable' if wrong else 'ok')
        with patch('terminal_akshare_funds.call_akshare',return_value=dict(rows=[],pools=[dict(date='20261116',total=0,count=0)])):
            self.assertEqual(AKShareFundFlowService().limit_pool('2026-11-16')['items'],{})

    def test_complete_akshare_mainline_keeps_source_breadth_and_roles(self):
        data=fixture()
        def fetch(method,**kw):
            if method=='stock_sector_fund_flow_hist':return history('BK0001',(1,2,3,4,5))
            if method=='stock_individual_fund_flow':return history(kw['stock'],(1,2,3,4,5))
            return data[method]
        with patch('terminal_akshare_funds.call_akshare',side_effect=fetch):result=MainlineService(AKShareFundFlowService()).build()
        self.assertIn('AKShare',result['source']);self.assertEqual(result['groups'][0]['breadth'],100)
        self.assertEqual(result['groups'][0]['funds']['three_day_net'],500000007)
        self.assertEqual(result['groups'][0]['board_type'],'main')
        self.assertTrue(any(s['role']=='龙头候选' for s in result['groups'][0]['leaders']))
        self.assertTrue(any(s['role']=='趋势龙候选' for s in result['groups'][0]['leaders']))

    def test_failed_interface_uses_backoff_without_demo_or_old_direct_http(self):
        s=AKShareFundFlowService()
        with patch('terminal_akshare_funds.call_akshare',side_effect=ValueError('AKShare：HTTP 502')) as api,patch.object(s,'_json',side_effect=AssertionError()):
            self.assertEqual(s.stock('600010')['status'],'unavailable')
            self.assertEqual(s.stock('600011')['status'],'unavailable')
        self.assertEqual(api.call_count,2)  # one live + one history probe, across both symbols
        self.assertTrue(all('AKShare' in h['reason'] for h in s.health()))

    def test_child_process_timeout_is_bounded_and_no_parent_requests_patch(self):
        original=requests.Session.request
        with patch('terminal_akshare_funds.subprocess.run',side_effect=subprocess.TimeoutExpired('worker',40)),self.assertRaises(TimeoutError):
            call_akshare('stock_individual_fund_flow',stock='600010',market='sh')
        self.assertIs(requests.Session.request,original)

    def test_local_capacity_does_not_mark_upstream_unavailable(self):
        service=AKShareFundFlowService()
        with patch('terminal_akshare_funds.call_akshare',side_effect=AKShareBusy('AKShare采集繁忙')):
            with self.assertRaises(ValueError):service._fetch('stock_sector_fund_flow_rank')
        self.assertFalse(any(h.get('status')=='unavailable' for h in service.health()))

    def test_date_only_history_remains_visible_without_live_trading_permission(self):
        def fetch(method,**kw):
            if method=='stock_individual_fund_flow':return history('600010')
            raise ValueError('AKShare：HTTP 502')
        with patch('terminal_akshare_funds.call_akshare',side_effect=fetch):
            result=AKShareFundFlowService().stock('600010')
        self.assertEqual(result['net'],5);self.assertEqual(result['five_day_net'],9)
        self.assertEqual(result['status'],'unverified');self.assertIsNone(result['as_of'])

    def test_worker_calls_akshare_retains_timestamp_and_restores_session(self):
        original=requests.Session.request;params={'fs':'b:BK0001','fields':'f12,f3'}
        response=Mock();response.json.return_value={'data':{'total':1,'diff':[dict(f12='600010',f3=1,f124=STAMP)]}}
        def library(**kw):
            r=requests.get('https://example.invalid/data',params=params)
            payload=r.json();self.assertNotIn('f124',payload['data']['diff'][0])
            return pd.DataFrame([{'代码':'600010','量比':2}])
        with patch('requests.Session.request',return_value=response) as network,patch('akshare.stock_board_industry_cons_em',side_effect=library):
            patched= requests.Session.request
            data=execute('stock_board_industry_cons_em',{'symbol':'BK0001'})
            self.assertIs(requests.Session.request,patched)
        self.assertIs(requests.Session.request,original)
        self.assertEqual(data['snapshots'][0]['rows'][0]['f124'],STAMP)
        self.assertEqual(network.call_args.kwargs['timeout'],(4,8))
        self.assertNotIn('f124',params['fields'])

    def test_api_reports_actual_akshare_adapter(self):
        from terminal_api import register_terminal
        app=Flask('akshare_source_test')
        with patch.dict('os.environ',{'TDX_NO_PAPER_WORKER':'1'}):register_terminal(app,lambda:None,lambda _:None)
        self.assertEqual(app.test_client().get('/api/terminal/sources').get_json()['data']['funds_adapter'],'akshare')
