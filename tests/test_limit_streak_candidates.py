import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tools.limit_streak_candidates import limit_price, screen


def fixture_rows(second=False):
    days = [(date(2026, 8, 1) + timedelta(days=i)).isoformat() for i in range(65)]
    rows = []
    for day in days:
        rows.append(dict(date=day, open='10', high='10.2', low='9.8', close='10',
                         preclose='10', volume='1000000', turn='5',
                         tradestatus='1', isST='0'))
    if second:
        rows[-2].update(open='10', high='11', low='10', close='11')
        rows[-1].update(open='11', high='12.1', low='11', close='12.1',
                        preclose='11', volume='800000')
    else:
        rows[-1].update(open='10', high='11', low='10', close='11')
    return days, rows


class CandidateScreenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'daily1').mkdir()
        (self.root / 'daily2').mkdir()
        self.days, first = fixture_rows()
        _, second = fixture_rows(second=True)
        self.write('daily1', '600001', first)
        self.write('daily2', '000002', second)
        (self.root / 'calendar.json').write_text(json.dumps(self.days))
        (self.root / 'roster.json').write_text(json.dumps([
            dict(code='600001', name='First'), dict(code='000002', name='Second')]))

    def write(self, folder, code, rows, retrieved_at='2026-10-05T15:30:00+08:00'):
        payload = dict(source='fixture', retrieved_at=retrieved_at, rows=rows)
        (self.root / folder / (code + '.json')).write_text(json.dumps(payload))

    def run_screen(self, roster=True):
        return screen([self.root / 'daily1', self.root / 'daily2'],
                      self.root / 'calendar.json', self.days[-1],
                      roster_path=self.root / 'roster.json' if roster else None)

    def test_first_and_second_board_are_separate_and_ranked(self):
        result = self.run_screen()
        self.assertEqual(result['status'], 'complete')
        first, second = result['groups']['一进二'], result['groups']['二进三']
        self.assertEqual([r['code'] for r in first], ['600001'])
        self.assertEqual([r['code'] for r in second], ['000002'])
        self.assertEqual(second[0]['second_to_first_volume'], .8)
        self.assertEqual(second[0]['name'], 'Second')
        self.assertEqual(second[0]['first_board_date'], self.days[-2])
        self.assertEqual(limit_price('10.15', .1), 11.17)

    def test_future_rows_cannot_change_candidate_list(self):
        original = self.run_screen()['groups']
        path = self.root / 'daily2/000002.json'
        payload = json.loads(path.read_text())
        future = copy.deepcopy(payload['rows'][-1])
        future.update(date='2026-12-01', open='50', high='55', low='49',
                      close='55', preclose='50')
        payload['rows'].append(future)
        path.write_text(json.dumps(payload))
        changed = self.run_screen()['groups']
        for group in original:
            for before, after in zip(original[group], changed[group]):
                before.pop('daily_sha256')
                after.pop('daily_sha256')
        self.assertEqual(original, changed)

    def test_break_between_limit_days_starts_a_new_first_board(self):
        path = self.root / 'daily1/600001.json'
        payload = json.loads(path.read_text())
        payload['rows'][-3].update(open='10', high='11', low='10', close='11')
        payload['rows'][-2].update(open='10', high='10.4', low='9.9',
                                   close='10', preclose='11')
        path.write_text(json.dumps(payload))
        result = self.run_screen()
        self.assertEqual([row['code'] for row in result['groups']['一进二']], ['600001'])

    def test_third_board_is_not_a_second_board_candidate(self):
        path = self.root / 'daily2/000002.json'
        payload = json.loads(path.read_text())
        payload['rows'][-3].update(open='10', high='11', low='10', close='11')
        payload['rows'][-2].update(open='11', high='12.1', low='11', close='12.1',
                                   preclose='11')
        payload['rows'][-1].update(open='12.1', high='13.31', low='12.1',
                                   close='13.31', preclose='12.1')
        path.write_text(json.dumps(payload))
        self.assertEqual(self.run_screen()['groups']['二进三'], [])

    def test_missing_roster_file_and_stale_data_are_visible(self):
        (self.root / 'daily2/000002.json').unlink()
        result = self.run_screen()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['skipped']['missing_daily_file'], 1)
        self.assertEqual(self.run_screen(roster=False)['status'], 'scope_unverified')
        _, rows = fixture_rows()
        self.write('daily1', '600001', rows, '2026-08-01T10:00:00+08:00')
        result = self.run_screen()
        self.assertNotIn('600001', [r['code'] for r in result['groups']['一进二']])
        self.assertTrue(any('before signal-day close' in key for key in result['skipped']))


if __name__ == '__main__':
    unittest.main()
