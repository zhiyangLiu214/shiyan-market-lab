"""Source-linked research notes, stored separately from trading accounts."""
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from terminal_engine import now_text

CATEGORIES=('待分类','交易模式','主线与龙头','补涨与趋势','仓位管理','盈亏比与风控','交易纪律')
STATUSES={'catalog':'目录待提取','transcribed':'转写待核验','verified':'原声已核对','personal':'个人笔记'}
FIELDS=('title','category','summary','conditions','actions','invalidation','risk','source_url','time_range','verification','tags')

class NoteConflict(ValueError):pass

class NoteStore:
    def __init__(self,path,catalog=None):
        self.path=str(path);Path(path).parent.mkdir(parents=True,exist_ok=True);self.lock=threading.RLock()
        self.catalog=Path(catalog) if catalog else None
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY,title TEXT,category TEXT,summary TEXT,
                conditions TEXT,actions TEXT,invalidation TEXT,risk TEXT,source_url TEXT,time_range TEXT,
                verification TEXT,tags TEXT,episode INTEGER,created TEXT,updated TEXT,revision INTEGER,archived INTEGER DEFAULT 0)''')
            db.execute('''CREATE TABLE IF NOT EXISTS note_versions(id TEXT,revision INTEGER,at TEXT,payload TEXT,
                PRIMARY KEY(id,revision))''')
        self.seed_catalog()

    @contextmanager
    def db(self):
        with self.lock:
            conn=sqlite3.connect(self.path,timeout=15);conn.row_factory=sqlite3.Row
            try:
                conn.execute('BEGIN IMMEDIATE');yield conn;conn.commit()
            except Exception:conn.rollback();raise
            finally:conn.close()

    def catalog_data(self):
        try:
            data=json.loads(self.catalog.read_text()) if self.catalog else {}
            return data if isinstance(data,dict) else {}
        except (OSError,ValueError):return {}

    def seed_catalog(self):
        catalog=self.catalog_data();stamp=now_text()
        with self.db() as db:
            for episode in catalog.get('episodes',[]):
                number=episode.get('episode');title=episode.get('title')
                if not isinstance(number,int) or not 1<=number<=59 or not isinstance(title,str):continue
                row=dict(id=f'catalog-{number:03}',title=title[:300],category='待分类',summary='',conditions='',
                         actions='',invalidation='',risk='',source_url=episode.get('url') or catalog.get('url',''),
                         time_range='',verification='catalog',tags='聊技术',episode=number,created=stamp,updated=stamp,revision=1,archived=0)
                db.execute('INSERT OR IGNORE INTO notes VALUES('+','.join('?' for _ in row)+')',list(row.values()))
                old=db.execute('SELECT * FROM notes WHERE id=?',(row['id'],)).fetchone()
                if (old and old['verification']=='catalog' and not old['summary'] and
                    old['source_url'] in ('',catalog.get('url','')) and row['source_url']!=old['source_url']):
                    db.execute('INSERT OR IGNORE INTO note_versions VALUES(?,?,?,?)',
                               (old['id'],old['revision'],stamp,json.dumps(dict(old),ensure_ascii=False)))
                    db.execute('UPDATE notes SET source_url=?,revision=revision+1,updated=? WHERE id=?',
                               (row['source_url'],stamp,old['id']))


    def list(self):
        with self.db() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM notes ORDER BY COALESCE(episode,9999),updated DESC')]
        catalog=self.catalog_data()
        return dict(items=rows,categories=CATEGORIES,statuses=STATUSES,collection=dict(title='聊技术',total=59,
                    url=catalog.get('url','https://www.douyin.com/collection/7526865577185380404/1'),
                    catalogued=len(catalog.get('episodes',[])),observed_at=catalog.get('observed_at'),
                    transcribed=sum(r['verification'] in ('transcribed','verified') and r['episode'] is not None and not r['archived'] for r in rows),
                    verified=sum(r['verification']=='verified' and r['episode'] is not None and not r['archived'] for r in rows)))

    def save(self,data):
        if not isinstance(data,dict):raise ValueError('笔记格式无效')
        cleaned={}
        for key in FIELDS:
            value=data.get(key,'')
            if not isinstance(value,str):raise ValueError('笔记字段须为文本')
            limit=12000 if key=='summary' else 6000
            if len(value)>limit:raise ValueError('单个字段过长，请分条记录')
            cleaned[key]=value.strip()
        if not cleaned['title'] or len(cleaned['title'])>300:raise ValueError('标题须为1～300字')
        if cleaned['category'] not in CATEGORIES:raise ValueError('请选择有效分类')
        if cleaned['verification'] not in STATUSES:raise ValueError('请选择有效核验状态')
        url=cleaned['source_url']
        if url:
            u=urlsplit(url)
            if len(url)>2048 or u.scheme not in ('http','https') or not u.hostname or u.username or u.password or any(ord(c)<32 for c in url):raise ValueError('来源须为有效HTTP/HTTPS链接')
        if cleaned['verification']=='verified' and (not url or not cleaned['summary']):raise ValueError('标记原声已核对前，请填写来源链接和内容要点')
        stamp=now_text();note_id=data.get('id') or uuid.uuid4().hex
        if not isinstance(note_id,str) or len(note_id)>100:raise ValueError('笔记编号无效')
        with self.db() as db:
            old=db.execute('SELECT * FROM notes WHERE id=?',(note_id,)).fetchone()
            if old:
                if data.get('revision')!=old['revision']:raise NoteConflict('这条笔记已在其他页面更新，请重新载入后再保存')
                db.execute('INSERT OR IGNORE INTO note_versions VALUES(?,?,?,?)',(note_id,old['revision'],stamp,json.dumps(dict(old),ensure_ascii=False)))
                db.execute('UPDATE notes SET '+','.join(k+'=?' for k in FIELDS)+',updated=?,revision=revision+1 WHERE id=?',list(cleaned.values())+[stamp,note_id])
            else:
                db.execute('INSERT INTO notes VALUES('+','.join('?' for _ in range(17))+')',[note_id]+list(cleaned.values())+[None,stamp,stamp,1,0])
            return dict(db.execute('SELECT * FROM notes WHERE id=?',(note_id,)).fetchone())

    def archive(self,note_id,revision,archived):
        if not isinstance(archived,bool):raise ValueError('归档状态无效')
        with self.db() as db:
            old=db.execute('SELECT * FROM notes WHERE id=?',(note_id,)).fetchone()
            if not old:raise ValueError('笔记不存在')
            if old['revision']!=revision:raise NoteConflict('笔记已更新，请重新载入')
            stamp=now_text()
            db.execute('INSERT OR IGNORE INTO note_versions VALUES(?,?,?,?)',(note_id,old['revision'],stamp,json.dumps(dict(old),ensure_ascii=False)))
            db.execute('UPDATE notes SET archived=?,revision=revision+1,updated=? WHERE id=?',(int(archived),stamp,note_id))
            return dict(db.execute('SELECT * FROM notes WHERE id=?',(note_id,)).fetchone())
