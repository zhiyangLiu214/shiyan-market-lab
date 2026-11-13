import tempfile
import threading
import unittest
from unittest.mock import patch

from terminal_universe import StockUniverse,stock_board
from terminal_strategy_sim import checked_config,StrategyStore,StrategyEngine
from terminal_strategy_scan import StrategyMarketScan

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
    def test_scan_passes_verified_name_to_formula(self):
        universe=unittest.mock.Mock();universe.load.return_value=[dict(code='600001',name='*ST测试')]
        scan=StrategyMarketScan(universe,lambda *a:dict(bars=[dict(date=DAY,close=10)],source='测试日线'))
        c=cfg();c['formula']="NAMELIKE('*ST')"
        with patch('terminal_strategy_scan.clock',return_value=DAY+' 14:00:00'):
            scan.request(dict(id='s',mode='live',config=checked_config(c)));scan.jobs['s']['thread'].join(3);pool=scan._pool('s')
        self.assertTrue(pool['ready']);self.assertEqual(len(pool['items']),1)

    def test_unavailable_roster_is_not_an_empty_success(self):
        universe=unittest.mock.Mock();universe.load.side_effect=ValueError('offline');scan=StrategyMarketScan(universe,None)
        scan.request(dict(id='s',mode='live',config=checked_config(cfg())));scan.jobs['s']['thread'].join(3)
        self.assertEqual(scan.snapshot('s')['status'],'error');self.assertFalse(scan._pool('s')['ready'])

if __name__=='__main__':unittest.main()
