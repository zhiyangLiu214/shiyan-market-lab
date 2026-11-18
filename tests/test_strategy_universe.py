import tempfile
import threading
import unittest
from unittest.mock import patch

from terminal_universe import StockUniverse,stock_board
from terminal_strategy_sim import checked_config,StrategyStore,StrategyEngine
from terminal_strategy_scan import StrategyMarketScan
from terminal_quotes import InsufficientDailyError,NoCurrentDailyError

DAY='2026-11-13'

def cfg(**kw):return dict(name='市场策略',pool='market',formula='C>0',ranking='C',ranking_mode='formula',**kw)

class UniverseTests(unittest.TestCase):
    def test_board_membership_includes_new_main_and_excludes_other_securities(self):
        for code in ['600000','601001','603001','605001','000001','001001','002001','003001']:self.assertEqual(stock_board(code),'main')
        for code in ['300001','301001']:self.assertEqual(stock_board(code),'chinext')
        self.assertEqual(stock_board('688001'),'star')
        for code in ['920001','830001','430001','200001','900001','510001','110001','689001','999999','30001',None]:self.assertIsNone(stock_board(code))
    def test_config_defaults_and_rejects_unselected_or_unknown_markets(self):
        self.assertEqual(checked_config(cfg())['boards'],['main','chinext','star'])
        for boards in [[],['beijing'],['main','beijing']]:
            with self.assertRaises(ValueError):checked_config(cfg(boards=boards))
        with self.assertRaises(ValueError):checked_config(dict(cfg(boards=['star']),pool='custom',codes=['600001']))
    def test_roster_cache_dedup_filter_and_partial_is_not_complete(self):
        rows=[dict(code=c,name=c) for c in ['600001','300001','688001','920001','600001']]
        fetch=unittest.mock.Mock(return_value=rows);universe=StockUniverse(fetch)
        self.assertEqual([r['code'] for r in universe.load(['star'])],['688001']);self.assertEqual(len(universe.load(['main','star'])),2);fetch.assert_called_once()
        with self.assertRaises(ValueError):StockUniverse(lambda:rows[:1]).load(['main'])
    def test_candidate_pool_filters_market_and_saved_legacy_config_has_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=StrategyStore(tmp);item=store.create(dict(cfg(boards=['star']),pool='main'),'live')
            engine=StrategyEngine(store,None,lambda *a:dict(ready=True,items=[dict(code=c) for c in ['600001','688001','920001']]))
            self.assertEqual(engine._pool(item)['items'],[dict(code='688001')])

