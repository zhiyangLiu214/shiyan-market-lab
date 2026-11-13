import tempfile
import unittest
from pathlib import Path
from terminal_paper import PaperLedger, fees

DAY='2026-11-11'
PLAN=dict(date=DAY,stop=9,target1=12,target2=14)

class PaperTests(unittest.TestCase):
    def test_one_basis_point_commission_and_one_yuan_minimum(self):
        self.assertEqual(fees(10000,'buy'),dict(commission=1.,transfer=.1,stamp=0.))
        self.assertEqual(fees(10000,'sell'),dict(commission=1.,transfer=.1,stamp=5.))
        self.assertEqual(fees(500,'buy')['commission'],1.)
        self.assertEqual(fees(20000,'buy')['commission'],2.)
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.ledger=PaperLedger(Path(self.tmp.name)/'paper.sqlite3')
        self.ledger.start('live',DAY+' 09:30:00')
    def tearDown(self): self.tmp.cleanup()
    def order(self,code='600001',qty=100,account='main',side='buy',now=DAY+' 10:00:00',reference=10):
        return self.ledger.submit('live',account,code,'样本',side,qty,reference,now[:10]+' 09:59',PLAN,'测试','manual',now)
    def quote(self,price=10,day=DAY,time='10:01',prev=10,volume=10000):
        return dict(valid=True,price=price,as_of=day+' '+time,date=day,prev_close=prev,points=[dict(time=time,price=price,volume=volume)])
    def account(self,aid='main'):
        return next(a for a in self.ledger.snapshot('live',DAY+' 10:05:00')['accounts'] if a['id']==aid)
    def test_simultaneous_independent_start_and_restart(self):
        snap=self.ledger.snapshot('live');self.assertEqual(len(snap['accounts']),2)
        self.assertEqual([a['cash'] for a in snap['accounts']],[100000,100000])
        self.ledger.start('live','2026-11-12 09:30:00')
        reopened=PaperLedger(self.ledger.path)
        self.assertEqual(reopened.snapshot('live')['run']['started'],DAY+' 09:30:00')
        self.assertIsNone(reopened.snapshot('demo')['run'])
    def test_no_same_signal_fill_fees_t1_and_idempotence(self):
        self.order();self.ledger.update('live',{'600001':self.quote(time='09:59')},DAY+' 10:00:30')
        self.assertEqual(self.account()['trades'],[])
        self.ledger.update('live',{'600001':self.quote()},DAY+' 10:02:00')
        a=self.account();self.assertEqual(len(a['trades']),1);self.assertEqual(a['positions'][0]['available'],0)
        self.assertLess(a['nav'],100000);self.assertEqual(self.account('scan')['nav'],100000)
        self.ledger.update('live',{'600001':self.quote()},DAY+' 10:02:30')
        self.assertEqual(len(self.account()['trades']),1)
        with self.assertRaisesRegex(ValueError,'T\+1'):
            self.order(side='sell',now=DAY+' 10:00:30')
        self.assertAlmostEqual(a['pnl'],a['realized']+a['unrealized'],places=2)
    def test_pending_slot_and_cap_reservations(self):
        for i in range(5):self.order(code='60000'+str(i))
        with self.assertRaisesRegex(ValueError,'5只'):self.order(code='600099')
        self.ledger.start('demo',DAY+' 09:30:00')
        with self.assertRaisesRegex(ValueError,'仓位'):
            self.ledger.submit('demo','main','600001','样本','buy',7000,10,DAY+' 09:59',PLAN,'测试',now=DAY+' 10:00:00')
        self.ledger.set_cap('demo','main',80)
        self.ledger.submit('demo','main','600001','样本','buy',7000,10,DAY+' 09:59',PLAN,'测试',now=DAY+' 10:00:00')
    def test_conservative_limit_volume_missing_and_expiry(self):
        oid=self.order()
        for q in [self.quote(price=11),self.quote(volume=1),self.quote(prev=None)]:
            self.ledger.update('live',{'600001':q},DAY+' 10:02:00')
            self.assertEqual(self.account()['trades'],[])
        self.ledger.update('live',{'600001':self.quote()},DAY+' 16:00:00')
        self.assertEqual(self.account()['orders'][0]['status'],'expired')
        self.assertEqual(self.account()['trades'],[])
    def test_no_old_price_orders_and_lot_minimum(self):
        with self.assertRaisesRegex(ValueError,'时段'):self.order(now=DAY+' 16:00:00')
        with self.assertRaisesRegex(ValueError,'申报'):self.order(code='688001',qty=100)
        with self.assertRaisesRegex(ValueError,'申报'):self.order(qty=101)
        self.order(code='688001',qty=201)
    def test_next_day_sale_realized_and_nav_reconcile(self):
        self.order();self.ledger.update('live',{'600001':self.quote()},DAY+' 10:02:00')
        day='2026-11-12'
        self.ledger.submit('live','main','600001','样本','sell',100,11,day+' 09:59',PLAN,'退出',now=day+' 10:00:00')
        self.ledger.update('live',{'600001':self.quote(price=11,day=day,prev=11)},day+' 10:02:00')
        a=self.account();self.assertEqual(a['positions'],[])
        self.assertAlmostEqual(a['realized'],a['pnl'],places=2)
        self.assertEqual(a['sell_win_rate'],100)
        self.assertAlmostEqual(a['fees'],2.57,places=2)
    def test_recheck_cap_when_price_moves_and_cancel(self):
        oid=self.order(qty=6500)
        self.ledger.update('live',{'600001':self.quote(price=10.9,prev=11)},DAY+' 10:02:00')
        self.assertEqual(self.account()['orders'][0]['status'],'rejected')
        self.assertEqual(self.account()['trades'],[])
        oid=self.order();self.ledger.cancel('live',oid)
        self.assertEqual(self.account()['orders'][0]['status'],'cancelled')


