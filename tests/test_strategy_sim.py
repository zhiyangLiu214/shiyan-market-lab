import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch

from terminal_formula import compile_formula,evaluate_formula,FormulaError
from terminal_strategy_sim import StrategyStore,StrategyEngine,checked_config,exit_lines
from terminal_strategy_exit import exit_decision,minute_metrics,pattern_reasons

DAY='2026-11-11'


def config(**kw):
    return dict(name='测试策略',formula='C>MA(C,5)',ranking='C/REF(C,1)-1',ranking_mode='formula',pool='custom',codes=['600001'],**kw)


def bars(day=DAY):
    return [dict(date=(datetime.strptime(day,'%Y-%m-%d')-timedelta(days=35-i)).strftime('%Y-%m-%d'),open=8+i*.05,close=8+i*.05,high=8.1+i*.05,low=7.9+i*.05,volume=10000,amount=10000000) for i in range(36)]


class FormulaTests(unittest.TestCase):
    def test_assignments_boolean_and_functions(self):
        data=bars()
        self.assertEqual(evaluate_formula('M:=MA(C,5);选股:C>M AND COUNT(C>REF(C,1),3)=3;',data),1)
        self.assertEqual(evaluate_formula('C<MA(C,5) OR NOT C>MA(C,5)',data),0)
        self.assertAlmostEqual(evaluate_formula('EMA(C,5)',data),9.65,places=2)
        self.assertEqual(evaluate_formula('EVERY(C>MA(C,5),3)',data),1)
    def test_no_python_code_or_unknown_functions(self):
        for formula in ["__import__('os').system('whoami')",'C[0]','C.__class__','[x for x in C]','C**100000','UNKNOWN(C)','MA(C)','X:=C;X:=V;X>0']:
            with self.subTest(formula=formula),self.assertRaises(FormulaError):compile_formula(formula)
    def test_unknown_history_and_missing_amount_are_not_false_matches(self):
        for formula in ['MA(C,250)>0','C/0>0','COUNT(C>MA(C,30),20)>0']:
            with self.subTest(formula=formula),self.assertRaises(FormulaError):evaluate_formula(formula,bars())
        rows=bars();[r.pop('amount') for r in rows]
        with self.assertRaises(FormulaError):evaluate_formula('AMOUNT>0',rows)


