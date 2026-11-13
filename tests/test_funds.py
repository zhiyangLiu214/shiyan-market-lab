import unittest
from unittest.mock import patch

from flask import Flask
import pandas as pd

from terminal_api import register_terminal
from terminal_funds import FundFlowService, parse_stock, missing


def payload(values):
    return {'data': {'code': '600519', 'klines': [
        f'2026-11-{i+2:02d},{n},0,0,{n*.4},{n*.6},1.5,0,0,0,0'
        for i, n in enumerate(values)]}}


class FundTests(unittest.TestCase):
    def test_signed_yuan_history_and_streak(self):
        result = parse_stock(payload([10, -20, 0, 40, 50]), '600519')
        self.assertEqual(result['net'], 50)
        self.assertEqual(result['five_day_net'], 80)
        self.assertEqual(result['positive_days'], 2)
        self.assertEqual(result['large'] + result['super_large'], result['net'])
        self.assertEqual(result['date'], '2026-11-06')
        self.assertEqual(parse_stock(payload([-1, -2]), '600519')['positive_days'], 0)
        self.assertIsNone(parse_stock(payload([1, 2]), '600519')['five_day_net'])

    def test_wrong_symbol_and_invalid_data_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_stock(payload([1]), '000001')
        with self.assertRaises(ValueError):
            parse_stock(payload([float('nan')]), '600519')
        with self.assertRaises(ValueError):
            parse_stock({'data': {'code': '600519', 'klines': ['bad,1,0,0,0,0,0,0,0,0,0']}}, '600519')

    def test_refresh_cache_preserves_stale_without_changing_date(self):
        service = FundFlowService()
        with patch('terminal_funds.time.monotonic', return_value=1000) as clock, \
             patch.object(service, '_json', side_effect=[payload([10]), RuntimeError(), payload([-20])]) as source:
            load=lambda:parse_stock(service._json('',{}),'600519')
            first = service._cached('test-stock',load)
            self.assertEqual(service._cached('test-stock',load), first)
            self.assertEqual(source.call_count, 1)
            clock.return_value = 1030
            stale = service._cached('test-stock',load)
            self.assertEqual(stale['status'], 'stale')
            self.assertEqual(stale['retrieved_at'], first['retrieved_at'])
            self.assertEqual(stale['net'], 10)
            clock.return_value = 1060
            self.assertEqual(service._cached('test-stock',load)['net'], -20)

    def test_initial_failure_is_unknown_not_zero(self):
        service = FundFlowService()
        with patch.object(service, '_json', side_effect=RuntimeError()):
            self.assertIsNone(service.stock('600519')['net'])

    def test_http_failure_reason_is_preserved_without_request_secrets(self):
        import requests
        service=FundFlowService()
        response=requests.Response();response.status_code=502
        error=requests.HTTPError('secret-bearing-url', response=response)
        with patch.object(service, '_json', side_effect=error):
            result=service.stock('600519')
        self.assertIn('502',result['reason'])
        self.assertNotIn('secret',result['reason'])

    def test_market_uses_unified_mainline_service_not_cross_taxonomy_matching(self):
        app = Flask('fund_market')
        register_terminal(app, lambda: self.fail('mainlines must not use THS universe'),
                          lambda _: self.fail('mainlines must not use THS members'))
        expected = {'groups': [], 'errors': [], 'version': 'mainline-funds-v1.0'}
        with patch('terminal_api.MainlineService.build', return_value=expected) as build:
            result = app.test_client().get('/api/terminal/market').get_json()['data']
        self.assertEqual(result, expected)
        build.assert_called_once()


if __name__ == '__main__':
    unittest.main()
