import copy
import unittest
from datetime import datetime, timedelta

from tools.research_bottom_reversal import (
    shape, strengthening, path_evidence, recovery_leg_evidence,
    late_partial, optimistic_late_shape,
)


class BottomReversalTests(unittest.TestCase):
    def bars(self):
        prior = [dict(open=10.4, close=10.3, high=10.5, low=10.1, volume=100) for _ in range(10)]
        return prior+[dict(open=10., close=10.2, high=10.3, low=9.5, volume=150)]

    def ticks(self):
        times = []
        for hour, count in [(9, 120), (13, 90)]:
            base = datetime(2000, 1, 1, hour, 30 if hour == 9 else 0)
            times += [(base+timedelta(minutes=i)).strftime('%H:%M') for i in range(count)]
        rows = []
        for i, t in enumerate(times):
            price = 10-i*.5/60 if i <= 60 else 9.5+(i-60)*.7/149
            rows.append(dict(time=t, price=price, vol=2 if i <= 60 else 4))
        partial = dict(volume=sum(r['vol'] for r in rows), high=10.2, low=9.5, close=10.2)
        return dict(rows=rows), partial

    def test_five_day_low_and_wick_requirements(self):
        bars = self.bars()
        self.assertTrue(shape(bars))
        changed = copy.deepcopy(bars)
        changed[-5]['low'] = 9.4
        self.assertFalse(shape(changed))
        changed[-5]['low'] = 9.5
        self.assertTrue(shape(changed))
        changed[-1]['high'] = 11.
        self.assertFalse(shape(changed))

    def test_low_before_five_day_window_does_not_reject_signal(self):
        bars = self.bars()
        bars[-6]['low'] = 9.0
        self.assertTrue(shape(bars[-5:]))
        self.assertTrue(shape(bars))
        bars[-5]['low'] = 9.4
        self.assertFalse(shape(bars))

    def test_strengthening_uses_prior_full_shape_and_same_time_volume(self):
        bars = self.bars()+[dict(open=10.2, close=10.6, high=10.7, low=10.1, volume=200)]
        result = strengthening(bars, 200, [180, 150])
        self.assertTrue(result['base'])
        self.assertTrue(result['close_break'])
        self.assertFalse(strengthening(bars, 170, [180, 150])['base'])
        bars[-1]['close'] = 10.25
        result = strengthening(bars, 200, [180, 150])
        self.assertTrue(result['base'])
        self.assertFalse(result['close_break'])

    def test_path_sequence_and_directional_volume(self):
        ticks, partial = self.ticks()
        result = path_evidence(ticks, 10., partial)
        self.assertTrue(result['volume_confirmed'])
        self.assertGreater(result['up_volume'], result['down_volume'])
        self.assertEqual(result['observed_minutes'], 210)
        self.assertEqual(result['low_time'], '10:30')

    def test_cutoff_excludes_future_trades(self):
        ticks, partial = self.ticks()
        before = path_evidence(ticks, 10., partial)
        ticks['rows'] += [dict(time='14:30', price=1., vol=100000),
                          dict(time='15:00', price=100., vol=100000)]
        self.assertEqual(before, path_evidence(ticks, 10., partial))

    def test_morning_close_endpoint_merges_into_final_morning_minute(self):
        ticks, partial = self.ticks()
        previous = path_evidence(ticks, 10., partial)
        endpoint = dict(time='11:30', price=ticks['rows'][119]['price'], vol=10)
        ticks['rows'].insert(120, endpoint)
        partial['volume'] += 10
        result = path_evidence(ticks, 10., partial)
        self.assertEqual(result['observed_minutes'], 210)
        self.assertTrue(result['volume_confirmed'])
        self.assertAlmostEqual(result['up_volume'], previous['up_volume']+10)

    def test_disagreement_and_missing_minutes_rejected(self):
        ticks, partial = self.ticks()
        with self.assertRaisesRegex(ValueError, '成交量'):
            path_evidence(ticks, 10., dict(partial, volume=1))
        with self.assertRaisesRegex(ValueError, '高低收'):
            path_evidence(ticks, 10., dict(partial, low=9.))
        ticks['rows'] = ticks['rows'][::2]
        partial['volume'] = sum(r['vol'] for r in ticks['rows'])
        partial['close'] = ticks['rows'][-1]['price']
        with self.assertRaisesRegex(ValueError, '分钟覆盖'):
            path_evidence(ticks, 10., partial)

    def test_post_red_volume_cannot_qualify_recovery_leg(self):
        ticks, partial = self.ticks()
        for row in ticks['rows']:
            row['vol'] = 1000 if row['price'] >= 10.05 else 1
        partial['volume'] = sum(r['vol'] for r in ticks['rows'])
        result = recovery_leg_evidence(ticks, 10., 10.05, partial)
        self.assertFalse(result['volume_confirmed'])
        self.assertGreater(result['up_volume'], result['down_volume'])
        self.assertGreater(result['post_crossing_volume'], result['recovery_volume'])

    def test_recovery_before_red_is_compared_separately(self):
        ticks, partial = self.ticks()
        result = recovery_leg_evidence(ticks, 10., 10.05, partial)
        self.assertTrue(result['volume_confirmed'])
        self.assertGreater(result['recovery_mean_volume_ratio'], 1)
        cross = result['recovery_crossing_minute']
        expected = sum(r['vol'] for r in ticks['rows'] if result['low_time'] < r['time'] < cross)
        self.assertEqual(result['recovery_volume'], expected)

    def test_late_partial_does_not_use_1450_or_closing_trades(self):
        ticks, checkpoint = self.ticks()
        for i in range(20):
            ticks['rows'].append(dict(time=f'14:{30+i:02d}', price=10.2+i*.001, vol=4))
        before = late_partial(ticks, 10., checkpoint)
        ticks['rows'] += [dict(time='14:50', price=1., vol=100000),
                          dict(time='15:00', price=100., vol=100000)]
        self.assertEqual(before, late_partial(ticks, 10., checkpoint))
        self.assertAlmostEqual(before['close'], 10.219)
        ticks['rows'] = [r for r in ticks['rows'] if r['time'] != '14:49']
        with self.assertRaisesRegex(ValueError, '覆盖不足'):
            late_partial(ticks, 10., checkpoint)

    def test_late_envelope_preserves_viable_shapes(self):
        history = self.bars()[:-1]
        for low in [9.4, 9.5, 9.6]:
            for high in [10.2, 10.3, 10.5]:
                for close in [10.1, 10.2, 10.3]:
                    if close > high:
                        continue
                    bar = dict(open=10., high=high, low=low, close=close)
                    if shape(history+[bar]):
                        self.assertTrue(optimistic_late_shape(
                            history, 10., min(10.1, high), low-.1, high+.1))
                        self.assertTrue(optimistic_late_shape(
                            history, 10., min(10.1, high), low-.1, 20.))


if __name__ == '__main__':
    unittest.main()
