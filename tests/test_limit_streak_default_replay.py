import unittest

from tools.limit_streak_default_replay import replay


DATES = ['2026-11-11', '2026-11-12', '2026-11-13']


def bar(day, opening, close, preclose, low=None):
    return dict(date=day, open=str(opening), high=str(max(opening, close)),
                low=str(min(opening, close) if low is None else low), close=str(close),
                preclose=str(preclose), volume='1000000',
                tradestatus='1', isST='0')


def ticks(day, opening, first, second, close):
    return dict(source='fixture', day=day, rows=[
        dict(time='09:25', price=opening, vol=500),
        dict(time='09:30', price=first, vol=5000),
        dict(time='09:31', price=second, vol=4000),
        dict(time='15:00', price=close, vol=500),
    ])


class FixtureSource:
    def __init__(self, payloads):
        self.payloads = payloads

    def ticks(self, code, day):
        return self.payloads[day]


class DefaultReplayTests(unittest.TestCase):
    def setUp(self):
        self.signals = {DATES[0]: [dict(code='600001', name='Fixture', stage=1,
                                         priority=1, observation_tag='high',
                                         pre_first_position_60_pct=80)]}
        self.histories = {'600001': {
            DATES[1]: bar(DATES[1], 10, 10, 10, low=9.4),
            DATES[2]: bar(DATES[2], 9.4, 9.2, 10),
        }}
        self.source = FixtureSource({
            DATES[1]: ticks(DATES[1], 10, 9.4, 9.4, 10),
            DATES[2]: ticks(DATES[2], 9.4, 9.4, 9.3, 9.2),
        })

    def test_buy_day_stop_waits_until_next_session(self):
        result = replay(DATES, self.signals, self.histories,
                        self.source, 'A', None)
        self.assertEqual(result['status'], 'complete_price_scenario')
        self.assertEqual((result['buys'], result['sells']), (1, 1))
        self.assertEqual(result['trades'][-1]['date'], DATES[2])
        self.assertEqual(result['trades'][-1]['time'], '09:31')
        self.assertLess(result['return_pct'], 0)

    def test_next_day_stop_waits_until_following_minute(self):
        self.source.payloads[DATES[1]] = ticks(DATES[1], 10, 10, 10, 10)
        self.histories['600001'][DATES[1]]['low'] = '10'
        result = replay(DATES, self.signals, self.histories,
                        self.source, 'A', None)
        self.assertEqual(result['trades'][-1]['time'], '09:32')

    def test_missing_exit_day_ticks_cannot_publish_return(self):
        del self.source.payloads[DATES[2]]
        result = replay(DATES, self.signals, self.histories,
                        self.source, 'A', None)
        self.assertEqual(result['status'], 'partial_missing_evidence')
        self.assertIsNone(result['return_pct'])
        self.assertEqual(result['errors'][0]['day'], DATES[2])

    def test_inconsistent_tick_extreme_cannot_publish_return(self):
        self.histories['600001'][DATES[2]]['high'] = '9.8'
        result = replay(DATES, self.signals, self.histories,
                        self.source, 'A', None)
        self.assertEqual(result['status'], 'partial_missing_evidence')
        self.assertIsNone(result['return_pct'])
        self.assertIn('high/low', result['errors'][0]['reason'])


if __name__ == '__main__':
    unittest.main()
