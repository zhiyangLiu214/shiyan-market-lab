"""Private strategy configuration transfer; never copies ledgers or enables trading."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from terminal_entry_filter import EntryFilterService, model_id
from terminal_paper import clock
from terminal_strategy_sim import StrategyStore, checked_config

SCHEMA = 'shiyan-strategy-bundle-v1'
MAX_SIZE = 8*1024*1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def normalized(config):
    return json.dumps(checked_config(config), ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def validate_bundle(bundle):
    if not isinstance(bundle, dict) or bundle.get('schema') != SCHEMA:
        raise ValueError('不支持的策略包格式')
    rows, models = bundle.get('strategies'), bundle.get('models')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 50 or not isinstance(models, dict):
        raise ValueError('策略包须包含1至50套策略及模型字典')
    seen, required, clean = set(), set(), []
    for row in rows:
        if not isinstance(row,dict) or row.get('mode') not in ('live','demo'):
            raise ValueError('策略数据模式无效')
        cfg = checked_config(row.get('config'))
        key = (row['mode'], normalized(cfg))
        if key in seen: raise ValueError('策略包存在重复配置')
        seen.add(key)
        if cfg['entry_model']:required.add(cfg['entry_model'])
        clean.append(dict(mode=row['mode'],config=cfg))
    if set(models) != required:
        raise ValueError('策略依赖模型缺失或包含无关模型')
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);(root/'models').mkdir()
        for identifier,record in models.items():
            model_id(identifier)
            if not isinstance(record,dict) or not isinstance(record.get('content'),str):
                raise ValueError('模型格式无效')
            raw=record['content'].encode('utf-8')
            if len(raw)>1024*1024 or digest(raw)!=record.get('sha256'):
                raise ValueError('模型内容哈希校验失败')
            (root/'models'/(identifier+'.json')).write_bytes(raw)
            EntryFilterService(root).load(identifier)
    return clean


def make_bundle(records, model_root):
    entries=[];models={};root=Path(model_root)
    for row in records:
        cfg=checked_config(row['config'])
        entries.append(dict(source_id=row.get('id'),mode=row['mode'],config=cfg))
        identifier=cfg['entry_model']
        if identifier:
            raw=(root/(identifier+'.json')).read_bytes()
            models[identifier]=dict(content=raw.decode('utf-8'),sha256=digest(raw))
    bundle=dict(schema=SCHEMA,created_at=clock(),strategies=entries,models=models,
                scope='仅策略配置与依赖模型；不包含凭证、账户、持仓、委托、成交或运行状态')
    validate_bundle(bundle)
    return bundle


def export_runtime(runtime, ids=None):
    root=Path(runtime);path=root/'strategy-sim/strategies.sqlite3'
    if not path.is_file():raise ValueError('未找到源策略数据库')
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        rows=[dict(r) for r in db.execute("SELECT id,mode,config FROM strategies WHERE mode IN ('live','demo') ORDER BY created,id")]
    if ids:
        rows=[r for r in rows if r['id'] in ids]
        if {r['id'] for r in rows}!=set(ids):raise ValueError('指定的源策略不存在')
    for row in rows:row['config']=json.loads(row['config'])
    return make_bundle(rows,root/'strategy-entry/models')


def write_private(path, bundle):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    raw=json.dumps(bundle,ensure_ascii=False,indent=2).encode('utf-8')
    if len(raw)>MAX_SIZE:raise ValueError('策略包过大')
    # Exclusive creation avoids replacing another file or following a symlink.
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(raw)


def import_bundle(bundle, runtime, apply=False):
    rows=validate_bundle(bundle);root=Path(runtime)
    path=root/'strategy-sim/strategies.sqlite3'
    existing=[]
    if path.exists():
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
            existing=[(r[0],checked_config(json.loads(r[1]))) for r in db.execute("SELECT mode,config FROM strategies WHERE mode IN ('live','demo')")]
    keys={(mode,normalized(cfg)) for mode,cfg in existing}
    additions=[];skipped=[]
    for row in rows:
        key=(row['mode'],normalized(row['config']))
        if key in keys:skipped.append(row['config']['name']);continue
        if any(mode==row['mode'] and cfg['name']==row['config']['name'] for mode,cfg in existing):
            raise ValueError('同名策略配置不同，未覆盖：'+row['config']['name'])
        additions.append(row)
    if len(existing)+len(additions)>50:raise ValueError('目标环境将超过50套策略上限')
    model_root=root/'strategy-entry/models';new_models={}
    for identifier,record in bundle['models'].items():
        target=model_root/(identifier+'.json')
        if target.is_symlink():raise ValueError('模型目标不能是符号链接')
        if target.exists():
            if digest(target.read_bytes())!=record['sha256']:raise ValueError('同名模型内容不同，未覆盖：'+identifier)
        else:new_models[identifier]=record['content'].encode('utf-8')
    summary=dict(applied=apply,created=[r['config']['name'] for r in additions],skipped=skipped,
                 new_models=sorted(new_models),enabled=False)
    if not apply:return summary
    # This utility is intended for a local development instance. The existing
    # strategy/ledger files are never replaced; new rows are inserted paused.
    store=StrategyStore(root/'strategy-sim');written=[]
    try:
        model_root.mkdir(parents=True,exist_ok=True)
        for identifier,raw in new_models.items():
            target=model_root/(identifier+'.json')
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write(raw)
            written.append(target)
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current=[(r['mode'],checked_config(json.loads(r['config']))) for r in db.execute("SELECT mode,config FROM strategies WHERE mode IN ('live','demo')")]
            if sorted((m,normalized(c)) for m,c in current)!=sorted((m,normalized(c)) for m,c in existing):
                raise ValueError('目标策略在导入期间发生变化，请重新预览')
            for row in additions:
                db.execute('INSERT INTO strategies VALUES(?,?,?,0,NULL,?,?,NULL,?)',
                    (uuid.uuid4().hex,row['mode'],json.dumps(row['config'],ensure_ascii=False),clock(),'协作导入，已暂停，未创建历史成交','{}'))
    except Exception:
        for target in written:target.unlink(missing_ok=True)
        raise
    return summary


def main():
    parser=argparse.ArgumentParser(description='开发环境私有策略导出/导入；不传递运行账本')
    sub=parser.add_subparsers(dest='action',required=True)
    export=sub.add_parser('export');export.add_argument('--runtime',default='.runtime');export.add_argument('--output',required=True);export.add_argument('--id',action='append')
    load=sub.add_parser('import');load.add_argument('--runtime',default='.runtime');load.add_argument('--bundle',required=True);load.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    try:
        if args.action=='export':
            bundle=export_runtime(args.runtime,args.id);write_private(args.output,bundle)
            result=dict(exported=len(bundle['strategies']),models=len(bundle['models']),output=args.output)
        else:
            raw=Path(args.bundle).read_bytes()
            if len(raw)>MAX_SIZE:raise ValueError('策略包过大')
            result=import_bundle(json.loads(raw),args.runtime,args.apply)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (ValueError,KeyError,TypeError,OSError,sqlite3.Error) as exc:
        parser.exit(1,'处理失败：'+str(exc)+'\n')


if __name__=='__main__':main()