class ExitTests(unittest.TestCase):
    def position(self):return dict(code='600001',average_cost=10,plan={'_opened_at':DAY+' 10:00:00'})
    def quote(self,prices,averages=None,volumes=None,prev=10):
        points=[dict(time=(datetime(2026,11,11,14,0)+timedelta(minutes=i)).strftime('%H:%M'),price=p,average=(averages or [10]*len(prices))[i],volume=(volumes or [100]*len(prices))[i]) for i,p in enumerate(prices)]
        return dict(date=DAY,price=prices[-1],prev_close=prev,points=points)
    def test_one_minute_speed_and_no_lunch_splicing(self):
        q=self.quote([10,10.11],volumes=[100,200]);self.assertAlmostEqual(minute_metrics(q['points'])['speed'],1.1)
        q['points'][-2]['time']='11:30';q['points'][-1]['time']='13:00'
        self.assertIsNone(minute_metrics(q['points'])['speed'])
    def test_limit_holds_lifts_both_lines_and_break_latches_exit(self):
        c=checked_config(config());p=self.position();q=self.quote([10.9,11]);lines=dict(stop=9.5,target=11,opened=p['plan']['_opened_at'],anchor=None)
        a=exit_decision(p,q,c,lines)
        self.assertFalse(a['sell']);self.assertEqual(a['lines']['stop'],11);self.assertEqual(a['lines']['target'],12.1)
        self.assertEqual(a['notices'][0]['kind'],'limit_up')
        q=self.quote([11,10.98]);b=exit_decision(p,q,c,a['lines'],a['state'])
        self.assertTrue(b['sell']);self.assertIn('炸板',b['reason'])
        again=exit_decision(p,self.quote([11,11]),c,b['lines'],b['state']);self.assertTrue(again['sell'])
    def test_profit_target_delays_only_while_strong_then_sells_on_negative_speed(self):
        p=self.position();c=checked_config(config(exit_patterns=[]));lines=dict(stop=9.5,target=10.5)
        a=exit_decision(p,self.quote([10.5,10.62],volumes=[100,200]),c,lines)
        self.assertFalse(a['sell']);self.assertIn('量增价升',a['reason'])
        b=exit_decision(p,self.quote([10.62,10.61]),c,lines,a['state']);self.assertTrue(b['sell'])
    def test_document_cross_protects_profit_before_fixed_target_but_not_a_loss(self):
        c=checked_config(config());q=self.quote([10.5,10.3],averages=[10.4,10.4]);lines=dict(stop=9.5,target=11)
        a=exit_decision(self.position(),q,c,lines);self.assertTrue(a['sell']);self.assertIn('浮盈保护',a['reason'])
        p=self.position();p['average_cost']=10.6
        self.assertFalse(exit_decision(p,q,c,lines)['sell'])
    def test_other_document_patterns_and_zero_average(self):
        cases=[('rebound_failure',[10.3,10.3,10.3,10.3,10.39,10.3]),
               ('range_break',[10.5]*10+[10.3]),
               ('spike_reversal',[10,10.1,10.25,10.3,10.3,10.2])]
        for name,prices in cases:
            with self.subTest(name=name):
                q=self.quote(prices,averages=[10.4]*len(prices))
                self.assertTrue(pattern_reasons(q['points'],[name]))
        q=self.quote([10.3]*6,averages=[0]*6)
        self.assertEqual(pattern_reasons(q['points'],['rebound_failure','vwap_cross']),[])
        self.assertFalse(minute_metrics(q['points'])['below_average'])

    def test_previous_sealed_day_lifts_next_day_and_never_moves_down(self):
        p=self.position();q=dict(date='2026-11-12',daily={'bars':[dict(date='2026-11-10',close=10),dict(date=DAY,close=11)]})
        c=checked_config(config());a=exit_lines(p,q,c)
        self.assertEqual(a['stop'],11);self.assertEqual(a['target'],12.1)
        a['stop']=12;a['target']=14
        b=exit_lines(p,q,c,a);self.assertEqual((b['stop'],b['target']),(12,14))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=StrategyStore(Path(self.tmp.name));self.now=DAY+' 14:30:10';self.minute='14:29';self.price=10;self.prev_close=10;self.good=True
        self.engine=StrategyEngine(self.store,self.read,lambda *a:dict(ready=True,items=[]))
        with patch('terminal_strategy_sim.clock',side_effect=lambda:self.now):self.item=self.store.create(config(),'live')
        self.sid=self.item['id']
    def read(self,code,mode,meta):
        if not self.good:return None
        day=self.now[:10]
        return dict(valid=True,price=self.price,date=day,as_of=day+' '+self.minute,prev_close=self.prev_close,
            points=[dict(time=self.minute,price=self.price,average=9.9,volume=20000)],
            daily=dict(name='测试股票',bars=bars(day),source='隔离测试数据'))
    def toggle(self,enabled=True):
        with patch('terminal_strategy_sim.clock',side_effect=lambda:self.now),patch('terminal_paper.clock',side_effect=lambda:self.now):self.engine.toggle(self.sid,enabled)
    def tick(self,preview=False):
        with patch('terminal_strategy_sim.clock',side_effect=lambda:self.now),patch('terminal_paper.clock',side_effect=lambda:self.now):return self.engine.tick(self.sid,preview)
    def test_preview_never_starts_an_account_or_submits(self):
        result=self.tick(True)
        self.assertFalse(result['enabled']);self.assertEqual(result['ledger']['accounts'],[])
        self.assertTrue(result['result']['items'][0]['matched'])
    def test_edit_keeps_running_account_and_orders_and_updates_cap(self):
        self.toggle()
        before = self.tick()['ledger']['accounts'][0]
        self.assertTrue(before['orders'])
        updated = self.engine.update_config(self.sid, dict(self.item['config'], name='改名', max_exposure=50))
        self.assertEqual(updated['id'], self.sid)
        self.assertTrue(updated['enabled'])
        self.assertEqual(len(self.store.list('live')), 1)
        after = self.store.snapshot(self.sid)['ledger']['accounts'][0]
        self.assertEqual(after['orders'], before['orders'])
        self.assertEqual(after['cash'], before['cash'])
        self.assertEqual(after['cap'], 50)

    def test_delete_running_strategy_archives_ledger_and_cancels_order(self):
        self.toggle()
        self.tick()
        book = self.store.book(self.sid)
        self.engine.archive(self.sid)
        self.assertEqual(self.store.list('live'), [])
        with self.assertRaises(ValueError):
            self.store.get(self.sid)
        account = book.snapshot('live')['accounts'][0]
        self.assertEqual(account['orders'][0]['status'], 'cancelled')

    def test_selection_formula_receives_verified_daily_name(self):
        item=self.store.create(dict(config(),formula="NAMELIKE('测试') AND C>MA(C,5)"),'live')
        self.sid=item['id'];result=self.tick(True)
        self.assertTrue(result['result']['items'][0]['matched'])
        self.assertTrue(result['result']['items'][0]['data_ok'])

    def test_scheduled_buy_forward_fill_one_account_and_no_duplicate(self):
        self.toggle();a=self.tick()['ledger']['accounts'][0]
        self.assertEqual(len(a['orders']),1);self.assertEqual(a['trades'],[])
        self.now=DAY+' 14:31:10';self.minute='14:30'
        self.assertEqual(self.tick()['ledger']['accounts'][0]['trades'],[])
        self.now=DAY+' 14:32:10';self.minute='14:31'
        result=self.tick();a=result['ledger']['accounts'][0]
        self.assertEqual(len(a['orders']),1);self.assertEqual(len(a['trades']),1)
        self.assertEqual(len(result['ledger']['accounts']),1);self.assertEqual(a['positions'][0]['available'],0)
        self.assertEqual(self.tick()['ledger']['accounts'][0]['orders'][0]['id'],a['orders'][0]['id'])
        restored=StrategyStore(self.tmp.name);self.assertTrue(restored.get(self.sid)['enabled'])
        self.assertEqual(len(restored.snapshot(self.sid)['ledger']['accounts'][0]['trades']),1)
    def test_missing_data_can_recover_in_window_and_missed_time_is_not_backfilled(self):
        self.toggle();self.good=False
        self.assertEqual(self.tick()['ledger']['accounts'][0]['orders'],[])
        self.good=True;self.now=DAY+' 14:31:00';self.minute='14:30'
        self.assertEqual(len(self.tick()['ledger']['accounts'][0]['orders']),1)
        other=self.store.create(dict(config(),name='其他策略'),'live')
        self.sid=other['id'];self.now=DAY+' 14:35:00';self.toggle()
        self.assertEqual(self.tick()['ledger']['accounts'][0]['orders'],[])
        self.assertEqual(self.store.daily(self.sid,DAY)['status'],'missed')
    def test_pause_cancels_pending_and_other_strategy_cash_is_independent(self):
        self.toggle();self.tick();self.toggle(False)
        self.assertEqual(self.store.snapshot(self.sid)['ledger']['accounts'][0]['orders'][0]['status'],'cancelled')
        other=self.store.create(dict(config(),name='独立策略'),'live')
        self.sid=other['id'];self.toggle()
        a=self.store.snapshot(self.sid)['ledger']['accounts'][0]
        self.assertEqual(a['cash'],100000);self.assertEqual(a['orders'],[])
    def test_three_custom_accounts_run_alongside_original_two(self):
        from concurrent.futures import ThreadPoolExecutor
        from terminal_paper import PaperLedger
        original=PaperLedger(Path(self.tmp.name)/'original.sqlite3');original.start('live',self.now)
        before=original.snapshot('live',self.now)
        ids=[self.sid]+[self.store.create(dict(config(),name='策略'+str(i)),'live')['id'] for i in range(2)]
        with patch('terminal_strategy_sim.clock',side_effect=lambda:self.now),patch('terminal_paper.clock',side_effect=lambda:self.now):
            for sid in ids:self.engine.toggle(sid,True)
            with ThreadPoolExecutor(max_workers=3) as executor:results=list(executor.map(self.engine.tick,ids))
            fourth=self.store.create(dict(config(),name='第四套'),'live')
            with self.assertRaisesRegex(ValueError,'3套'):self.engine.toggle(fourth['id'],True)
        self.assertEqual(len(original.snapshot('live',self.now)['accounts']),2)
        self.assertEqual(original.snapshot('live',self.now),before)
        for result in results:
            self.assertEqual(len(result['ledger']['accounts']),1)
            account=result['ledger']['accounts'][0]
            self.assertEqual(account['cash'],100000);self.assertEqual(len(account['orders']),1)
        self.assertEqual(len({r['ledger']['accounts'][0]['orders'][0]['id'] for r in results}),3)

    def test_limit_notice_break_intent_waits_for_t1_then_sells_forward(self):
        self.toggle();self.tick()
        self.now=DAY+' 14:32:10';self.minute='14:31';self.tick()
        self.now=DAY+' 14:33:10';self.minute='14:32';self.price=11
        a=self.tick();self.assertEqual(a['risk_lines'][0]['stop'],11)
        self.assertEqual(len(self.store.notices('live')),1)
        self.tick();self.assertEqual(len(self.store.notices('live')),1)
        self.now=DAY+' 14:34:10';self.minute='14:33';self.price=10.95
        a=self.tick();self.assertIn('T+1',a['status']);self.assertEqual(len(a['ledger']['accounts'][0]['orders']),1)
        self.now='2026-11-12 09:32:10';self.minute='09:31';self.price=10.9;self.prev_close=10.95
        a=self.tick();self.assertEqual(a['ledger']['accounts'][0]['orders'][0]['side'],'sell')
        self.now='2026-11-12 09:34:10';self.minute='09:33';self.price=10.8
        a=self.tick()['ledger']['accounts'][0]
        self.assertEqual(a['positions'],[]);self.assertEqual(len(a['trades']),2);self.assertGreater(a['realized'],0)


