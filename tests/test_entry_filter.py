import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from terminal_entry_filter import EntryFilterService,entry_eligible
from terminal_strategy_sim import checked_config,StrategyStore,StrategyEngine
from terminal_strategy_scan import StrategyMarketScan
from terminal_strategy_rank import strategy_rank
from terminal_backtest import BacktestStore
from tests import test_strategy_sim as harness
from tests.test_strategy_sim import config,bars,DAY


class FrozenEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'models').mkdir()
        self.model=dict(version='entry-model-v1',features=[dict(feature='volume_ratio',lower=0,upper=5,center=1,scale=1)],return_coefficients=[0,1],stop_coefficients=[0,0],rise_coefficients=[0,0])
        (self.root/'models'/'test-model.json').write_text(json.dumps(dict(id='test-model',label='隔离量价测试',model=self.model)))
        self.reader=Mock(return_value=dict(features=dict(volume_ratio=2),as_of=DAY+' 14:30:00',source='隔离量价数据'))
        self.service=EntryFilterService(self.root,self.reader,now=lambda:DAY+' 14:31:00')

    def test_exact_positive_gate_and_missing_evidence(self):
        cfg=dict(entry_model='test-model')
        r=self.service.evaluate(cfg,'600001');self.assertEqual(r['rank'],1);self.assertTrue(entry_eligible(r))
        self.assertEqual(len(r['entry_model_sha256']),64)
        self.service.evaluate(cfg,'600001');self.assertEqual(self.reader.call_count,1)
        self.reader.return_value['features']['volume_ratio']=1
        r=self.service.evaluate(cfg,'600002');self.assertEqual(r['rank'],0);self.assertFalse(entry_eligible(r))
        self.reader.return_value['features']['volume_ratio']=None
        r=self.service.evaluate(cfg,'600003');self.assertIsNone(r['rank']);self.assertFalse(entry_eligible(r));self.assertEqual(r['entry_status'],'missing')

    def test_cutoff_missing_model_and_wrong_time_never_fall_back(self):
        self.service.now=lambda:DAY+' 14:29:59'
        self.assertFalse(entry_eligible(self.service.evaluate(dict(entry_model='test-model'),'600001')))
        self.reader.assert_not_called()
        self.service.now=lambda:DAY+' 14:31:00'
        self.assertFalse(entry_eligible(self.service.evaluate(dict(entry_model='not-installed'),'600001')))
        self.reader.return_value['as_of']=DAY+' 15:00:00'
        self.assertFalse(entry_eligible(self.service.evaluate(dict(entry_model='test-model'),'600001')))

    def test_unpublished_snapshot_cannot_poison_current_day_cache(self):
        for kind in ('daily','bars'):
            p=self.root/'data'/DAY/kind/'600001.json';p.parent.mkdir(parents=True);p.write_text('{}')
        with patch.object(self.service,'_snapshot',side_effect=ValueError('14:30 bar pending')):
            with self.assertRaises(ValueError):self.service.snapshot('600001',DAY,self.model)
        self.assertFalse((self.root/'data'/DAY/'bars'/'600001.json').exists())
        self.assertFalse((self.root/'data'/DAY/'daily'/'600001.json').exists())

    def test_settings_validate_clock_scope_and_paths(self):
        body=dict(config(),entry_model='test-model',buy_time='14:30',boards=['main'])
        self.assertEqual(checked_config(body)['entry_model'],'test-model')
        for change in [dict(entry_model='../secret'),dict(buy_time='14:31'),dict(boards=['main','star'])]:
            with self.subTest(change=change),self.assertRaises(ValueError):checked_config(dict(body,**change))
        with self.assertRaises(ValueError):strategy_rank(body,bars())

    def test_scan_keeps_formula_matches_but_only_positive_candidates_in_buy_pool(self):
        cfg=checked_config(dict(config(),boards=['main'],entry_model='test-model',buy_time='14:30'))
        universe=Mock();universe.load.return_value=[dict(code='600001',name='一'),dict(code='600002',name='二')]
        reader=lambda code,*_:dict(bars=bars(),source='隔离日线')
        ranker=lambda c,code:dict(rank=2 if code=='600002' else -1,entry_model='test-model',entry_pass=code=='600002',entry_status='passed' if code=='600002' else 'filtered')
        scan=StrategyMarketScan(universe,reader,entry_reader=ranker)
        with patch('terminal_strategy_scan.clock',return_value=DAY+' 14:31:00'):
            scan.request(dict(id='s',mode='live',config=cfg));scan.jobs['s']['thread'].join(3)
            p=scan._pool('s');self.assertTrue(p['ready']);self.assertEqual([r['code'] for r in p['items']],['600002'])
            self.assertEqual(scan.snapshot('s')['matched'],2)

    def test_missing_filter_data_prevents_incomplete_scan_ranking_from_buying(self):
        cfg=checked_config(dict(config(),boards=['main'],entry_model='test-model',buy_time='14:30'))
        universe=Mock();universe.load.return_value=[dict(code='600001',name='一')]
        scan=StrategyMarketScan(universe,lambda *a:dict(bars=bars()),entry_reader=lambda *a:dict(rank=None,entry_pass=False,entry_status='missing',entry_reason='同期量缺失'))
        with patch('terminal_strategy_scan.clock',return_value=DAY+' 14:31:00'):
            scan.request(dict(id='s',mode='live',config=cfg));scan.jobs['s']['thread'].join(3)
            self.assertFalse(scan._pool('s')['ready']);self.assertEqual(scan.snapshot('s')['failed'],1)

    def test_backtest_cannot_silently_ignore_active_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=StrategyStore(Path(tmp)/'strategies');item=store.create(dict(config(),entry_model='test-model',boards=['main'],buy_time='14:30'),'live')
            replay=BacktestStore(Path(tmp)/'history',store)
            with patch('terminal_backtest.clock',return_value=DAY+' 19:00:00'),self.assertRaisesRegex(ValueError,'不能忽略'):
                replay.start([item['id']],'2026-10-01','2026-11-01')
            self.assertEqual(replay.list(),[])


class ForwardGateTests(unittest.TestCase):
    setUp=harness.EngineTests.setUp
    read=harness.EngineTests.read
    toggle=harness.EngineTests.toggle
    tick=harness.EngineTests.tick
    def test_filter_blocks_entry_and_cancels_previously_pending_buy(self):
        self.toggle();self.assertTrue(self.tick()['ledger']['accounts'][0]['orders'])
        cfg=dict(self.store.get(self.sid)['config'],entry_model='test-model',boards=['main'])
        self.engine.update_config(self.sid,cfg)
        self.engine.entry_reader=lambda c,code:dict(rank=-1,entry_model='test-model',entry_pass=False,entry_reason='未通过门槛')
        result=self.tick();orders=result['ledger']['accounts'][0]['orders']
        self.assertTrue(all(o['status']=='cancelled' for o in orders));self.assertEqual(result['ledger']['accounts'][0]['trades'],[])
        self.assertTrue(result['result']['items'][0]['matched']);self.assertFalse(result['result']['items'][0]['entry_pass'])


if __name__=='__main__':unittest.main()
