import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools.limit_entry_validation import (LocalEvidence, audit, entry_evidence, freeze,
                                          limit_price, select_signals)


def candidate(**overrides):
    row = dict(code='600001', name='fixture', date='2026-11-13', stage=2,
               first_word=False, stage_word=False, stage_volprev=1.5, stage_floatcap=120,
               pre_pos60=20, pre_ret20=-5, pre_range10=18, first_break20=False)
    row.update(overrides)
    return row


def daily(**overrides):
    row = dict(date='2026-11-16', open='10', high='11', low='10', close='11',
               preclose='10', volume='100000', tradestatus='1', isST='0')
    row.update(overrides)
    return row


class FrozenSignalTests(unittest.TestCase):
    def test_future_outcomes_and_buy_day_fields_cannot_change_selection(self):
        study = dict(entries=[candidate()], posts=[])
        changed = copy.deepcopy(study)
        changed['entries'][0].update(after1_close=-99, after4_close=999,
                                     seal=False, buy_word=True, buy_gap=100, shape='future winner')
        self.assertEqual(select_signals(study), select_signals(changed))
        self.assertNotIn('after1_close', select_signals(changed)[0]['features'])

    def test_boundaries_and_first_word_are_not_silently_reclassified(self):
        for overrides in [dict(stage_volprev=1), dict(stage_volprev=2), dict(first_word=True),
                          dict(stage_word=True), dict(stage_floatcap=99.99), dict(stage_floatcap=True)]:
            self.assertEqual(select_signals(dict(entries=[candidate(**overrides)], posts=[])), [])

    def test_first_yin_is_not_first_break_and_controlled_volume_is_not_strict_shrink(self):
        post = dict(code='600001', date='2026-11-13', kind='首次断板', height=2,
                    vol5=1.4, ma3=True, pos=.5, downword=False)
        self.assertEqual(select_signals(dict(posts=[post]))[0]['strategy'], 'controlled_break')
        post['kind'] = '首次阴线'
        self.assertEqual(select_signals(dict(posts=[post])), [])

    def test_repair_observation_respects_platform_priority(self):
        r = candidate(stage=1)
        self.assertEqual(select_signals(dict(entries=[r]))[0]['role'], 'observation_only')
        r.update(pre_range10=15, first_break20=True)
        self.assertEqual(select_signals(dict(entries=[r])), [])

    def test_incomplete_or_duplicate_inputs_are_not_frozen_as_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); (p/'study.json').write_text(json.dumps(dict(universe=2, complete=1)))
            (p/'calendar.json').write_text('[]')
            with self.assertRaisesRegex(ValueError, 'Incomplete'):
                freeze(p/'study.json', p/'calendar.json', p/'run')
            self.assertFalse((p/'run').exists())
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            select_signals(dict(entries=[candidate(), candidate()]))

    def test_freeze_is_immutable_and_audit_uses_market_calendar(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            study = dict(universe=1, complete=1, start='2026-11-13', end='2026-11-16',
                         entries=[candidate()], posts=[])
            (p/'study.json').write_text(json.dumps(study)); (p/'cal.json').write_text(json.dumps(['2026-11-13', '2026-11-16']))
            freeze(p/'study.json', p/'cal.json', p/'run')
            with self.assertRaisesRegex(ValueError, 'already exists'):
                freeze(p/'study.json', p/'cal.json', p/'run')
            (p/'daily').mkdir()
            payload = dict(source='fixture', retrieved_at='2026-11-16T15:01:00+08:00',
                           rows=[daily(open='11', low='11')])
            (p/'daily/600001.json').write_text(json.dumps(payload))
            result = audit(p/'run', [p/'daily'], p/'ticks')
            self.assertEqual(result['results'][0]['entry_day'], '2026-11-16')
            self.assertEqual(result['counts'], {'excluded_one_word': 1})
            self.assertEqual(result['actual_fills'], 0)
            self.assertEqual(result['pnl_status'], 'not_calculated')
            original = (p/'run/audit.json').read_bytes()
            repeated = audit(p/'run', [p/'daily'], p/'ticks')
            self.assertNotEqual(repeated['snapshot_file'], 'audit.json')
            self.assertEqual((p/'run/audit.json').read_bytes(), original)
            self.assertTrue((p/'run'/repeated['snapshot_file']).exists())
            manifest = json.loads((p/'run/manifest.json').read_text())
            manifest['protocol']['rules']['capacity_two']['entry'] = 'changed'
            (p/'run/manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'protocol changed'):
                audit(p/'run', [], p/'ticks')


class ExecutionEvidenceTests(unittest.TestCase):
    def test_touch_and_later_prints_at_cap_do_not_prove_fill(self):
        checked = dict(status='verified', ticks=[dict(time='09:40', price=11), dict(time='09:41', price=11)])
        result = entry_evidence('capacity_two', daily(), checked)
        self.assertEqual(result['status'], 'queue_fill_unconfirmed')
        self.assertFalse(result['fill_confirmed'])

    def test_same_minute_low_is_not_treated_as_later_queue_evidence(self):
        checked = dict(status='verified', ticks=[dict(time='09:40', price=11), dict(time='09:40', price=10.8)])
        self.assertEqual(entry_evidence('capacity_two', daily(), checked)['status'], 'queue_fill_unconfirmed')
        checked['ticks'].append(dict(time='09:41', price=10.8))
        result = entry_evidence('capacity_two', daily(), checked)
        self.assertEqual(result['status'], 'price_through_observed')
        self.assertFalse(result['fill_confirmed'])

    def test_opening_requires_actual_opening_print_and_excludes_limits(self):
        checked = dict(status='verified', ticks=[dict(time='09:31', price=10)])
        self.assertEqual(entry_evidence('controlled_break', daily(), checked)['status'], 'opening_trade_unverified')
        checked['ticks'].insert(0, dict(time='09:25', price=10))
        self.assertEqual(entry_evidence('controlled_break', daily(), checked)['status'], 'opening_trade_observed')
        self.assertEqual(entry_evidence('controlled_break', daily(open='11'), checked)['status'], 'excluded_limit_open')

    def test_decimal_limit_prices_and_invalid_daily_data(self):
        self.assertEqual(limit_price('10.15', '.1'), 11.17)
        with self.assertRaisesRegex(ValueError, 'nonfinite'):
            entry_evidence('capacity_two', daily(open='NaN'), dict(status='missing'))

    def test_tick_units_and_provenance_are_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); (p/'daily').mkdir(); (p/'ticks').mkdir()
            raw = daily(); raw['low'] = '10'
            payload = dict(day='2026-11-16', source='fixture', retrieved_at='2026-11-16T15:01:00+08:00',
                           rows=[dict(time='09:25', price=10, vol=500), dict(time='15:00', price=11, vol=500)])
            path = p/'ticks/600001-2026-11-16.json'; path.write_text(json.dumps(payload))
            store = LocalEvidence([p/'daily'], p/'ticks')
            self.assertEqual(store.ticks('600001', '2026-11-16', raw)['status'], 'verified')
            payload['rows'][0]['vol'] = 1; path.write_text(json.dumps(payload))
            self.assertEqual(store.ticks('600001', '2026-11-16', raw)['status'], 'invalid')
            payload['day'] = '2026-11-13'; path.write_text(json.dumps(payload))
            self.assertEqual(store.ticks('600001', '2026-11-16', raw)['status'], 'invalid')


if __name__ == '__main__':
    unittest.main()
