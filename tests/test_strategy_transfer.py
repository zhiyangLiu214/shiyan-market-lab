import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from terminal_strategy_sim import StrategyStore
from tests.test_strategy_sim import config
from tools.strategy_transfer import export_runtime, import_bundle, make_bundle, validate_bundle, write_private


class StrategyTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'source';self.target=self.root/'target'
        self.store=StrategyStore(self.source/'strategy-sim')
        self.model=dict(version='entry-model-v1',features=[dict(feature='volume_ratio',lower=0,upper=5,center=1,scale=1)],return_coefficients=[0,1],stop_coefficients=[0,0],rise_coefficients=[0,0])
        models=self.source/'strategy-entry/models';models.mkdir(parents=True)
        self.model_raw=json.dumps(dict(id='test-model',label='测试模型',model=self.model))
        (models/'test-model.json').write_text(self.model_raw)
        self.cfg=dict(config(),entry_model='test-model',boards=['main'],buy_time='14:30')
        self.item=self.store.create(self.cfg,'live')
        with self.store.db() as db:db.execute('UPDATE strategies SET enabled=1,started=?,result=?',('2026-11-13','{"private_state":"do not copy"}'))

    def bundle(self):return export_runtime(self.source)

    def test_bundle_contains_config_models_not_account_or_execution_state(self):
        bundle=self.bundle();text=json.dumps(bundle)
        self.assertEqual(len(bundle['strategies']),1);self.assertEqual(len(bundle['models']),1)
        self.assertNotIn('private_state',text);self.assertNotIn('started',bundle['strategies'][0])
        self.assertNotIn('enabled',bundle['strategies'][0]);self.assertNotIn('ledger',text)
        self.assertEqual(bundle['models']['test-model']['content'],self.model_raw)

    def test_preview_creates_no_target_and_apply_imports_paused_with_new_id(self):
        bundle=self.bundle();preview=import_bundle(bundle,self.target)
        self.assertFalse(preview['applied']);self.assertFalse(self.target.exists())
        summary=import_bundle(bundle,self.target,True);self.assertFalse(summary['enabled'])
        target=StrategyStore(self.target/'strategy-sim');items=target.list('live')
        self.assertEqual(len(items),1);item=items[0]
        self.assertNotEqual(item['id'],self.item['id']);self.assertFalse(item['enabled'])
        self.assertIsNone(item['started']);self.assertEqual(item['result'],{})
        self.assertEqual(item['config'],self.store.get(self.item['id'])['config'])
        self.assertFalse(list((self.target/'strategy-sim').glob('[0-9a-f]'*32+'.sqlite3')))
        self.assertEqual((self.target/'strategy-entry/models/test-model.json').read_text(),self.model_raw)

    def test_reimport_is_idempotent_and_does_not_pause_existing_enabled_strategy(self):
        bundle=self.bundle();import_bundle(bundle,self.target,True)
        store=StrategyStore(self.target/'strategy-sim')
        with store.db() as db:db.execute('UPDATE strategies SET enabled=1')
        again=import_bundle(bundle,self.target,True)
        self.assertEqual(again['created'],[]);self.assertEqual(len(again['skipped']),1)
        self.assertEqual(len(store.list('live')),1);self.assertTrue(store.list('live')[0]['enabled'])

    def test_same_name_different_config_is_rejected_without_changes(self):
        bundle=self.bundle();target=StrategyStore(self.target/'strategy-sim')
        changed=dict(self.cfg,formula='C>MA(C,20)');item=target.create(changed,'live')
        with self.assertRaises(ValueError):import_bundle(bundle,self.target,True)
        self.assertEqual(target.get(item['id'])['config']['formula'],changed['formula'])
        self.assertFalse((self.target/'strategy-entry').exists())

    def test_missing_model_tamper_and_path_traversal_rejected_before_writes(self):
        for mutation in ('missing','tamper','path'):
            bundle=self.bundle()
            if mutation=='missing':bundle['models']={}
            elif mutation=='tamper':bundle['models']['test-model']['content']+=' '
            else:bundle['strategies'][0]['config']['entry_model']='../outside'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):import_bundle(bundle,self.target,True)
            self.assertFalse(self.target.exists())

    def test_model_name_conflict_does_not_replace_local_file(self):
        bundle=self.bundle();path=self.target/'strategy-entry/models/test-model.json';path.parent.mkdir(parents=True);path.write_text('{}')
        with self.assertRaises(ValueError):import_bundle(bundle,self.target,True)
        self.assertEqual(path.read_text(),'{}');self.assertFalse((self.target/'strategy-sim').exists())

    def test_export_does_not_mutate_source_database_and_selected_id_checked(self):
        before=(self.source/'strategy-sim/strategies.sqlite3').read_bytes()
        export_runtime(self.source,[self.item['id']])
        self.assertEqual(before,(self.source/'strategy-sim/strategies.sqlite3').read_bytes())
        with self.assertRaises(ValueError):export_runtime(self.source,['0'*32])

    def test_private_output_is_exclusive_and_owner_read_write(self):
        path=self.root/'private.strategy-bundle.json';write_private(path,self.bundle())
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError):write_private(path,self.bundle())

    def test_unknown_execution_fields_cannot_enable_import(self):
        bundle=self.bundle();bundle['strategies'][0].update(enabled=True,started='today',ledger={'cash':999999})
        import_bundle(bundle,self.target,True)
        item=StrategyStore(self.target/'strategy-sim').list('live')[0]
        self.assertFalse(item['enabled']);self.assertIsNone(item['started']);self.assertEqual(item['result'],{})

    def test_failed_multi_strategy_import_rolls_back_rows_and_new_models(self):
        second=dict(self.cfg,name='中断策略')
        self.store.create(second,'live');bundle=self.bundle()
        target=StrategyStore(self.target/'strategy-sim')
        with target.db() as db:
            db.execute("CREATE TRIGGER reject_second BEFORE INSERT ON strategies WHEN NEW.config LIKE '%中断策略%' BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):import_bundle(bundle,self.target,True)
        self.assertEqual(target.list('live'),[])
        self.assertFalse((self.target/'strategy-entry/models/test-model.json').exists())
