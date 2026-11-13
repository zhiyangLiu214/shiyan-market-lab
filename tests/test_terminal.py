import time
import unittest
from unittest.mock import patch

import pandas as pd

from flask import Flask
from app import app
from terminal_api import register_terminal
from terminal_engine import (analyze, demo_history, demo_minutes, intraday_analysis,
                             normalize_bars, normalize_weights, select_mainline_roles)


class FactorTests(unittest.TestCase):
    def test_weights_are_normalized_and_invalid_values_rejected(self):
        self.assertEqual(normalize_weights([40, 30, 30]), [.4, .3, .3])
        for weights in ([0, 0, 0], [-1, 1, 1], [float('nan'), 1, 1], [float('inf'), 1, 1]):
            with self.assertRaises(ValueError):
                normalize_weights(weights)

    def test_plan_consistent_and_weighted_score(self):
        result = analyze(demo_history('000001'), '000001', weights=[100, 0, 0])
        self.assertEqual(result['score'], result['scores'][0])
        p = result['plan']
        self.assertEqual(p['reference'], result['price'])
        self.assertEqual(p['date'], result['date'])
        self.assertLess(p['stop'], p['reference'])
        self.assertGreater(p['rr_target2'], p['rr_target1'])
        if p['target1'] is not None:
            self.assertGreater(p['target1'], p['reference'])
            self.assertAlmostEqual(p['rr'], (p['target1']-p['reference'])/(p['reference']-p['stop']), delta=.01)
        else:
            self.assertIsNone(p['rr'])

    def test_missing_amount_does_not_fabricate_vwap(self):
        result = analyze(demo_history('000001').drop(columns='amount'), '000001')
        self.assertIsNone(result['vwap5'])

    def test_historical_signals_do_not_change_with_future_bars(self):
        history = demo_history('000005')
        earlier = analyze(history.iloc[:100], '000005')
        full = analyze(history, '000005')
        prefix = [s for s in full['signals'] if s['date'] <= earlier['date']]
        self.assertEqual(earlier['signals'], prefix)

    def test_bad_and_duplicate_bars_are_removed(self):
        frame = demo_history('000001')
        bad = frame.iloc[[0]].copy()
        bad['high'] = -1
        normalized = normalize_bars(pd.concat([frame, frame.iloc[[1]], bad]))
        self.assertEqual(len(normalized), len(frame))
        with self.assertRaises(ValueError):
            analyze(frame.head(20), '000001')


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_pages_and_opt_in_demo(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/legacy').status_code, 200)
        result = self.client.get('/api/terminal/market?mode=demo').get_json()['data']
        self.assertTrue(result['demo'])
        self.assertEqual(len(result['groups']), 3)
        detail = self.client.get('/api/terminal/stock/000001?mode=demo').get_json()['data']
        self.assertTrue(detail['demo'])
        self.assertTrue(detail['bars'])
        self.assertEqual(detail['source'], '合成演示数据')

    def test_validation(self):
        self.assertEqual(self.client.get('/api/terminal/stock/abc?mode=demo').status_code, 400)
        response = self.client.post('/api/terminal/scan', json={'mode': 'demo', 'weights': [0, 0, 0]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get('/api/terminal/scan/missing').status_code, 404)

    def test_background_scan_finishes_and_sorts(self):
        response = self.client.post('/api/terminal/scan', json={'mode': 'demo', 'weights': [40, 30, 30]})
        self.assertIn(response.status_code, [200, 202])
        job_id = response.get_json()['data']['id']
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = self.client.get('/api/terminal/scan/' + job_id).get_json()['data']
            if job['status'] != 'running':
                break
            time.sleep(.05)
        self.assertEqual(job['status'], 'complete')
        self.assertEqual(job['completed'], 32)
        self.assertGreater(len(job['results']), 0)
        self.assertLess(len(job['results']), 32)
        passed = {c['name'] for c in job['board_checks'] if c['passed']}
        self.assertEqual({s['board'] for s in job['results']}, passed)
        self.assertNotIn('高端制造', passed)
        scores = [s['score'] for s in job['results']]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for stock in job['results']:
            detail = self.client.get('/api/terminal/stock/' + stock['code'] + '?mode=demo').get_json()['data']
            self.assertEqual(stock['plan'], detail['plan'])

    def test_live_source_failure_does_not_return_demo(self):
        with patch('terminal_api.QuoteRouter.fetch', side_effect=ConnectionError('all providers offline')):
            result = self.client.get('/api/terminal/stock/600519?mode=live')
        self.assertEqual(result.status_code, 503)
        self.assertFalse(result.get_json()['ok'])
        self.assertNotIn('data', result.get_json())


class SeparateFlowTests(unittest.TestCase):
    def wait_job(self, client, body):
        response = client.post('/api/terminal/scan', json=body)
        self.assertIn(response.status_code, [200, 202])
        path = '/api/terminal/scan/' + response.get_json()['data']['id']
        for _ in range(200):
            job = client.get(path).get_json()['data']
            if job['status'] != 'running':
                self.assertEqual(job['status'], 'complete')
                return job
            time.sleep(.03)
        self.fail('scan did not finish')

    def test_mainline_roles_use_distinct_quote_criteria(self):
        stocks = [dict(code='000001', price=10, pct=10, amount=2),
                  dict(code='000002', price=10, pct=8, amount=3),
                  dict(code='000003', price=10, pct=5, amount=10),
                  dict(code='000004', price=10, pct=1, amount=1)]
        roles = select_mainline_roles(stocks)
        self.assertEqual([s['code'] for s in roles], ['000001', '000003', '000004'])
        self.assertTrue(all('score' not in s for s in roles))
        self.assertEqual(select_mainline_roles([dict(code='000001', price=10, pct=-1)]), [])

    def test_scan_has_own_universe_and_enforces_gate(self):
        boards = pd.DataFrame([dict(name='行业' + str(i), pct=6-i) for i in range(5)])
        called = []
        def members(board):
            called.append(board)
            base = int(board[-1]) * 10
            return pd.DataFrame([dict(code=f'{base+i:06d}', name='测试' + str(i), board=board)
                                 for i in range(1, 7)])
        isolated = Flask('test_flows')
        register_terminal(isolated, lambda: boards, members)
        with patch('terminal_api.ak.stock_zh_a_hist', return_value=demo_history('000001')), \
             patch('terminal_api.select_mainline_roles', side_effect=AssertionError('mainline must not run')), \
             patch('terminal_api.qualifies_momentum', side_effect=lambda d: d['board'] != '行业1' or int(d['code'][-1]) <= 4):
            job = self.wait_job(isolated.test_client(), {'mode': 'live', 'weights': [40,30,30]})
        self.assertIn('行业3', called)  # fourth sector, outside today's three mainlines
        self.assertIn('行业4', called)  # fifth sector meets the inclusive 2% gate
        passed = {c['name'] for c in job['board_checks'] if c['passed']}
        self.assertNotIn('行业1', passed)  # four qualifying stocks cannot pass a five-stock gate
        self.assertIn('行业3', passed)
        self.assertTrue(all(s['board'] in passed for s in job['results']))
        self.assertTrue(all('role' not in s for s in job['results']))

    def test_high_gate_returns_empty_without_fallback(self):
        isolated = Flask('empty_scan')
        register_terminal(isolated, lambda: None, lambda _: None)
        job = self.wait_job(isolated.test_client(), {'mode':'demo', 'min_board_pct':15})
        self.assertEqual(job['results'], [])
        self.assertEqual(job['board_checks'], [])

    def test_scan_score_floor_is_inclusive_and_independent_of_board_breadth(self):
        isolated = Flask('score_floor')
        register_terminal(isolated, lambda: None, lambda _: None)
        def scored(frame, code, name, board, *args):
            return dict(code=code, name=name, board=board, pct=1,
                        score=[84.9, 85, 90][int(code) % 3])
        with patch('terminal_api.analyze', side_effect=scored), \
             patch('terminal_api.qualifies_momentum', return_value=True):
            job = self.wait_job(isolated.test_client(), {'mode':'demo'})
        self.assertTrue(job['results'])
        self.assertTrue(all(s['score'] >= 85 for s in job['results']))
        self.assertIn(85, [s['score'] for s in job['results']])
        self.assertTrue(all(c['qualified']==c['sampled'] for c in job['board_checks']))
        self.assertEqual(job['min_score'],85)

    def test_intraday_ignores_zero_open_and_excludes_lunch_and_after_close(self):
        frame = demo_minutes('000001')
        frame['开盘'] = 0
        bad = frame.iloc[[0, 1]].copy()
        bad['时间'] = pd.to_datetime(['2026-11-09 12:00', '2026-11-09 15:30'])
        result = intraday_analysis(pd.concat([frame, bad]), '000001', True)
        self.assertEqual(len(result['points']), 240)
        self.assertNotIn('12:00', [p['time'] for p in result['points']])
        self.assertNotIn('15:30', [p['time'] for p in result['points']])
        self.assertTrue(result['signals'])
        self.assertEqual(result['plan']['reference'], result['signals'][0]['price'])
        earlier = intraday_analysis(frame.iloc[:110], '000001', True)
        self.assertEqual(earlier['signals'], [s for s in result['signals'] if s['time'] <= '11:20'])

    def test_intraday_endpoint_and_validation(self):
        client = app.test_client()
        result = client.get('/api/terminal/intraday/000001?mode=demo').get_json()
        self.assertTrue(result['ok'])
        self.assertTrue(result['data']['demo'])
        self.assertEqual(client.get('/api/terminal/intraday/bad').status_code, 400)
        for params in ({'min_companies':4}, {'min_board_pct':-1}, {'min_companies':9}):
            self.assertEqual(client.post('/api/terminal/scan', json=params).status_code, 400)


class StockNameTests(unittest.TestCase):
    def setUp(self):
        self.fund_patch = patch('terminal_api.FundFlowService.stock', return_value={'status': 'unavailable', 'net': None})
        self.fund_patch.start()
        self.addCleanup(self.fund_patch.stop)

    def test_fallback_name_is_cached_and_symbol_verified(self):
        isolated = Flask('stock_names')
        register_terminal(isolated, lambda: None, lambda _: None)
        client = isolated.test_client()
        with patch('terminal_api.ak.stock_zh_a_hist', return_value=demo_history('000001')), \
             patch('terminal_api.ak.stock_individual_info_em', side_effect=RuntimeError('unavailable')) as primary, \
             patch('terminal_api.requests.get') as fallback:
            fallback.return_value.content = 'v_sh600519="1~贵州茅台~600519~";'.encode('gbk')
            route = '/api/terminal/stock/600519?name=600519'
            self.assertEqual(client.get(route).get_json()['data']['name'], '贵州茅台')
            self.assertEqual(client.get(route).get_json()['data']['name'], '贵州茅台')
            self.assertEqual(primary.call_count, 1)
            self.assertEqual(fallback.call_count, 1)
            # A different symbol must never supply this stock's name.
            fallback.return_value.content = 'v_sz000001="1~平安银行~000001~";'.encode('gbk')
            result = client.get('/api/terminal/stock/600888').get_json()['data']
            self.assertNotEqual(result['name'], '平安银行')


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.fund_patch = patch('terminal_api.FundFlowService.stock', return_value={'status': 'unavailable', 'net': None})
        self.fund_patch.start()
        self.addCleanup(self.fund_patch.stop)

    def test_daily_cache_expires_before_thirty_second_refresh(self):
        isolated = Flask('fresh_daily')
        register_terminal(isolated, lambda: None, lambda _: None)
        first = demo_history('000001')
        second = first.copy()
        second.loc[second.index[-1], 'close'] += 1
        second.loc[second.index[-1], 'high'] += 1
        client = isolated.test_client()
        with patch('terminal_api.time.time', return_value=1000) as clock, \
             patch('terminal_api.ak.stock_zh_a_hist', side_effect=[first, second]) as source:
            route = '/api/terminal/stock/600888?name=测试'
            before = client.get(route).get_json()['data']['price']
            self.assertEqual(client.get(route).get_json()['data']['price'], before)
            self.assertEqual(source.call_count, 1)
            clock.return_value = 1030
            after = client.get(route).get_json()['data']['price']
            self.assertEqual(source.call_count, 2)
            self.assertNotEqual(before, after)

    def test_minute_cache_updates_at_next_refresh(self):
        isolated = Flask('fresh_minute')
        register_terminal(isolated, lambda: None, lambda _: None)
        first = demo_minutes('000001')
        from datetime import datetime
        from terminal_engine import TZ
        first['时间']=first['时间'].astype(str).str.replace('2026-11-09',datetime.now(TZ).strftime('%Y-%m-%d'),regex=False)
        second = first.copy()
        second.loc[second.index[-1], '收盘'] += .5
        second.loc[second.index[-1], '最高'] += .5
        client = isolated.test_client()
        with patch('terminal_api.time.time', return_value=1000) as clock, \
             patch('terminal_engine.datetime', wraps=datetime) as market_clock, \
             patch('terminal_api.ak.stock_zh_a_hist_min_em', side_effect=[first, second]) as source:
            # The fixture contains a full session. Freeze after close so the
            # altered 15:00 bar is confirmed, independent of the test run time.
            market_clock.now.return_value = datetime.now(TZ).replace(hour=16, minute=0, second=0, microsecond=0)
            route = '/api/terminal/intraday/600888'
            before = client.get(route).get_json()['data']['points'][-1]['price']
            self.assertEqual(client.get(route).get_json()['data']['points'][-1]['price'], before)
            self.assertEqual(source.call_count, 1)
            clock.return_value = 1030
            after = client.get(route).get_json()['data']['points'][-1]['price']
            self.assertEqual(source.call_count, 2)
            self.assertNotEqual(before, after)


if __name__ == '__main__':
    unittest.main()
