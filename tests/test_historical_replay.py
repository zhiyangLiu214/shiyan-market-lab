import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_backtest import selection_bars, minute_points, replay_strategy, BacktestStore
from terminal_formula import evaluate_formula
from terminal_formula_prefilter import might_match
from terminal_holding import holding_deadline
from terminal_paper import PaperLedger
from terminal_strategy_sim import checked_config


class HoldingDeadlineTests(unittest.TestCase):
    def quote(self, time='14:54'):
        dates=['2026-09-29','2026-09-30','2026-10-09','2026-10-12','2026-10-13','2026-10-14']
        return dict(date=dates[-1],as_of=dates[-1]+' '+time,sessions=dates,price=10.,prev_close=10.,
                    daily={'bars':[dict(date=day,close=10.) for day in dates[:-1]]})

    def position(self):
        return dict(code='600000',lots=[dict(day='2026-09-30',qty=100)],
                    plan={'_opened_at':'2026-09-29 14:55:00'})

    def test_actual_fill_day_and_holidays(self):
        q=self.quote()
        self.assertEqual(holding_deadline(self.position(),q)['days'],5)
        self.assertTrue(holding_deadline(self.position(),q)['sell'])
        self.assertFalse(holding_deadline(self.position(),self.quote('14:53'))['sell'])
        self.assertFalse(holding_deadline(self.position(),q,False)['sell'])
        q['sessions']=[]
        self.assertFalse(holding_deadline(self.position(),q)['sell'])

    def test_two_sealed_days_extend_single_sealed_day_exits(self):
        q=self.quote()
        q.update(price=11.,prev_close=10.)
        self.assertTrue(holding_deadline(self.position(),q)['sell'])
        q['daily']['bars'][-2]['close']=10.
        q['daily']['bars'][-1]['close']=11.
        q.update(price=12.10,prev_close=11.)
        self.assertTrue(holding_deadline(self.position(),q)['deferred'])
        q.update(price=12.)
        self.assertTrue(holding_deadline(self.position(),q)['sell'])

    def test_switch_default_and_persistence(self):
        cfg=checked_config(dict(name='test',formula='XG:C>O;',pool='market'))
        self.assertTrue(cfg['holding_limit'])
        with tempfile.TemporaryDirectory() as root:
            ledger=PaperLedger(Path(root)/'paper.sqlite3')
            ledger.start('demo')
            self.assertEqual(ledger.snapshot('demo')['accounts'][0]['holding_limit'],1)
            ledger.set_holding_limit('demo','main',False)
            again=PaperLedger(Path(root)/'paper.sqlite3')
            self.assertEqual(again.snapshot('demo')['accounts'][0]['holding_limit'],0)


class SelectionTests(unittest.TestCase):
    def fixture(self):
        day='2026-09-30'
        daily={'raw':[['2026-09-29',10,10,11,9,900],[day,10,12,13,9,800]],
               'qfq':[['2026-09-29',5,5,5.5,4.5,900],[day,5,6,6.5,4.5,800]]}
        times=['1000','1030','1100','1130','1330','1400','1430','1500']
        minutes=[['20260930'+time,10,10.5,11,9,100] for time in times]
        return daily,minutes,day

    def test_last_half_hour_cannot_change_1430_signal(self):
        daily,minutes,day=self.fixture()
        before=selection_bars(daily,minutes,day)
        minutes[-1][1:]=[99,100,101,98,1000000]
        self.assertEqual(before,selection_bars(daily,minutes,day))
        self.assertEqual(before[-1]['volume'],700)
        self.assertEqual(before[-1]['close'],10.5)
        self.assertEqual(before[0]['close'],10)

    def test_missing_or_duplicate_half_hour_is_not_filled(self):
        daily,minutes,day=self.fixture()
        with self.assertRaises(ValueError):selection_bars(daily,minutes[1:],day)
        with self.assertRaises(ValueError):selection_bars(daily,minutes+[minutes[0]],day)

    def test_prefilter_only_rejects_provably_false_terms(self):
        bars=[dict(open=10,close=10,high=11,low=9,volume=100)]*40
        expressions=['XG:C>O AND C>MA(C,20);','XG:C>O OR C>=MA(C,20);',
                     'XG:NOT(C<O) AND C>=REF(C,1);','XG:IF(C>O,1,0);',
                     'XG:CROSS(C,20);','B:=C>O; XG:B AND C>0;']
        for formula in expressions:
            if not might_match(formula,bars):
                self.assertEqual(evaluate_formula(formula,bars),0)


class MinuteHistoryTests(unittest.TestCase):
    def test_actual_tick_weighted_average_includes_opening_auction(self):
        data={'rows':[dict(time='09:25',price=10,vol=100),dict(time='09:30',price=12,vol=100),
                      dict(time='14:59',price=11,vol=100),dict(time='15:00',price=11,vol=100)]}
        points=minute_points(data,dict(close=11,volume=400))
        self.assertEqual(len(points),240)
        self.assertEqual(points[0]['time'],'09:31')
        self.assertEqual(points[0]['average'],11)
        self.assertEqual(points[0]['volume'],200)
        self.assertEqual(points[-1]['price'],11)
        self.assertEqual(sum(p['volume'] for p in points),400)

    def test_incomplete_tick_archive_is_rejected(self):
        with self.assertRaises(ValueError):
            minute_points({'rows':[dict(time='10:00',price=10,vol=100)]},dict(close=10,volume=1000))

    def test_future_dates_are_rejected_before_fetch(self):
        with tempfile.TemporaryDirectory() as root:
            store=BacktestStore(root,None)
            with self.assertRaises(ValueError):store.start(['a'],'2099-01-01','2099-01-03')

    def test_exports_are_confined_to_one_historical_run(self):
        with tempfile.TemporaryDirectory() as root:
            store=BacktestStore(root,None)
            run_id='a'*32
            run=Path(root)/run_id
            run.mkdir()
            (run/'report.json').write_text('{"status":"partial"}')
            self.assertFalse(store.get(run_id)['xlsx_available'])
            self.assertEqual(store.export_path(run_id,'json'),(run/'report.json').resolve())
            with self.assertRaises(FileNotFoundError):store.export_path(run_id,'xlsx')
            with self.assertRaises(ValueError):store.export_path('../accounts','json')
            with self.assertRaises(ValueError):store.export_path(run_id,'../../account.sqlite3')
            (run/'detail.xlsx').write_bytes(b'export')
            self.assertTrue(store.get(run_id)['xlsx_available'])


if __name__=='__main__':
    unittest.main()
