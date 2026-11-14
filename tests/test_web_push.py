import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from terminal_web_push import WebPushService,b64,subscription_checked


class SystemPushTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'config.json').write_text(json.dumps(dict(public_origin='https://example.com')))
        self.clock=1000.;self.sender=Mock();self.notices=Mock()
        self.notices.latest_cursors.return_value={'paper':5}
        self.notices.page.return_value=dict(events=[dict(id='paper:6',seq=6,stream='paper',title='买入已模拟成交',origin='A号',name='测试',code='600001',text='100股，价格10.00元，费用1.10元',at='2026-11-14 10:00:00')],cursors={'paper':6},has_more=False)
        self.service=WebPushService(self.root,self.notices,self.sender,now=lambda:self.clock)
        key=ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
        self.subscription=dict(endpoint='https://fcm.googleapis.com/fcm/send/isolated-test',keys=dict(auth=b64(b'a'*16),p256dh=b64(key)))

    def test_key_survives_restart_and_private_key_is_not_exposed(self):
        status=self.service.status();other=WebPushService(self.root,self.notices,self.sender)
        self.assertEqual(status['public_key'],other.status()['public_key']);self.assertNotIn('private',json.dumps(status))
        self.assertEqual((self.root/'vapid-private.pem').stat().st_mode&0o777,0o600)

    def test_subscription_baselines_existing_events_and_reconnect_preserves_cursor(self):
        sid=self.service.subscribe(self.subscription)['id'];self.notices.latest_cursors.assert_called_once_with('live')
        self.service.collect();self.service.collect()
        with self.service.db() as db:
            self.assertEqual(db.execute('select count(*) from deliveries').fetchone()[0],1)
            self.assertEqual(json.loads(db.execute('select cursors from subscriptions').fetchone()[0]),{'paper':6})
        self.service.subscribe(self.subscription)
        with self.service.db() as db:self.assertEqual(json.loads(db.execute('select cursors from subscriptions').fetchone()[0]),{'paper':6})
        self.assertEqual(self.service.status(sid)['subscription']['active'],1)

    def test_delayed_preview_and_actual_delivery_remain_distinct(self):
        sid=self.service.subscribe(self.subscription)['id'];self.service.test(sid)
        self.service.deliver();self.sender.assert_not_called();self.clock+=15;self.service.deliver();self.service.deliver()
        self.sender.assert_called_once();payload=self.sender.call_args.args[1]
        self.assertIn('测试',payload['title']);self.assertIn('未下单',payload['body'])
        with self.service.db() as db:self.assertEqual(db.execute('select status from deliveries').fetchone()[0],'accepted')

    def test_transient_failures_retry_after_restart_without_losing_jobs(self):
        self.service.subscribe(self.subscription);self.service.collect();self.sender.side_effect=TimeoutError()
        self.service.deliver()
        with self.service.db() as db:
            row=dict(db.execute('select * from deliveries').fetchone());self.assertEqual(row['status'],'pending');self.assertEqual(row['attempts'],1)
        self.service.deliver();self.assertEqual(self.sender.call_count,1)
        self.clock+=11;sender=Mock();reopened=WebPushService(self.root,self.notices,sender,now=lambda:self.clock);reopened.deliver();sender.assert_called_once()

    def test_collect_rollback_does_not_advance_cursor_or_leave_half_a_batch(self):
        self.service.subscribe(self.subscription)
        valid=self.notices.page.return_value['events'][0]
        self.notices.page.return_value=dict(events=[valid,dict(id='bad-event')],cursors={'paper':7})
        with self.assertRaises(KeyError):self.service.collect()
        with self.service.db() as db:
            self.assertEqual(db.execute('select count(*) from deliveries').fetchone()[0],0)
            self.assertEqual(json.loads(db.execute('select cursors from subscriptions').fetchone()[0]),{'paper':5})

    def test_gone_subscription_is_disabled_and_not_reported_as_accepted(self):
        class Gone(Exception):response=type('Response',(),{'status_code':410})()
        sid=self.service.subscribe(self.subscription)['id'];self.service.collect();self.sender.side_effect=Gone()
        self.service.deliver();self.assertEqual(self.service.status(sid)['subscription']['active'],0)
        with self.service.db() as db:self.assertEqual(db.execute('select status from deliveries').fetchone()[0],'expired')

    def test_arbitrary_urls_and_invalid_keys_cannot_be_used_for_push(self):
        for url in ['http://fcm.googleapis.com/x','https://127.0.0.1/x','https://fcm.googleapis.com.evil.test/x','https://user@fcm.googleapis.com/x','https://fcm.googleapis.com:8000/x']:
            with self.subTest(url=url),self.assertRaises(ValueError):subscription_checked(dict(self.subscription,endpoint=url))
        with self.assertRaises(ValueError):subscription_checked(dict(self.subscription,keys=dict(auth='bad',p256dh='bad')))

    def test_push_api_requires_the_configured_origin_and_never_starts_a_trading_account(self):
        import os
        from flask import Flask
        from unittest.mock import patch
        from terminal_api import register_terminal
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'TDX_NO_PAPER_WORKER':'1'}):
            root=Path(tmp)/'.runtime/web-push';root.mkdir(parents=True);(root/'config.json').write_text(json.dumps(dict(public_origin='https://example.com')))
            factory=lambda path,notices:WebPushService(path,notices,sender=Mock())
            with patch('terminal_api.WebPushService',side_effect=factory):
                app=Flask('push_test',root_path=tmp);register_terminal(app,lambda:[],lambda _:[])
            client=app.test_client();self.assertTrue(client.get('/api/terminal/system-push').json['data']['configured'])
            self.assertEqual(client.post('/api/terminal/system-push',json=dict(action='subscribe',subscription=self.subscription)).status_code,400)
            headers={'Origin':'https://example.com'}
            response=client.post('/api/terminal/system-push',json=dict(action='subscribe',subscription=self.subscription),headers=headers)
            self.assertEqual(response.status_code,200)
            test=client.post('/api/terminal/system-push',json=dict(action='test',id=response.json['data']['id']),headers=headers);self.assertEqual(test.json['data']['delay_seconds'],15)
            worker=client.get('/service-worker.js');self.assertEqual(worker.status_code,200);self.assertEqual(worker.headers['Service-Worker-Allowed'],'/');worker.close()
            self.assertEqual(client.get('/api/terminal/paper').json['data']['accounts'],[])


if __name__=='__main__':unittest.main()
