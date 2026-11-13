import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from terminal_market_cache import MarketCache
from terminal_funds import FundFlowService
import requests

class MarketCacheTests(unittest.TestCase):
    def test_single_flight_cold_load_does_not_block_request(self):
        gate=threading.Event();entered=threading.Event()
        def load(demo):entered.set();gate.wait(3);return {'groups':[],'updated_at':'2026-11-11 15:00:00'}
        loader=Mock(side_effect=load);cache=MarketCache(loader)
        self.assertTrue(cache.get()['loading']);self.assertTrue(entered.is_set())
        self.assertTrue(cache.get()['loading']);self.assertEqual(loader.call_count,1)
        thread=cache.states[False]['running'];gate.set();thread.join(2)
        self.assertNotIn('loading',cache.get())
    def test_failure_keeps_original_time_and_respects_retry_backoff(self):
        tick=[100];gate=threading.Event();loader=Mock(return_value={'groups':[],'updated_at':'2026-11-11 15:00:00'})
        cache=MarketCache(loader,now=lambda:tick[0]);cache.get();tick[0]=800
        loader.side_effect=RuntimeError('network timeout')
        old=cache.get();self.assertTrue(old['stale']);self.assertEqual(old['updated_at'],'2026-11-11 15:00:00')
        cache.get();self.assertEqual(loader.call_count,2)
    def test_persisted_snapshot_is_stale_after_restart_and_demo_is_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot={'groups':[],'updated_at':'2026-11-11 15:00:00','demo':False}
            Path(tmp,'market-live.json').write_text(json.dumps(snapshot))
            loader=Mock(side_effect=RuntimeError('offline'));cache=MarketCache(loader,tmp)
            result=cache.get();self.assertTrue(result['stale']);self.assertEqual(result['updated_at'],snapshot['updated_at'])
            demo=cache.get(True);self.assertTrue(demo['loading']);self.assertNotIn('demo',demo)
    def test_retry_timeout_once_but_never_retry_forbidden(self):
        response=Mock();response.json.return_value={'data':{}};response.raise_for_status.return_value=None
        session=Mock();session.get.side_effect=[requests.ReadTimeout('slow'),response]
        FundFlowService._network_local.session=session
        service=FundFlowService()
        with patch('terminal_funds.time.sleep'):
            self.assertEqual(service._json('https://example.com',{}),{'data':{}})
        self.assertEqual(session.get.call_count,2);self.assertEqual(session.get.call_args.kwargs['timeout'],(5,12))
        session.get.reset_mock();session.get.side_effect=requests.HTTPError('403')
        with self.assertRaises(requests.HTTPError):service._json('https://example.com',{})
        self.assertEqual(session.get.call_count,1)
        del FundFlowService._network_local.session

if __name__=='__main__':unittest.main()
