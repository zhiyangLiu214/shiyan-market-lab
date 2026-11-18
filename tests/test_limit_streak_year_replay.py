import json
import tempfile
import unittest
from pathlib import Path

from tools.limit_streak_year_replay import (continuation_paths, matches_cohort,
                                            simulate, verified_dividend)


DATES = ['2026-11-11', '2026-11-12', '2026-11-13']


def bar(day, opening, high, close, preclose, *, status='1'):
    return dict(date=day, open=str(opening), high=str(high), low=str(min(opening, close)),
                close=str(close), preclose=str(preclose), tradestatus=status,
                isST='0', volume='1000000')


class YearReplayTests(unittest.TestCase):
    def setUp(self):
        self.signals = {DATES[0]: [dict(code='600001', name='Fixture', stage=1,
                                         priority=2, observation_tag='test',
                                         first_board_turnover_pct=10,
                                         signal_turnover_pct=10,
                                         estimated_float_cap_100m=50,
                                         pre_first_position_60_pct=80,
                                         second_to_first_volume=None)]}
        self.histories = {'600001': {
            DATES[1]: bar(DATES[1], 10, 11, 11, 10),
            DATES[2]: bar(DATES[2], 12, 12, 12, 11),
        }}

    def test_open_buy_third_day_sell_reconciles_fees_and_cash(self):
        result = simulate(DATES, self.signals, self.histories, 1)
        self.assertEqual(result['status'], 'complete_price_scenario')
        self.assertEqual((result['buys'], result['sells']), (1, 1))
        buy, sell = result['trades']
        self.assertEqual(buy['qty'], 900)
        self.assertTrue(sell['third_session'])
        self.assertEqual(result['completed_chain_count'], 1)
        self.assertAlmostEqual(result['final_nav'], 100000 + sell['pnl'], places=2)
        self.assertEqual(result['total_fees'], round(buy['fees'] + sell['fees'], 2))

    def test_suspension_is_marked_and_unsold_at_period_end(self):
        self.histories['600001'][DATES[2]] = bar(DATES[2], 11, 11, 11, 11, status='0')
        result = simulate(DATES, self.signals, self.histories, 1)
        self.assertEqual(result['status'], 'complete_marked_open_positions')
        self.assertEqual(result['sells'], 0)
        self.assertEqual(result['skipped']['sell_suspended'], 1)
        self.assertEqual(result['unresolved'][0]['code'], '600001')

    def test_preset_third_day_profit_order_can_fill_before_weak_close(self):
        self.histories['600001'][DATES[2]] = bar(DATES[2], 10, 10.3, 9.95, 11)
        result = simulate(DATES, self.signals, self.histories, 1, exit_mode='target2')
        self.assertEqual(result['trades'][-1]['exit_reason'], 'target_crossed_daily_high')
        self.assertEqual(result['trades'][-1]['price'], 10.2)
        self.assertGreater(result['trades'][-1]['pnl'], 0)

    def test_missing_holding_bar_does_not_publish_partial_year_return(self):
        del self.histories['600001'][DATES[2]]
        result = simulate(DATES, self.signals, self.histories, 1)
        self.assertEqual(result['status'], 'partial_price_scenario')
        self.assertIsNone(result['return_pct'])

    def test_priority_filter_applies_before_buying(self):
        self.signals[DATES[0]][0]['priority'] = 1
        result = simulate(DATES, self.signals, self.histories, 1, min_priority=2)
        self.assertEqual(result['buys'], 0)
        self.assertEqual(result['return_pct'], 0)

    def test_dividend_needs_record_date_and_no_share_distribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '600001.json'
            row = dict(EX_DIVIDEND_DATE='2026-11-13 00:00:00',
                       EQUITY_RECORD_DATE='2026-11-12 00:00:00',
                       PRETAX_BONUS_RMB=.7, BONUS_IT_RATIO=None, BONUS_RATIO=None)
            payload = dict(source='fixture', retrieved_at='2026-11-16', rows=[row])
            path.write_text(json.dumps(payload))
            event = verified_dividend(tmp, '600001', DATES[2], DATES[1], 10, 9.93)
            self.assertAlmostEqual(event['per_share'], .07)
            payload['rows'][0]['BONUS_IT_RATIO'] = 4
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, 'Share entitlement'):
                verified_dividend(tmp, '600001', DATES[2], DATES[1], 10, 9.93)

    def test_touch_without_seal_still_tracks_third_day_path(self):
        self.histories['600001'][DATES[1]] = bar(DATES[1], 10, 11, 10.5, 10)
        path = continuation_paths(DATES, self.signals, self.histories)['一进二:priority2']
        self.assertEqual(path['counts']['entry_day_touch'], 1)
        self.assertNotIn('entry_day_sealed', path['counts'])
        self.assertEqual(path['counts']['touch_then_third_open_above_buy'], 1)
        self.assertAlmostEqual(path['touch_third_open_return_median_pct'], 20)
        self.assertIsNone(path['sealed_third_open_return_median_pct'])
        tagged = continuation_paths(DATES, self.signals, self.histories)['一进二:tag=test']
        self.assertEqual(tagged['counts'], path['counts'])

    def test_later_board_is_outcome_only(self):
        before = continuation_paths(DATES, self.signals, self.histories)['一进二:all']
        self.histories['600001'][DATES[2]] = bar(DATES[2], 12, 12.1, 12.1, 11)
        after = continuation_paths(DATES, self.signals, self.histories)['一进二:all']
        self.assertEqual(before['counts']['signal'], after['counts']['signal'])
        self.assertEqual(before['counts']['entry_day_touch'], after['counts']['entry_day_touch'])
        self.assertEqual(after['counts']['touch_then_next_board_sealed'], 1)

    def test_formula_cohorts_use_only_signal_day_features(self):
        first = dict(self.signals[DATES[0]][0], priority=1)
        self.assertTrue(matches_cohort(first, 'A'))
        self.assertFalse(matches_cohort(first, 'B'))
        second = dict(first, stage=2, priority=2, first_board_one_word=False,
                      second_board_one_word=False, second_to_first_volume=.8,
                      estimated_float_cap_100m=80)
        self.assertTrue(matches_cohort(second, 'B'))
        self.assertFalse(matches_cohort(second, 'C'))
        second.update(priority=0, second_to_first_volume=1.5,
                      estimated_float_cap_100m=120)
        self.assertTrue(matches_cohort(second, 'C'))
        second['second_board_one_word'] = True
        self.assertFalse(matches_cohort(second, 'C'))


if __name__ == '__main__':
    unittest.main()