class PaperRunnerTests(unittest.TestCase):
    def test_two_pools_run_same_rules_forward_without_reusing_signal(self):
        from unittest.mock import patch
        from terminal_paper_runner import PaperRunner
        with tempfile.TemporaryDirectory() as tmp:
            ledger=PaperLedger(Path(tmp)/'ledger.db');ledger.start('live',DAY+' 09:30:00')
            current=[DAY+' 10:02:00'];bar=['10:01']
            f=dict(status='ok',date=DAY,net=100000,ratio=5)
            def detail(code,*args):
                return dict(code=code,name='样本'+code,score=90,funds=f,support=8,
                            plan=dict(date='2026-11-10',as_of='15:00',stop=9,target1=13,target2=15),
                            bars=[dict(date='2026-11-10',close=10)]*10)
            def minute(code,*args):
                return dict(date=DAY,as_of=DAY+' '+bar[0],points=[dict(time=bar[0],price=10,average=9.9,volume=10000)],signals=[dict(time='10:01',kind='弱转强')])
            runner=PaperRunner(ledger,lambda demo:{'groups':[{'name':'行业','board_type':'main','leaders':[{'code':'600001','name':'主线样本'}]}]},lambda demo:[{'code':'600002','name':'三维样本','score':90}],detail,minute)
            with patch('terminal_paper_runner.clock',side_effect=lambda:current[0]):
                runner.tick('live')
                s=ledger.snapshot('live',current[0]);self.assertTrue(all(len(a['orders'])==1 and not a['trades'] for a in s['accounts']))
                current[0]=DAY+' 10:04:00';bar[0]='10:03';runner.tick('live')
                s=ledger.snapshot('live',current[0]);a,b=s['accounts']
                self.assertEqual(a['positions'][0]['code'],'600001');self.assertEqual(b['positions'][0]['code'],'600002')
                self.assertEqual(len(a['orders']),1);self.assertEqual(len(b['orders']),1)
                self.assertEqual(a['nav'],b['nav'])

class PaperAPITests(unittest.TestCase):
    def test_start_cap_toggle_validation_and_mode_isolation(self):
        from unittest.mock import patch
        from flask import Flask
        from terminal_api import register_terminal
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ',{'TDX_NO_PAPER_WORKER':'1'}):
            app=Flask('paper_test',root_path=tmp);register_terminal(app,lambda:None,lambda name:None);client=app.test_client()
            self.assertIsNone(client.get('/api/terminal/paper').get_json()['data']['run'])
            result=client.post('/api/terminal/paper',json={'action':'start'}).get_json()['data']
            self.assertEqual(len(result['accounts']),2)
            result2=client.post('/api/terminal/paper',json={'action':'start'}).get_json()['data']
            self.assertEqual(result['run']['started'],result2['run']['started'])
            self.assertEqual(client.post('/api/terminal/paper',json={'action':'cap','account':'main','cap':101}).status_code,400)
            data=client.post('/api/terminal/paper',json={'action':'cap','account':'main','cap':80}).get_json()['data']
            self.assertEqual(data['accounts'][0]['cap'],80);self.assertEqual(data['accounts'][1]['cap'],70)
            self.assertEqual(len(data['events']),1)
            self.assertEqual(client.post('/api/terminal/paper',json={'action':'toggle','enabled':'yes'}).status_code,400)
            data=client.post('/api/terminal/paper',json={'action':'toggle','enabled':False}).get_json()['data']
            self.assertFalse(data['run']['enabled'])
            self.assertIsNone(client.get('/api/terminal/paper?mode=demo').get_json()['data']['run'])

if __name__=='__main__':unittest.main()
