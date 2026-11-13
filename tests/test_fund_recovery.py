"""Source outages, concurrent symbols, and independent paper recovery acceptance."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import requests

from terminal_funds import FundFlowService, FundsCooling, parse_live_stock
from terminal_engine import TZ
from datetime import datetime
from terminal_paper import PaperLedger
from terminal_paper_runner import PaperRunner


class FundRecoveryTests(unittest.TestCase):
    def test_concurrent_list_pages_are_paced_but_live_money_is_independent(self):
        service=FundFlowService();tick=[100.0];starts=[]
        def wait(seconds):tick[0]+=seconds
        def fetch(url,params):starts.append((url,tick[0]));return {'data':{}}
        with patch('terminal_funds.time.monotonic',side_effect=lambda:tick[0]), \
             patch('terminal_funds.time.sleep',side_effect=wait),patch.object(service,'_request_json',side_effect=fetch):
            url='https://push2.eastmoney.com/api/qt/clist/get'
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda i:service._json(url,{'pn':i}),range(4)))
            self.assertTrue(all(b[1]-a[1]>=1.19 for a,b in zip(starts,starts[1:])))
            before=tick[0]
            service._json('https://push2.eastmoney.com/api/qt/ulist.np/get',{})
            self.assertEqual(tick[0],before)

    def test_sector_twenty_row_pagination_is_complete_and_missing_page_rejected(self):
        stamp=int(datetime.now(TZ).timestamp())
        rows=[dict(f12='BK'+str(i).zfill(4),f14='行业'+str(i),f62=100,f184=2,f124=stamp) for i in range(21)]
        def page(url,params):
            self.assertEqual(params['pz'],20)
            return {'data':{'total':21,'diff':rows[(params['pn']-1)*20:params['pn']*20]}}
        with patch.object(FundFlowService,'_json',side_effect=page):
            self.assertEqual(len(FundFlowService().sectors()['items']),21)
        with patch.object(FundFlowService,'_json',return_value={'data':{'total':21,'diff':rows[:20]}}):
            missing=FundFlowService().sectors()
            self.assertEqual(missing['status'],'unavailable');self.assertIn('分页不完整',missing['reason'])

    def test_live_money_uses_source_time_and_yesterday_is_not_today(self):
        now=datetime(2026,11,13,14,10,tzinfo=TZ)
        row=dict(f12='002739',f62=411785122,f184=28.83,f124=int(now.timestamp()))
        with patch('terminal_funds.datetime',wraps=datetime) as dt:
            dt.now.return_value=now
            good=parse_live_stock({'data':{'diff':[row]}},'002739')
            self.assertEqual(good['date'],'2026-11-13');self.assertEqual(good['as_of'],'14:10:00')
            self.assertEqual(good['net'],411785122);self.assertEqual(good['ratio'],28.83)
            row['f124']-=86400
            self.assertEqual(parse_live_stock({'data':{'diff':[row]}},'002739')['status'],'stale')
            row['f124']=0
            with self.assertRaises(ValueError):parse_live_stock({'data':{'diff':[row]}},'002739')

    def test_current_snapshot_survives_history_outage_and_routes_are_independent(self):
        service=FundFlowService();now=datetime(2026,11,13,14,10,tzinfo=TZ)
        live={'data':{'diff':[dict(f12='600519',f62=100000,f184=5,f124=int(now.timestamp()))]}}
        with patch('terminal_funds.datetime',wraps=datetime) as dt,patch.object(service,'_json',side_effect=[live,requests.ConnectionError()]):
            dt.now.return_value=now
            result=service.stock('600519')
            self.assertEqual(result['status'],'ok');self.assertEqual(result['date'],'2026-11-13')
            self.assertEqual(result['history_status'],'unavailable');self.assertIsNone(result['positive_days'])
        with patch.object(service,'_request_json',side_effect=[requests.ConnectionError(),live]):
            with self.assertRaises(requests.ConnectionError):service._json('https://push2.eastmoney.com/api/qt/clist/get',{})
            self.assertEqual(service._json('https://push2.eastmoney.com/api/qt/ulist.np/get',{}),live)

    def test_failed_host_shared_across_symbols_and_recovers_with_backoff(self):
        service=FundFlowService()
        url='https://push2his.eastmoney.com/api/test'
        with patch('terminal_funds.time.monotonic',return_value=100) as now, \
             patch.object(service,'_request_json',side_effect=[requests.ConnectionError('private-url'),
                  requests.ConnectionError(), {'data':'recovered'}]) as fetch:
            with self.assertRaises(requests.ConnectionError): service._json(url,{})
            def another_symbol(i):
                try: service._json(url,{'secid':i})
                except FundsCooling: return True
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertTrue(all(pool.map(another_symbol,range(20))))
            self.assertEqual(fetch.call_count,1)
            self.assertEqual(service.health()[0]['retry_in'],30)
            self.assertNotIn('private',json.dumps(service.health()))
            now.return_value=131
            with self.assertRaises(requests.ConnectionError): service._json(url,{})
            self.assertEqual(service.health()[0]['retry_in'],60)
            now.return_value=192
            self.assertEqual(service._json(url,{})['data'],'recovered')
            self.assertEqual(service.health()[0]['status'],'ok')
            self.assertEqual(service.health()[0]['recoveries'],1)
            self.assertNotIn('outage_since',service.health()[0])

    def test_health_survives_restart_and_failed_history_does_not_wait_ten_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'fund-health.json';service=FundFlowService(path)
            with patch('terminal_funds.time.monotonic',return_value=100) as now, \
                 patch.object(service,'_request_json',side_effect=requests.Timeout()):
                with self.assertRaises(requests.Timeout):service._json('https://funds.example/test',{})
                self.assertTrue(path.exists())
                restored=FundFlowService(path)
                self.assertEqual(restored.health()[0]['failures_total'],1)
                self.assertEqual(restored.health()[0]['retry_in'],0)
                with patch.object(service,'_json',side_effect=[RuntimeError(),{'data':{'code':'BK0001','klines':['2026-11-13,1,0,0,0,1,2,0,0,0,0']}}]) as fetch:
                    self.assertEqual(service.sector_history('BK0001')['status'],'unavailable')
                    now.return_value=126
                    self.assertEqual(service.sector_history('BK0001')['status'],'ok')
                    self.assertEqual(fetch.call_count,2)


class PaperRecoveryAcceptance(unittest.TestCase):
    def test_two_accounts_recover_buy_hold_partial_exit_and_reconcile(self):
        # Synthetic fixtures only, isolated temporary SQLite; never call production APIs.
        with tempfile.TemporaryDirectory() as tmp:
            ledger=PaperLedger(Path(tmp)/'acceptance.db')
            ledger.start('live','2026-11-11 09:30:00')
            state=dict(now='2026-11-11 10:02:00',bar='10:01',day='2026-11-11',
                       funds='unavailable',price=10)
            def market(_):
                return dict(groups=[dict(name='测试板块',board_type='main',leaders=[dict(code='600001',name='主线样本')])])
            def detail(code,*args):
                return dict(name='样本'+code,score=90,support=8,
                            funds=dict(status=state['funds'],date=state['day'],net=10000,ratio=5),
                            plan=dict(date='2026-11-10',as_of='15:00',stop=9,target1=11.8,target2=13),
                            bars=[dict(date='2026-11-10',close=10)]*10+
                                 ([dict(date='2026-11-11',close=10.8)] if state['day']>'2026-11-11' else []))
            def minute(code,*args):
                return dict(date=state['day'],as_of=state['day']+' '+state['bar'],
                            points=[dict(time=state['bar'],price=state['price'],average=9.9,volume=10000)],
                            signals=[dict(time='10:01',kind='弱转强')] if state['day']=='2026-11-11' else [])
            runner=PaperRunner(ledger,market,lambda _:[dict(code='600002',score=90)],detail,minute)
            def tick():
                # Move both caches past their normal refresh interval without sleeping.
                runner.cache.clear();runner.last_pool.clear()
                with patch('terminal_paper_runner.clock',return_value=state['now']):runner.tick('live')
                return ledger.snapshot('live',state['now'])['accounts']
            self.assertTrue(all(not a['orders'] for a in tick()))
            state['funds']='stale'
            self.assertTrue(all(not a['orders'] for a in tick()))
            state['funds']='ok'
            self.assertTrue(all(len(a['orders'])==1 and not a['trades'] for a in tick()))
            self.assertTrue(all(not a['trades'] for a in tick()))  # Same minute cannot fill.
            state.update(now='2026-11-11 10:04:00',bar='10:03')
            accounts=tick()
            self.assertEqual([a['positions'][0]['code'] for a in accounts],['600001','600002'])
            self.assertTrue(all(a['positions'][0]['available']==0 for a in accounts))
            quantities=[a['positions'][0]['qty'] for a in accounts]
            state.update(now='2026-11-12 10:02:00',bar='10:01',day='2026-11-12',price=11.82)
            self.assertTrue(all(any(o['side']=='sell' and o['status']=='pending' for o in a['orders']) for a in tick()))
            state.update(now='2026-11-12 10:04:00',bar='10:03')
            accounts=tick()
            for i,a in enumerate(accounts):
                self.assertEqual(len(a['trades']),2)
                self.assertTrue(0<a['positions'][0]['qty']<quantities[i])
                self.assertGreater(a['realized'],0)
                self.assertAlmostEqual(a['pnl'],a['realized']+a['unrealized'],places=2)
                self.assertLess(a['exposure'],70)
            self.assertEqual(accounts[0]['nav'],accounts[1]['nav'])


if __name__=='__main__':unittest.main()