class StrategyAPITests(unittest.TestCase):
    def test_save_validate_mode_isolation_and_original_accounts_untouched(self):
        from flask import Flask
        from terminal_api import register_terminal
        with tempfile.TemporaryDirectory() as tmp,patch.dict('os.environ',{'TDX_NO_PAPER_WORKER':'1'}):
            app=Flask('strategy_api',root_path=tmp);register_terminal(app,lambda:None,lambda _:None);client=app.test_client()
            self.assertEqual(client.post('/api/terminal/strategy-sim',json={'action':'validate','config':dict(config(),formula='x')}).status_code,400)
            saved=client.post('/api/terminal/strategy-sim',json={'action':'save','config':config()}).get_json()['data']
            self.assertFalse(saved['enabled'])
            listing=client.get('/api/terminal/strategy-sim').get_json()['data'];self.assertEqual(len(listing['items']),1)
            self.assertEqual(client.get('/api/terminal/strategy-sim?mode=demo&id='+saved['id']).status_code,400)
            self.assertIsNone(client.get('/api/terminal/paper').get_json()['data']['run'])
            # Saving or compiling a formula does not start any simulated account.
            book=client.get('/api/terminal/strategy-sim?id='+saved['id']).get_json()['data']['ledger']
            self.assertEqual(book['accounts'],[]);self.assertIsNone(book['run'])


if __name__=='__main__':unittest.main()