class FullScanTests(unittest.TestCase):
    def test_scheduled_strategies_share_quotes_but_keep_separate_results(self):
        metas=[dict(code='600001',name='主板'),dict(code='300001',name='创业板')]
        universe=unittest.mock.Mock();universe.load.return_value=metas
        reads=[]
        def read(code,*_):
            reads.append(code)
            return dict(bars=[dict(date=DAY,close=20 if code=='300001' else 5)],source='测试日线')
        first=dict(id='first',mode='live',enabled=True,config=checked_config(cfg(boards=['main'])))
        second=dict(id='second',mode='live',enabled=True,config=checked_config(cfg(boards=['main','chinext'])))
        second['config']['formula']='C>10'
        scan=StrategyMarketScan(universe,read,cohort_reader=lambda:[first,second])
        with patch('terminal_strategy_scan.clock',return_value=DAY+' 14:30:00'):
            scan.request(first,scheduled_for=DAY+' 14:30:00')
            scan.jobs['first']['thread'].join(3)
            one=scan._pool('first');two=scan.request(second,scheduled_for=DAY+' 14:30:00')
        self.assertEqual(sorted(reads),['300001','600001'])
        universe.load.assert_called_once()
        self.assertEqual((one['market_scan']['total'],one['market_scan']['matched']),(1,1))
        self.assertEqual((two['market_scan']['total'],two['market_scan']['matched']),(2,1))
        self.assertEqual([row['code'] for row in one['items']],['600001'])
        self.assertEqual([row['code'] for row in two['items']],['300001'])

    def test_invalidated_scan_stops_fetching_queued_symbols(self):
        universe=unittest.mock.Mock()
        universe.load.return_value=[dict(code=str(600001+i),name='股票') for i in range(60)]
        gate=threading.Event();entered=threading.Event();reads=[]
        def read(code,*_):
            reads.append(code);entered.set();gate.wait(2)
            return dict(bars=[dict(date=DAY,close=10)])
        scan=StrategyMarketScan(universe,read)
        with patch('terminal_strategy_scan.clock',return_value=DAY+' 14:30:00'):
            scan.request(dict(id='s',mode='live',config=checked_config(cfg())))
            self.assertTrue(entered.wait(1))
            thread=scan.jobs['s']['thread']
            scan.invalidate('s');gate.set();thread.join(3)
        self.assertLessEqual(len(reads),12)

    def test_all_symbols_scanned_all_matches_visible_top50_requoted(self):
        metas=[dict(code=str(600000+i),name='股票'+str(i)) for i in range(70)]
        universe=unittest.mock.Mock();universe.load.return_value=metas
        def read(code,*a):return dict(bars=[dict(date=DAY,close=int(code))],source='测试日线',as_of=DAY+' 14:00:00')
        scan=StrategyMarketScan(universe,read)
        with patch('terminal_strategy_scan.clock',return_value=DAY+' 14:01:00'):
            scan.request(dict(id='s',mode='live',config=checked_config(cfg())))
            scan.jobs['s']['thread'].join(3);pool=scan._pool('s');snap=scan.snapshot('s')
        self.assertTrue(pool['ready']);self.assertEqual((snap['total'],snap['done'],snap['matched']),(70,70,70))
        self.assertEqual(len(pool['items']),50);self.assertEqual(pool['items'][0]['code'],'600069');self.assertEqual(len(snap['rows']),70)
    def test_scan_does_not_block_and_partial_failure_blocks_buy_pool(self):
        gate=threading.Event();entered=threading.Event();universe=unittest.mock.Mock();universe.load.return_value=[dict(code='600001',name='一'),dict(code='300001',name='二')]
        def read(code,*a):
            entered.set();gate.wait(2)
            if code=='300001':raise ValueError('offline')
            return dict(bars=[dict(date=DAY,close=10)],source='测试')
        scan=StrategyMarketScan(universe,read)
        with patch('terminal_strategy_scan.clock',return_value=DAY+' 14:01:00'):
            first=scan.request(dict(id='s',mode='live',config=checked_config(cfg())))
            self.assertFalse(first['ready']);self.assertTrue(entered.wait(1));self.assertEqual(scan.snapshot('s')['status'],'running')
            gate.set();scan.jobs['s']['thread'].join(3);pool=scan._pool('s')
        self.assertFalse(pool['ready']);self.assertEqual(pool['items'],[]);self.assertEqual(pool['market_scan']['failed'],1)
        self.assertEqual(next(r for r in pool['market_scan']['rows'] if r['code']=='300001')['reason'],'offline')
    def test_scan_passes_verified_name_to_formula(self):
        universe=unittest.mock.Mock();universe.load.return_value=[dict(code='600001',name='*ST测试')]
        scan=StrategyMarketScan(universe,lambda *a:dict(bars=[dict(date=DAY,close=10)],source='测试日线'))
        c=cfg();c['formula']="NAMELIKE('*ST')"
        with patch('terminal_strategy_scan.clock',return_value=DAY+' 14:00:00'):
            scan.request(dict(id='s',mode='live',config=checked_config(c)));scan.jobs['s']['thread'].join(3);pool=scan._pool('s')
        self.assertTrue(pool['ready']);self.assertEqual(len(pool['items']),1)

    def test_scheduled_run_starts_fresh_after_manual_preview(self):
        universe=unittest.mock.Mock()
        universe.load.return_value=[dict(code='600001',name='股票')]
        scan=StrategyMarketScan(universe,lambda *args:dict(bars=[dict(date=DAY,close=10)]))
        item=dict(id='s',mode='live',config=checked_config(cfg()))
        with patch('terminal_strategy_scan.clock',return_value=DAY+' 14:30:05'):
            scan.request(item)
            scan.jobs['s']['thread'].join(2)
            scan.request(item,scheduled_for=DAY+' 14:30:00')
            scan.jobs['s']['thread'].join(2)
            self.assertTrue(scan.request(item,scheduled_for=DAY+' 14:30:00')['ready'])
        self.assertEqual(universe.load.call_count,2)

    def test_unavailable_roster_is_not_an_empty_success(self):
        universe=unittest.mock.Mock();universe.load.side_effect=ValueError('offline');scan=StrategyMarketScan(universe,None)
        with patch('terminal_strategy_scan.time.sleep'):
            scan.request(dict(id='s',mode='live',config=checked_config(cfg())));scan.jobs['s']['thread'].join(3)
        self.assertEqual(scan.snapshot('s')['status'],'error');self.assertFalse(scan._pool('s')['ready'])
        self.assertIn('offline',scan.snapshot('s')['reason'])
        self.assertEqual(universe.load.call_count,2)

    def test_transient_roster_failure_retries_and_scans(self):
        universe=unittest.mock.Mock()
        universe.load.side_effect=[ValueError('temporary'),[dict(code='600001',name='股票')]]
        scan=StrategyMarketScan(universe,lambda *args:dict(bars=[dict(date=DAY,close=10)]))
        with patch('terminal_strategy_scan.clock',return_value=DAY+' 14:01:00'),patch('terminal_strategy_scan.time.sleep'):
            scan.request(dict(id='s',mode='live',config=checked_config(cfg())))
            scan.jobs['s']['thread'].join(3)
            self.assertTrue(scan._pool('s')['ready'])
        self.assertEqual(universe.load.call_count,2)

    def test_missing_current_bar_is_excluded_only_with_broad_current_coverage(self):
        universe=unittest.mock.Mock()
        universe.load.return_value=[dict(code=str(600001+i),name='股票') for i in range(20)]
        def read(code,*args):
            if code=='600001':raise NoCurrentDailyError('两个前复权源均无当日日线')
            return dict(bars=[dict(date=DAY,close=10)])
        scan=StrategyMarketScan(universe,read)
        with patch('terminal_strategy_scan.clock',return_value=DAY+' 14:01:00'):
            scan.request(dict(id='s',mode='live',config=checked_config(cfg())))
            scan.jobs['s']['thread'].join(3)
            result=scan._pool('s')
        self.assertTrue(result['ready'])
        self.assertEqual((result['market_scan']['excluded'],result['market_scan']['failed']),(1,0))

        universe.load.return_value=[dict(code='600001',name='股票')]
        second=StrategyMarketScan(universe,read)
        with patch('terminal_strategy_scan.clock',return_value=DAY+' 14:01:00'):
            second.request(dict(id='s',mode='live',config=checked_config(cfg())))
            second.jobs['s']['thread'].join(3)
            self.assertFalse(second._pool('s')['ready'])

    def test_recent_listing_with_short_history_is_excluded(self):
        universe=unittest.mock.Mock()
        universe.load.return_value=[dict(code='600001',name='新股')]
        scan=StrategyMarketScan(universe,lambda *args:(_ for _ in ()).throw(InsufficientDailyError(11)))
        with patch('terminal_strategy_scan.clock',return_value=DAY+' 14:01:00'):
            scan.request(dict(id='s',mode='live',config=checked_config(cfg())))
            scan.jobs['s']['thread'].join(3)
            result=scan._pool('s')
        self.assertTrue(result['ready'])
        self.assertEqual((result['market_scan']['excluded'],result['market_scan']['failed']),(1,0))

if __name__=='__main__':unittest.main()
