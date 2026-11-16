"""Publish immutable research inputs and exchange results through MySQL."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ALLOWED_DIRS = {'daily', 'bars', 'ticks', 'actions', 'dividends'}
ALLOWED_FILES = {'roster.json', 'strategies.json'}
MAX_FILE = 32 * 1024 * 1024


def checked_path(value):
    path = PurePosixPath(value)
    if (path.is_absolute() or '..' in path.parts or not path.parts or
            not (path.parts[0] in ALLOWED_DIRS and len(path.parts) == 2 and path.suffix == '.json'
                 or len(path.parts) == 1 and value in ALLOWED_FILES)):
        raise ValueError('不允许共享此路径: ' + value)
    return value


def file_record(root, path):
    root = Path(root).resolve()
    relative = checked_path(path.relative_to(root).as_posix())
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
        raise ValueError('文件不是普通 JSON 或超过 32 MiB: ' + relative)
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, (dict, list)):
        raise ValueError('数据文件不是 JSON 对象或数组: ' + relative)
    metadata = payload if isinstance(payload, dict) else {}
    return (relative, hashlib.sha256(raw).hexdigest(), metadata.get('source'),
            metadata.get('source_time') or metadata.get('date'), metadata.get('retrieved_at'), raw)


def candidates(root):
    root = Path(root).resolve()
    paths = []
    for name in sorted(ALLOWED_FILES):
        path = root / name
        if path.exists():
            paths.append(path)
    for name in sorted(ALLOWED_DIRS):
        folder = root / name
        if folder.is_symlink():
            raise ValueError('数据目录不能是符号链接: ' + name)
        if folder.exists():
            paths.extend(folder.glob('*.json'))
    yield from sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def connection():
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError('先安装 python -m pip install PyMySQL') from exc
    password_file = Path(os.environ.get('TDX_RESEARCH_PASSWORD_FILE', ''))
    if not password_file.is_file() or password_file.stat().st_mode & 0o077:
        raise ValueError('需设置权限为 0600 的 TDX_RESEARCH_PASSWORD_FILE')
    return pymysql.connect(
        host=os.environ.get('TDX_RESEARCH_HOST', '127.0.0.1'),
        port=int(os.environ.get('TDX_RESEARCH_PORT', '13306')),
        user=os.environ['TDX_RESEARCH_USER'],
        password=password_file.read_text().strip(),
        database='shiyan_research', charset='utf8mb4', autocommit=False)


def publish(root, name):
    records = list(candidates(root))
    if not records:
        raise ValueError('没有可共享的 JSON 数据文件')
    dataset_id = str(uuid.uuid4())
    manifest = hashlib.sha256()
    db = connection()
    try:
        with db.cursor() as cur:
            cur.execute('INSERT INTO datasets (id,name,kind,created_by) VALUES (%s,%s,%s,USER())',
                        (dataset_id, name, 'historical-cache'))
        db.commit()
        try:
            for index, path in enumerate(records, 1):
                relative, sha, source, source_time, retrieved_at, raw = file_record(Path(root).resolve(), path)
                manifest.update((relative + '\0' + sha + '\n').encode())
                with db.cursor() as cur:
                    cur.execute('INSERT INTO files VALUES (%s,%s,%s,%s,%s,%s,%s)',
                                (dataset_id, relative, sha, source, source_time, retrieved_at, raw))
                if index % 100 == 0:
                    db.commit()
                    print(f'{index}/{len(records)}', flush=True)
            with db.cursor() as cur:
                cur.execute('UPDATE datasets SET sealed_at=now(), manifest_sha256=%s WHERE id=%s',
                            (manifest.hexdigest(), dataset_id))
            db.commit()
        except Exception:
            db.rollback()
            with db.cursor() as cur:
                cur.execute('DELETE FROM files WHERE dataset_id=%s', (dataset_id,))
                cur.execute('DELETE FROM datasets WHERE id=%s', (dataset_id,))
            db.commit()
            raise
    finally:
        db.close()
    return {'id': dataset_id, 'files': len(records), 'manifest_sha256': manifest.hexdigest()}


def checkout(dataset_id, destination):
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    count = 0
    db = connection()
    try:
        with db.cursor() as cur:
            cur.execute('SELECT sealed_at, manifest_sha256 FROM datasets WHERE id=%s', (dataset_id,))
            row = cur.fetchone()
        if not row or not all(row):
            raise ValueError('数据集不存在或尚未封存')
        manifest = hashlib.sha256()
        import pymysql
        with db.cursor(pymysql.cursors.SSCursor) as cur:
            cur.execute('SELECT path,sha256,content FROM files WHERE dataset_id=%s ORDER BY path', (dataset_id,))
            for relative, sha, content in cur:
                relative = checked_path(relative)
                raw = bytes(content)
                if hashlib.sha256(raw).hexdigest() != sha:
                    raise ValueError('数据库文件哈希不符: ' + relative)
                manifest.update((relative + '\0' + sha + '\n').encode())
                target = root / relative
                if target.exists():
                    if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                        raise ValueError('本地已有不同内容，停止覆盖: ' + relative)
                else:
                    if target.parent.is_symlink():
                        raise ValueError('目标目录不能是符号链接: ' + relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(raw)
                count += 1
        if manifest.hexdigest() != row[1]:
            raise ValueError('数据集清单哈希不符')
    finally:
        db.close()
    return {'files': count, 'destination': str(root)}


def upload_run(dataset_id, report_path):
    report = json.loads(Path(report_path).read_text())
    if not isinstance(report, dict) or report.get('status') not in ('completed', 'partial'):
        raise ValueError('仅可上传已完成或部分完成的回测报告')
    run_id = str(uuid.uuid4())
    db = connection()
    try:
        with db.cursor() as cur:
            cur.execute('CALL add_own_run(%s,%s,%s)',
                        (run_id, dataset_id, json.dumps(report, ensure_ascii=False)))
        db.commit()
    finally:
        db.close()
    return {'id': run_id, 'owner': os.environ['TDX_RESEARCH_USER']}


def publish_strategies(bundle_path, name):
    from tools.strategy_transfer import MAX_SIZE, validate_bundle
    raw = Path(bundle_path).read_bytes()
    if len(raw) > MAX_SIZE:
        raise ValueError('策略包过大')
    rows = validate_bundle(json.loads(raw))
    bundle_id = str(uuid.uuid4())
    sha = hashlib.sha256(raw).hexdigest()
    db = connection()
    try:
        with db.cursor() as cur:
            cur.execute('INSERT INTO strategy_bundles (id,name,sha256,content) VALUES (%s,%s,%s,%s)',
                        (bundle_id, name, sha, raw))
        db.commit()
    finally:
        db.close()
    return {'id': bundle_id, 'strategies': len(rows), 'sha256': sha}


def download_strategies(bundle_id, output):
    from tools.strategy_transfer import validate_bundle
    db = connection()
    try:
        with db.cursor() as cur:
            cur.execute('SELECT sha256,content FROM strategy_bundles WHERE id=%s', (bundle_id,))
            row = cur.fetchone()
    finally:
        db.close()
    if not row:
        raise ValueError('未找到策略包')
    sha, raw = row
    raw = bytes(raw)
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError('策略包哈希不符')
    validate_bundle(json.loads(raw))
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
    return {'output': str(path), 'sha256': sha}


def list_datasets():
    db = connection()
    try:
        with db.cursor() as cur:
            cur.execute('SELECT id,name,kind,sealed_at,manifest_sha256 FROM datasets ORDER BY created_at DESC')
            return [dict(zip(('id', 'name', 'kind', 'sealed_at', 'manifest_sha256'), map(str, row))) for row in cur]
    finally:
        db.close()


def list_bundles():
    db = connection()
    try:
        with db.cursor() as cur:
            cur.execute('SELECT id,name,created_at,sha256 FROM strategy_bundles ORDER BY created_at DESC')
            return [dict(zip(('id', 'name', 'created_at', 'sha256'), map(str, row))) for row in cur]
    finally:
        db.close()


def list_runs():
    db = connection()
    try:
        with db.cursor() as cur:
            cur.execute('SELECT id,dataset_id,created_at FROM own_runs ORDER BY created_at DESC')
            return [dict(zip(('id', 'dataset_id', 'created_at'), map(str, row))) for row in cur]
    finally:
        db.close()


def download_run(run_id, output):
    db = connection()
    try:
        with db.cursor() as cur:
            cur.execute('SELECT report FROM own_runs WHERE id=%s', (run_id,))
            row = cur.fetchone()
    finally:
        db.close()
    if not row:
        raise ValueError('未找到属于当前账号的回测结果')
    raw = row[0].encode('utf-8') if isinstance(row[0], str) else bytes(row[0])
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
    return {'output': str(path), 'bytes': len(raw)}


def main():
    parser = argparse.ArgumentParser(description='共享研究数据集与回测报告；通过 SSH 隧道连接 MySQL')
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('publish'); p.add_argument('--root', required=True); p.add_argument('--name', required=True)
    p = sub.add_parser('checkout'); p.add_argument('--dataset', required=True); p.add_argument('--destination', required=True)
    p = sub.add_parser('upload-run'); p.add_argument('--dataset', required=True); p.add_argument('--report', required=True)
    p = sub.add_parser('publish-strategies'); p.add_argument('--bundle', required=True); p.add_argument('--name', required=True)
    p = sub.add_parser('download-strategies'); p.add_argument('--bundle-id', required=True); p.add_argument('--output', required=True)
    p = sub.add_parser('download-run'); p.add_argument('--run-id', required=True); p.add_argument('--output', required=True)
    sub.add_parser('list')
    sub.add_parser('list-bundles')
    sub.add_parser('list-runs')
    args = parser.parse_args()
    try:
        result = (publish(args.root, args.name) if args.action == 'publish' else
                  checkout(args.dataset, args.destination) if args.action == 'checkout' else
                  upload_run(args.dataset, args.report) if args.action == 'upload-run' else
                  publish_strategies(args.bundle, args.name) if args.action == 'publish-strategies' else
                  download_strategies(args.bundle_id, args.output) if args.action == 'download-strategies' else
                  download_run(args.run_id, args.output) if args.action == 'download-run' else
                  list_bundles() if args.action == 'list-bundles' else
                  list_runs() if args.action == 'list-runs' else list_datasets())
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        parser.exit(1, '共享失败: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
