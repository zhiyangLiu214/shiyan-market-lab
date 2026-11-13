import unittest
import pandas as pd
from terminal_engine import demo_minutes, intraday_analysis


class MinuteCausalTests(unittest.TestCase):
    def test_appending_minutes_does_not_rewrite_confirmed_events(self):
        frame = demo_minutes('000001')
        prefix = intraday_analysis(frame.iloc[:70], '000001', True)
        full = intraday_analysis(frame, '000001', True)
        cutoff = prefix['points'][-1]['time']
        self.assertEqual(prefix['signals'], [s for s in full['signals'] if s['time'] <= cutoff])
        self.assertTrue(all(s['time'] == s['confirmed_at'] for s in full['signals']))

    def test_same_minute_can_have_volume_and_cross_events(self):
        times = pd.date_range('2026-11-11 09:30', periods=8, freq='min')
        prices = [10, 10, 10, 10, 9.99, 9.98, 10.1, 10.12]
        volumes = [100]*6 + [400, 100]
        frame = pd.DataFrame({'时间': times, '收盘': prices, '最低': prices,
                              '成交量': volumes,
                              '成交额': [p*v*100 for p, v in zip(prices, volumes)]})
        signals = intraday_analysis(frame, '000001')['signals']
        kinds = {s['kind'] for s in signals if s['time'] == '09:36'}
        self.assertTrue({'放量上攻', '上穿均价', '弱转强'} <= kinds)

    def test_opening_volume_is_not_auction_and_needs_complete_start(self):
        frame = demo_minutes('000001')
        complete = intraday_analysis(frame, '000001', True)
        missing = intraday_analysis(frame.iloc[6:], '000001', True)
        self.assertTrue(complete['opening']['available'])
        self.assertFalse(missing['opening']['available'])
        self.assertIsNone(missing['opening']['amount'])
        self.assertIn('非集合竞价', complete['opening']['label'])


if __name__ == '__main__':
    unittest.main()
