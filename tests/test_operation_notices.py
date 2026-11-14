import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from terminal_paper import PaperLedger
from terminal_strategy_sim import StrategyStore
from terminal_operation_notices import TradeNoticeService

DAY='2026-11-11'


class OperationNoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.book=PaperLedger(self.root/'paper.sqlite3');self.book.start('live',DAY+' 09:30:00')

    def order(self,side='buy',qty=100,day=DAY,source='manual'):
        return self.book.submit('live','main','600001','样本',side,qty,10,day+' 09:59',{},'测试原因',source,day+' 10:00:00')

    def update(self,day=DAY,price=10,volume=10000,now=None):
        quote=dict(valid=True,date=day,as_of=day+' 10:01',price=price,prev_close=10,points=[dict(time='10:01',price=price,volume=volume)])
        self.book.update('live',{'600001':quote},now or day+' 10:02:00')

    def notices(self,kind=None):
        with self.book.db() as db:
            rows=[dict(r) for r in db.execute('select * from operation_notices order by seq')]
        return [r for r in rows if kind is None or r['kind']==kind]

    def test_submission_fill_and_repeated_updates_are_distinct_and_idempotent(self):
        oid=self.order();self.assertEqual(len(self.notices('submitted')),1);self.assertEqual(self.notices('buy_filled'),[])
        self.assertIn('尚未成交',self.notices('submitted')[0]['text'])
        self.update();self.update();self.assertEqual(len(self.notices('buy_filled')),1)
        n=self.notices('buy_filled')[0];self.assertEqual(n['order_id'],oid);self.assertEqual(n['qty'],100);self.assertIsNotNone(n['trade_id']);self.assertGreater(n['fees'],0)
        reopened=PaperLedger(self.book.path)
        with reopened.db() as db:self.assertEqual(db.execute("select count(*) from operation_notices where kind='buy_filled'").fetchone()[0],1)

    def test_add_reduce_and_exit_report_actual_fills_and_pnl(self):
        self.order(qty=200);self.update()
        self.order(qty=100,day='2026-11-12');self.update(day='2026-11-12')
        self.assertIn('加仓',self.notices('buy_filled')[-1]['title'])
        self.order(side='sell',qty=100,day='2026-11-13');self.update(day='2026-11-13',price=10.1)
        n=self.notices('sell_filled')[-1];self.assertIn('减仓',n['title']);self.assertIn('已实现盈亏',n['text'])
        with self.book.db() as db:trade=dict(db.execute('select * from trades where id=?',(n['trade_id'],)).fetchone())
        self.assertEqual(n['realized'],trade['realized']);self.assertEqual(n['price'],trade['price'])
        self.order(side='sell',qty=200,day='2026-11-16');self.update(day='2026-11-16')
        self.assertIn('清仓',self.notices('sell_filled')[-1]['title'])

    def test_cancel_wait_expire_and_reject_each_emit_once(self):
        oid=self.order();self.update(price=11);self.update(price=11)
        self.assertEqual(len(self.notices('waiting')),1);self.assertIn('尚未成交',self.notices('waiting')[0]['text'])
        self.book.cancel('live',oid);self.book.cancel('live',oid)
        self.assertEqual(len(self.notices('cancelled')),1)
        self.order();self.update(now=DAY+' 15:00:00');self.update(now=DAY+' 15:01:00')
        self.assertEqual(len(self.notices('expired')),1)
        self.order()
        with self.book.db() as db:db.execute("update accounts set cash=0 where mode='live' and id='main'")
        self.update();self.update();self.assertEqual(len(self.notices('rejected')),1)

    def test_submission_failure_has_no_fake_order_or_trade_and_auto_deduplicates(self):
        for _ in range(2):
            with self.assertRaises(ValueError):self.order(qty=1,source='auto')
        self.assertEqual(len(self.notices('submission_rejected')),1)
        self.assertIn('未形成有效委托',self.notices('submission_rejected')[0]['text'])
        with self.book.db() as db:
            self.assertEqual(db.execute('select count(*) from orders').fetchone()[0],0)
            self.assertEqual(db.execute('select count(*) from trades').fetchone()[0],0)

    def test_notice_and_trade_roll_back_together(self):
        self.order()
        with self.book.db() as db:
            db.execute("create trigger fail_update before update of status on orders when NEW.status='filled' begin select raise(ABORT,'isolated failure'); end")
        with self.assertRaises(sqlite3.IntegrityError):self.update()
        self.assertEqual(self.notices('buy_filled'),[])
        with self.book.db() as db:
            self.assertEqual(db.execute('select count(*) from trades').fetchone()[0],0)
            self.assertEqual(db.execute("select cash from accounts where mode='live' and id='main'").fetchone()[0],100000)

    def test_migration_does_not_backfill_old_operations_or_change_balances(self):
        self.order();self.update()
        with self.book.db() as db:
            before=db.execute('select cash,realized from accounts order by id').fetchall()
            for row in db.execute("select name from sqlite_master where type='trigger' and name like 'ops_%'").fetchall():db.execute('drop trigger '+row[0])
            db.execute('drop table operation_notices')
        reopened=PaperLedger(self.book.path)
        with reopened.db() as db:
            self.assertEqual([tuple(r) for r in db.execute('select cash,realized from accounts order by id')],[tuple(r) for r in before])
            self.assertEqual(db.execute('select count(*) from operation_notices').fetchone()[0],0)

    def test_direct_sql_cancel_and_account_controls_are_covered(self):
        oid=self.order()
        with self.book.db() as db:db.execute("update orders set status='cancelled',result='策略暂停' where id=?",(oid,))
        self.assertEqual(len(self.notices('cancelled')),1)
        self.book.toggle('live',False);self.book.toggle('live',False);self.book.toggle('live',True)
        self.assertEqual(len(self.notices('paused')),1);self.assertEqual(len(self.notices('resumed')),1)
        self.book.set_cap('live','main',60);self.assertEqual(len(self.notices('settings')),1)

    def test_cursor_pages_modes_archived_accounts_and_signals(self):
        store=StrategyStore(self.root/'strategies');item=store.create(dict(name='测试策略',formula='C>0',pool='custom',codes=['600001']),'live')
        custom=store.book(item['id']);custom.start('live',DAY+' 09:30:00')
        custom.submit('live','main','600001','股票','buy',100,10,DAY+' 09:59',{},'测试',now=DAY+' 10:00:00')
        with store.db() as db:
            db.execute("update strategies set mode='archived-live' where id=?",(item['id'],))
            db.execute('insert into notices values(?,?,?,?,?,?,?,?,?)',('signal',item['id'],'live','600001','股票','exit',DAY,DAY+' 10:01:00','触发卖出，尚非成交'))
        self.book.start('demo',DAY+' 09:30:00');self.order()
        service=TradeNoticeService(self.book,store);cursor={};events=[]
        for _ in range(10):
            page=service.page('live',cursor,limit=1);events+=page['events'];cursor=page['cursors']
            if not page['has_more']:break
        self.assertEqual(len({e['id'] for e in events}),len(events));self.assertTrue(any(e['phase']=='signal' for e in events));self.assertTrue(any(e['origin']=='策略：测试策略' for e in events))
        self.assertTrue(all(e['mode']=='live' for e in events));self.assertEqual(service.page('live',cursor)['events'],[])
        with self.assertRaises(ValueError):service.page('live',{'paper':-1})
        with self.assertRaises(ValueError):service.page('live',{'paper':True})

    def test_public_endpoint_pages_notices_and_records_prevalidation_failure_without_starting_account(self):
        import os
        from flask import Flask
        from unittest.mock import patch
        from terminal_api import register_terminal
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'TDX_NO_PAPER_WORKER':'1'}):
            app=Flask('notice_test',root_path=tmp);register_terminal(app,lambda:[],lambda _:[]);client=app.test_client()
            self.assertEqual(client.get('/api/terminal/trade-notices').json['data']['events'],[])
            result=client.post('/api/terminal/paper',json=dict(action='order',mode='live',account='main',code='600001',side='buy',qty=100))
            self.assertEqual(result.status_code,400)
            page=client.get('/api/terminal/trade-notices').json['data'];self.assertEqual(len(page['events']),1);self.assertEqual(page['events'][0]['kind'],'submission_rejected')
            cursor=json.dumps(page['cursors']);self.assertEqual(client.get('/api/terminal/trade-notices',query_string=dict(cursors=cursor)).json['data']['events'],[])
            self.assertEqual(client.get('/api/terminal/trade-notices?mode=demo').json['data']['events'],[])
            self.assertEqual(client.get('/api/terminal/paper').json['data']['accounts'],[])
            self.assertEqual(client.get('/api/terminal/trade-notices',query_string=dict(cursors='{"paper":-1}')).status_code,400)


if __name__=='__main__':unittest.main()
