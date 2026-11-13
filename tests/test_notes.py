import json
import tempfile
import unittest
from pathlib import Path
from terminal_notes import NoteStore, NoteConflict

class NotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'notes.db';self.catalog=Path(self.tmp.name)/'catalog.json'
        self.catalog.write_text(json.dumps(dict(episodes=[dict(episode=1,title='目录标题')],url='https://www.douyin.com/collection/1')))
        self.store=NoteStore(self.path,self.catalog)
    def payload(self,**kwargs):
        return dict(title='测试笔记',category='交易模式',verification='personal',summary='个人理解',**kwargs)
    def test_catalogue_is_not_verified_content(self):
        data=self.store.list();self.assertEqual(data['collection']['verified'],0)
        self.assertEqual(data['items'][0]['summary'],'');self.assertEqual(data['items'][0]['verification'],'catalog')
    def test_seed_preserves_edits_and_archive_is_reversible(self):
        n=self.store.list()['items'][0];n.update(summary='已整理',verification='transcribed')
        saved=self.store.save(n);NoteStore(self.path,self.catalog)
        self.assertEqual(self.store.list()['items'][0]['summary'],'已整理')
        archived=self.store.archive(saved['id'],saved['revision'],True)
        restored=self.store.archive(archived['id'],archived['revision'],False)
        self.assertFalse(restored['archived'])
    def test_stale_revision_rejected(self):
        n=self.store.save(self.payload());self.store.save(n)
        with self.assertRaises(NoteConflict):self.store.save(n)
        with self.assertRaises(NoteConflict):self.store.archive(n['id'],n['revision'],True)
    def test_verified_requires_evidence_and_links_are_safe(self):
        n=self.payload();n['verification']='verified'
        with self.assertRaises(ValueError):self.store.save(n)
        for url in ['javascript:alert(1)','https://user:secret@example.com','https://example.com/\nfoo']:
            n['source_url']=url
            with self.assertRaises(ValueError):self.store.save(n)
        n['source_url']='https://www.douyin.com/video/123';saved=self.store.save(n)
        self.assertEqual(saved['verification'],'verified')
    def test_persistence_without_market_or_mode(self):
        saved=self.store.save(self.payload());second=NoteStore(self.path,self.catalog)
        self.assertEqual(next(n for n in second.list()['items'] if n['id']==saved['id'])['summary'],'个人理解')

    def test_catalogue_enriches_sources_without_overwriting_notes(self):
        d=json.loads(self.catalog.read_text());d['episodes'][0]['url']='https://www.douyin.com/video/123';self.catalog.write_text(json.dumps(d))
        second=NoteStore(self.path,self.catalog);n=second.list()['items'][0]
        self.assertEqual(n['source_url'],'https://www.douyin.com/video/123')
        n['source_url']='https://example.com/my-source';second.save(n)
        self.assertEqual(NoteStore(self.path,self.catalog).list()['items'][0]['source_url'],'https://example.com/my-source')
