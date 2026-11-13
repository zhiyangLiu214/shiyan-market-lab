import unittest

import pandas as pd

from terminal_engine import (analyze, demo_history, demo_minutes, intraday_analysis,
                             pressure_candidates, pressure_plan)


class PressureTests(unittest.TestCase):
    def test_nearest_zones_merge_and_actual_risk_reward(self):
        levels = [dict(price=v, basis='历史前高', date='2026-11-09')
                  for v in [12, 9, 10.32, 10.30, 11]]
        p = pressure_plan(10, 9.5, levels, '参考价', '2026-11-10', '日线')
        self.assertEqual(p['target1'], 10.3)
        self.assertEqual(p['target2'], 11)
        self.assertEqual(p['levels'][0]['high'], 10.32)
        self.assertEqual(p['rr'], .6)
        self.assertEqual(p['rr2'], 2)
        self.assertEqual(p['upside'], 3)
        self.assertEqual(p['assessment'], '首压力空间小于止损距离')
        self.assertEqual(p['rr_target1'], 11)

    def test_breakout_has_no_fabricated_pressure_or_ratio(self):
        p = pressure_plan(10, 9.5, [dict(price=10, basis='旧高', date='2026-11-09')],
                          '参考价', '2026-11-10', '日线')
        self.assertIsNone(p['target1'])
        self.assertIsNone(p['target2'])
        self.assertIsNone(p['rr'])
        self.assertEqual(p['levels'], [])
        self.assertEqual(p['rr_target1'], 11)
        self.assertEqual(p['assessment'], '暂无明确上方压力位')

    def test_only_one_pressure_and_inadequate_ratio(self):
        p = pressure_plan(10, 9, [dict(price=11.2, basis='旧高', date='2026-11-09')],
                          '参考价', '2026-11-10', '日线')
        self.assertEqual(p['rr'], 1.2)
        self.assertIsNone(p['target2'])
        self.assertIsNone(p['rr2'])
        self.assertEqual(p['assessment'], '首压力盈亏比低于 1.5')

    def test_daily_excludes_current_bar_high(self):
        frame = demo_history('000001')
        before = analyze(frame, '000001')['plan']
        frame.loc[frame.index[-1], 'high'] = 999999
        self.assertEqual(analyze(frame, '000001')['plan'], before)

    def test_local_peak_requires_two_confirmation_bars(self):
        frame = pd.DataFrame({'date': pd.date_range('2026-11-01', periods=7),
                              'high': [1, 2, 5, 2, 1, 7, 1]})
        levels = pressure_candidates(frame, '日线')
        self.assertEqual([p['price'] for p in levels], [5])

    def test_minute_targets_do_not_use_later_highs(self):
        frame = demo_minutes('000001')
        original = intraday_analysis(frame, '000001', True)
        first = original['signals'][0]['time']
        prefix = frame[pd.to_datetime(frame['时间']).dt.strftime('%H:%M') <= first].copy()
        at_signal = intraday_analysis(prefix, '000001', True)
        self.assertEqual(original['plan'], at_signal['plan'])
        changed = frame.copy()
        later = pd.to_datetime(changed['时间']).dt.strftime('%H:%M') > first
        changed.loc[later, '最高'] = 999999
        self.assertEqual(intraday_analysis(changed, '000001', True)['plan'], original['plan'])


if __name__ == '__main__':
    unittest.main()
