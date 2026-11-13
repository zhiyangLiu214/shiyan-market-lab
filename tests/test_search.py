import json
import unittest
from unittest.mock import patch

from app import app
from terminal_search import StockSearch, parse_suggestions


def suggestions():
    return 'v_hint=' + json.dumps('sh~600519~贵州茅台~gzmt~GP-A^sz~000001~平安银行~payh~GP-A^hk~02318~中国平安~zgpa~GP^sh~508036~测试基金~cs~FJ')


class SearchTests(unittest.TestCase):
    def test_parse_unicode_and_filter_non_a_shares(self):
        items = parse_suggestions(suggestions())
        self.assertEqual([(s['code'], s['name']) for s in items], [('600519', '贵州茅台'), ('000001', '平安银行')])
        self.assertEqual(parse_suggestions('v_hint="";'), [])
        with self.assertRaises(ValueError):
            parse_suggestions('v_hint="";alert(1)')

    def test_search_cache_query_encoding_and_exact_name_first(self):
        service = StockSearch()
        with patch('terminal_search.requests.get') as source:
            source.return_value.content = suggestions().encode('gbk')
            items = service.search('平安银行')
            self.assertEqual(items[0]['code'], '000001')
            self.assertEqual(service.search(' 平安银行 '), items)
            self.assertEqual(source.call_count, 1)
            self.assertEqual(source.call_args.kwargs['params']['q'], '平安银行')
            self.assertEqual(source.call_args.kwargs['timeout'], 5)

    def test_demo_names_and_codes_do_not_request_live_data(self):
        with patch('terminal_search.requests.get', side_effect=AssertionError('no live data')):
            service = StockSearch()
            item = service.search('光域互联', demo=True)[0]
            self.assertEqual(item['name'], '示例·光域互联')
            self.assertEqual(service.search('示例·光域互联', demo=True)[0], item)
            self.assertEqual(service.search(item['code'], demo=True)[0], item)
            self.assertEqual(service.search('', demo=True), [])

    def test_endpoint_empty_validation_and_failure(self):
        client = app.test_client()
        self.assertEqual(client.get('/api/terminal/search?q=').get_json()['data']['items'], [])
        self.assertEqual(client.get('/api/terminal/search?q=' + 'a'*41).status_code, 400)
        with patch('terminal_api.StockSearch.search', side_effect=RuntimeError('offline')):
            response = client.get('/api/terminal/search?q=茅台')
            self.assertEqual(response.status_code, 503)
            self.assertIn('六位股票代码', response.get_json()['error'])


if __name__ == '__main__':
    unittest.main()
