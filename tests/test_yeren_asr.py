import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import yeren_asr_standard as asr

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.addCleanup(patch.stopall);patch.object(asr,'ROOT',root).start()
        (root/'research/yeren/transcripts').mkdir(parents=True);self.audio=root/'audio.mp3';self.audio.write_bytes(b'audio')
        self.row=dict(episode=41,video_id='123',title='测试',duration=5,source_url='https://www.douyin.com/video/123',audio_path=str(self.audio),audio_sha256=hashlib.sha256(b'audio').hexdigest(),audio_url='http://example.com/a.mp3')
        self.jobs={};self.journal=root/'jobs.json'
    def test_author_works_need_verified_author_and_distinct_number_range(self):
        from yeren_asr_batch import checked_items
        row=dict(self.row,episode=60,source_checked=True,source_kind='author_works',author_uid='86057203756')
        self.assertEqual(checked_items([row]),[row])
        with self.assertRaises(ValueError):checked_items([dict(row,author_uid='other')])
        with self.assertRaises(ValueError):checked_items([dict(row,source_kind='tech_collection')])

    def test_restarts_query_instead_of_resubmit(self):
        calls=[]
        def send(path,*args):
            calls.append(path);return ('20000000',{'task_id':'returned-id'}) if path=='submit' else ('20000001',{})
        for _ in range(2):asr.step([self.row],'key',self.jobs,self.journal,10,send)
        self.assertEqual(calls.count('submit'),1);self.assertEqual(calls.count('query'),2)
        self.assertEqual(next(iter(self.jobs.values()))['task_id'],'returned-id')
    def test_uncertain_submit_is_never_automatically_repeated(self):
        def lost(*args):raise OSError('timeout')
        with self.assertRaises(ValueError):asr.step([self.row],'key',self.jobs,self.journal,10,lost)
        def query(path,*args):self.assertEqual(path,'query');return '20000001',{}
        asr.step([self.row],'key',self.jobs,self.journal,10,query)
    def test_quota_and_audio_integrity_prevent_submission(self):
        with self.assertRaises(ValueError):asr.step([self.row],'key',self.jobs,self.journal,1)
        self.audio.write_bytes(b'changed')
        with self.assertRaises(ValueError):asr.prepare(self.row)
    def test_completed_results_are_saved_and_not_overwritten(self):
        def send(path,*args):return ('20000000',{}) if path=='submit' else ('20000000',{'result':{'text':'正文','utterances':[{'text':'正文','start_time':0,'end_time':100}]}})
        asr.step([self.row],'key',self.jobs,self.journal,10,send)
        self.assertTrue(asr.already_done(self.row))
        def unexpected(*args):raise AssertionError('must skip completed')
        asr.step([self.row],'key',self.jobs,self.journal,10,unexpected)
