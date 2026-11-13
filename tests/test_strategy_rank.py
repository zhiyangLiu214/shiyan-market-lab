import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from datetime import datetime,timedelta
import pandas as pd

from terminal_engine import analyze
from terminal_formula import FormulaError
from terminal_strategy_rank import strategy_rank
from terminal_strategy_sim import StrategyStore,StrategyEngine,checked_config
from terminal_strategy_scan import StrategyMarketScan

DAY='2026-11-13'

def bars(strong=False):
    rows=[]
    for i in range(40):
        price=(10+i*.04 if strong else 10-i*.01)
        rows.append(dict(date=(datetime(2026,11,13)-timedelta(days=39-i)).strftime('%Y-%m-%d'),open=price,close=price,high=price+.1,low=price-.1,volume=20000 if strong and i==39 else 10000))
    return rows

def config(**kw):return checked_config(dict(name='综合分策略',pool='custom',codes=['600001','600002'],formula='C>0',**kw))

class RankTests(unittest.TestCase):
    def test_composite_matches_existing_model_and_is_not_85_gate(self):
        for strong in [False,True]:
            data=bars(strong);score=strategy_rank(config(),data,'600001');expected=analyze(pd.DataFrame(data),'600001')
            self.assertEqual(score['rank'],expected['score']);self.assertEqual(score['scores'],expected['scores']);self.assertEqual(score['score_weights'],[40,30,30]);self.assertEqual(score['ranking_mode'],'composite')
        self.assertLess(strategy_rank(config(),bars())['rank'],85)
        self.assertGreater(strategy_rank(config(),bars(True))['rank'],strategy_rank(config(),bars())['rank'])
    def test_missing_history_and_zero_volume_do_not_receive_default_score(self):
        with self.assertRaises(FormulaError):strategy_rank(config(),bars()[:10])
        data=bars();[row.update(volume=0) for row in data]
        with self.assertRaises(FormulaError):strategy_rank(config(),data)
    def test_new_config_defaults_composite_legacy_formula_preserved(self):
        self.assertEqual(config()['ranking_mode'],'composite')
        self.assertEqual(strategy_rank(dict(ranking='C'),bars())['rank'],bars()[-1]['close'])
        with self.assertRaises(ValueError):config(ranking_mode='mystery')
        with tempfile.TemporaryDirectory() as tmp:
            store=StrategyStore(tmp);item=store.create(config(),'live')
            with store.db() as db:
                import json
                old=dict(item['config']);old.pop('ranking_mode');db.execute('UPDATE strategies SET config=? WHERE id=?',(json.dumps(old),item['id']))
            self.assertEqual(store.get(item['id'])['config']['ranking_mode'],'formula')
    def test_execution_buys_highest_score_with_one_slot_and_records_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=StrategyStore(Path(tmp));item=store.create(config(max_positions=1),'live')
            def read(code,mode,meta):
                data=bars(code=='600002')
                return dict(valid=True,price=data[-1]['close'],date=DAY,as_of=DAY+' 14:29',points=[dict(time='14:29',price=data[-1]['close'],average=10,volume=20000)],prev_close=data[-2]['close'],daily=dict(name='测试'+code,bars=data,source='隔离日线'))
            engine=StrategyEngine(store,read,None)
            with patch('terminal_strategy_sim.clock',return_value=DAY+' 14:30:10'),patch('terminal_paper.clock',return_value=DAY+' 14:30:10'):
                engine.toggle(item['id'],True);snapshot=engine.tick(item['id'])
            orders=snapshot['ledger']['accounts'][0]['orders'];self.assertEqual(len(orders),1);self.assertEqual(orders[0]['code'],'600002');self.assertIn('综合评分',orders[0]['reason'])
            ranked={r['code']:r for r in snapshot['result']['items']};self.assertGreater(ranked['600002']['rank'],ranked['600001']['rank'])
    def test_market_scan_uses_same_composite_score_before_shortlist(self):
        universe=unittest.mock.Mock();universe.load.return_value=[dict(code=c,name=c) for c in ['600001','600002']]
        scan=StrategyMarketScan(universe,lambda code,*_:dict(bars=bars(code=='600002'),source='隔离日线'))
        cfg=config();cfg['pool']='market'
        with patch('terminal_strategy_scan.clock',return_value=DAY+' 14:00:00'):
            scan.request(dict(id='s',mode='live',config=cfg));scan.jobs['s']['thread'].join(4);pool=scan._pool('s')
        self.assertTrue(pool['ready']);self.assertEqual(pool['items'][0]['code'],'600002');self.assertEqual(pool['items'][0]['score'],strategy_rank(cfg,bars(True),'600002')['score'])

if __name__=='__main__':unittest.main()
