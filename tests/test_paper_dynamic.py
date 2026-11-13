import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from terminal_paper import PaperLedger
from terminal_paper_runner import PaperRunner

DAY='2026-11-11'
PLAN=dict(date='2026-11-10',as_of='15:00',stop=9,target1=13,target2=15)

class DynamicTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.ledger=PaperLedger(Path(self.tmp.name)/'paper.db')
        self.ledger.start('live','2026-11-10 09:30:00');self.now=DAY+' 10:03:00';self.bar='10:02'
        self.weak_codes=set();self.pools={'main':[],'scan':[],'_ready':{'main':True,'scan':True},'_safe':{'main':True,'scan':True},'_at':{'main':DAY+' 10:02:00','scan':DAY+' 10:02:00'}}
        self.runner=PaperRunner(self.ledger,None,None,None,None)
        self.runner._pool=lambda *args:self.pools
        self.runner.read=lambda code,*args:self.quote(code)
    def tearDown(self):self.tmp.cleanup()
    def quote(self,code):
        negative=code in self.weak_codes;f=dict(status='ok',date=DAY,net=-100 if negative else 1000,ratio=-1 if negative else 5)
        points=[dict(time='10:01',price=10,average=11 if negative else 9.9,volume=10000),dict(time=self.bar,price=10,average=11 if negative else 9.9,volume=10000)]
        minute=dict(date=DAY,as_of=DAY+' '+self.bar,points=points,signals=[dict(time=self.bar,kind='弱转强')])
        return dict(valid=True,price=10,date=DAY,as_of=minute['as_of'],prev_close=10,points=points,minute=minute,daily=dict(plan=PLAN,name='样本'+code,funds=f,score=90,support=8))
    def tick(self):
        with patch('terminal_paper_runner.clock',return_value=self.now):self.runner.tick('live')
        return self.ledger.snapshot('live',self.now)['accounts'][0]
    def pending(self,code='600001'):
        return self.ledger.submit('live','main',code,'样本','buy',100,10,DAY+' 09:59',PLAN,'测试',now=DAY+' 10:00:00')
    def seed_five(self):
        for i in range(5):
            self.ledger.submit('live','main','60000'+str(i),'样本','buy',100,10,'2026-11-10 09:59',PLAN,'种子',now='2026-11-10 10:00:00')
        quotes={c:dict(valid=True,price=10,prev_close=10,date='2026-11-10',as_of='2026-11-10 10:01',points=[dict(time='10:01',price=10,volume=10000)]) for c in ['60000'+str(i) for i in range(5)]}
        self.ledger.update('live',quotes,'2026-11-10 10:02:00')
    def test_background_market_result_is_consumed_before_freshness_window_expires(self):
        snapshots=[dict(stale=True,last_error='刷新中',groups=[]),dict(groups=[dict(name='行业',board_type='main',leaders=[dict(code='600001',name='样本')])])]
        runner=PaperRunner(self.ledger,lambda _:snapshots.pop(0),lambda _:[],None,None)
        with patch('terminal_paper_runner.clock',return_value=DAY+' 10:00:00'), patch('terminal_paper_runner.time.time',return_value=100):
            self.assertFalse(runner._pool('live')['_ready']['main'])
        with patch('terminal_paper_runner.clock',return_value=DAY+' 10:00:06'), patch('terminal_paper_runner.time.time',return_value=106):
            self.assertEqual(runner._pool('live')['main'][0]['code'],'600001')

    def test_potential_boards_cannot_generate_automatic_entries(self):
        runner=PaperRunner(self.ledger,lambda _:dict(groups=[dict(name='潜在',board_type='potential',board_label='潜在待涨观察',leaders=[dict(code='600001',name='观察')])]),lambda _:None,None,None)
        pools=runner._pool('live')
        self.assertEqual(pools['main'],[])
        diag=self.ledger.snapshot('live')['pool_diagnostics']
        self.assertIn('潜在观察不自动开仓',next(d for d in diag if d['account']=='main')['reason'])
        self.assertEqual(next(d for d in diag if d['account']=='scan')['state'],'loading')

    def test_completed_exclusion_cancels_pending_before_fill(self):
        self.pending();a=self.tick()
        self.assertEqual(a['orders'][0]['status'],'cancelled');self.assertEqual(a['trades'],[])
        self.assertIn('移出',a['orders'][0]['result'])
    def test_unavailable_or_partial_pool_never_implies_exclusion(self):
        self.pending();self.pools['_ready']['main']=False
        self.assertEqual(self.tick()['orders'][0]['status'],'pending')
        self.pools['_ready']['main']=True;self.pools['_safe']['main']=False
        self.assertEqual(self.tick()['orders'][0]['status'],'pending')
    def test_in_pool_buy_revalidates_money_before_fill(self):
        self.pending();self.pools['main']=[dict(code='600001',name='样本')];self.weak_codes.add('600001')
        a=self.tick();self.assertEqual(a['orders'][0]['status'],'cancelled');self.assertEqual(a['trades'],[])
    def test_entry_explains_missing_funds_and_zero_is_known_negative_evidence(self):
        q=self.quote('600001');q['daily']['funds'].update(status='stale',reason='资金接口超时')
        check=self.runner.entry(q,'scan')
        self.assertTrue(check['data_error']);self.assertIn('资金接口超时','；'.join(check['reasons']))
        q['daily']['funds'].update(status='ok',net=0,ratio=0)
        check=self.runner.entry(q,'scan')
        self.assertTrue(check['known']);self.assertFalse(check['good']);self.assertFalse(check['data_error'])
        self.assertIn('未同时为正','；'.join(check['reasons']))
    def test_zero_funds_cancels_a_pending_buy_before_fill(self):
        self.pending();self.pools['main']=[dict(code='600001',name='样本')]
        q=self.quote('600001');q['daily']['funds'].update(net=0,ratio=0)
        self.runner.read=lambda *args:q
        a=self.tick();self.assertEqual(a['orders'][0]['status'],'cancelled');self.assertFalse(a['trades'])
    def test_distinct_snapshot_confirmation_and_return_reset(self):
        for t in ['10:00:00','10:00:00','10:01:00']:
            state=self.ledger.pool_absence('live','main',{'600001'},set(),DAY+' '+t)
        self.assertEqual(state['600001']['misses'],2);self.assertFalse(state['600001']['mature'])
        reopened=PaperLedger(self.ledger.path)
        state=reopened.pool_absence('live','main',{'600001'},set(),DAY+' 10:02:00');self.assertTrue(state['600001']['mature'])
        self.assertEqual(reopened.pool_absence('live','main',{'600001'},{'600001'},DAY+' 10:03:00'),{})
        state=reopened.pool_absence('live','main',{'600001'},set(),DAY+' 10:04:00');self.assertEqual(state['600001']['misses'],1)
    def test_five_healthy_holdings_not_churned_when_pool_changes(self):
        self.seed_five();self.pools['main']=[dict(code='600099',name='新候选')]
        a=self.tick();self.assertEqual(len(a['positions']),5)
        self.assertFalse(any(o['side']=='sell' for o in a['orders']))
        self.assertFalse(any(o['code']=='600099' for o in a['orders']))
    def test_rotation_sells_before_slot_released_and_then_reevaluates(self):
        self.seed_five();codes={'60000'+str(i) for i in range(5)}
        for t in ['10:00:00','10:01:00','10:02:00']:self.ledger.pool_absence('live','main',codes,set(),DAY+' '+t)
        self.weak_codes={'600001'};self.pools['main']=[dict(code='600099',name='新候选')]
        a=self.tick();sell=[o for o in a['orders'] if o['side']=='sell']
        self.assertEqual(len(sell),1);self.assertIn('动态腾仓',sell[0]['reason']);self.assertEqual(sell[0]['status'],'pending')
        self.assertFalse(any(o['code']=='600099' for o in a['orders']))
        self.now=DAY+' 10:05:00';self.bar='10:04'
        a=self.tick();self.assertFalse(any(p['code']=='600001' for p in a['positions']))
        new=[o for o in a['orders'] if o['code']=='600099'];self.assertEqual(len(new),1);self.assertEqual(new[0]['status'],'pending')
        self.assertEqual(next(o for o in a['orders'] if o['id']==sell[0]['id'])['status'],'filled')

    def test_rotation_does_not_bypass_t1(self):
        self.seed_five();codes={'60000'+str(i) for i in range(5)}
        for t in ['10:00:00','10:01:00','10:02:00']:self.ledger.pool_absence('live','main',codes,set(),DAY+' '+t)
        with self.ledger.db() as db:db.execute('UPDATE lots SET day=? WHERE code=?',(DAY,'600001'))
        self.weak_codes={'600001'};self.pools['main']=[dict(code='600099',name='新候选')]
        a=self.tick();self.assertEqual(len(a['positions']),5)
        self.assertFalse(any(o['side']=='sell' for o in a['orders']))
        self.assertFalse(any(o['code']=='600099' for o in a['orders']))
    def test_recent_exit_not_rebought_despite_new_signal(self):
        self.seed_five()
        self.ledger.submit('live','main','600001','样本','sell',100,10,DAY+' 09:58',PLAN,'退出',now=DAY+' 09:59:00')
        q=self.quote('600001');q['points']=[dict(time='10:00',price=10,volume=10000)];q['as_of']=DAY+' 10:00'
        self.ledger.update('live',{'600001':q},DAY+' 10:01:00')
        self.pools['main']=[dict(code='600001',name='新候选')]
        a=self.tick()
        self.assertFalse(any(o['side']=='buy' and o['status']=='pending' for o in a['orders']))
        pool=self.ledger.snapshot('live',self.now)['candidates']['main']['items']
        self.assertIn('冷静期',pool[0]['decision'])

if __name__=='__main__':unittest.main()
