"""Private Web Push subscriptions and durable, independent delivery worker."""
import base64
import hashlib
import importlib.util
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse


def b64(value):return base64.urlsafe_b64encode(value).decode().rstrip('=')


def subscription_checked(body):
    if not isinstance(body,dict):raise ValueError('浏览器推送订阅格式无效')
    endpoint=body.get('endpoint','');keys=body.get('keys',{})
    if not isinstance(endpoint,str) or len(endpoint)>4096:raise ValueError('推送地址无效')
    p=urlparse(endpoint);host=p.hostname or ''
    allowed=host=='fcm.googleapis.com' or host=='updates.push.services.mozilla.com' or host.endswith('.push.apple.com')
    if p.scheme!='https' or not allowed or p.username or p.password or p.port not in (None,443) or not p.path or p.fragment:
        raise ValueError('当前浏览器推送服务地址不受支持，请使用Chrome、Firefox或Safari')
    clean={}
    for key,size in [('auth',16),('p256dh',65)]:
        value=keys.get(key) if isinstance(keys,dict) else None
        if not isinstance(value,str) or len(value)>200:raise ValueError('推送加密参数缺失')
        try:raw=base64.b64decode(value.replace('-','+').replace('_','/')+'='*(-len(value)%4),validate=True)
        except Exception:raise ValueError('推送加密参数无效') from None
        if len(raw)!=size or key=='p256dh' and raw[0]!=4:raise ValueError('推送加密参数长度无效')
        if key=='p256dh':
            from cryptography.hazmat.primitives.asymmetric import ec
            try:ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),raw)
            except ValueError:raise ValueError('浏览器推送公钥无效') from None
        clean[key]=value
    return dict(endpoint=endpoint,keys=clean)


class WebPushService:
    def __init__(self,root,notices,sender=None,now=time.time):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.root.chmod(0o700)
        self.notices,self.sender,self.now=notices,sender,now
        self.lock=threading.RLock();self.worker=None;self.wakeup=threading.Event()
        self.last_worker_error=None
        self.origin=os.environ.get('SHIYAN_PUBLIC_ORIGIN','').rstrip('/')
        config=self.root/'config.json'
        if config.exists():
            try:self.origin=json.loads(config.read_text()).get('public_origin',self.origin).rstrip('/')
            except (ValueError,TypeError,AttributeError):self.origin='';self.last_worker_error='系统通知配置无效'
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY,payload TEXT,cursors TEXT,active INTEGER,created REAL,updated REAL,last_success REAL,last_error TEXT);
            CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,subscription TEXT,event TEXT,payload TEXT,status TEXT,attempts INTEGER,next_try REAL,created REAL,last_error TEXT,UNIQUE(subscription,event));''')
        (self.root/'push.sqlite3').chmod(0o600)

    @contextmanager
    def db(self):
        with self.lock:
            c=sqlite3.connect(self.root/'push.sqlite3',timeout=15);c.row_factory=sqlite3.Row
            try:yield c;c.commit()
            except Exception:c.rollback();raise
            finally:c.close()

    def public_key(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        path=self.root/'vapid-private.pem'
        with self.lock:
            if path.exists():key=serialization.load_pem_private_key(path.read_bytes(),password=None)
            else:
                key=ec.generate_private_key(ec.SECP256R1())
                data=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
                fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as f:f.write(data)
            return b64(key.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint))

    def status(self,identifier=None):
        ready=bool(self.sender or importlib.util.find_spec('pywebpush')) and self.origin.startswith('https://')
        result=dict(configured=ready,public_origin=self.origin,public_key=None,worker_error=self.last_worker_error)
        if ready:
            try:result['public_key']=self.public_key()
            except (ValueError,OSError,TypeError):result.update(configured=False,worker_error='推送签名密钥不可用')
        if identifier:
            with self.db() as db:row=db.execute('SELECT active,last_success,last_error FROM subscriptions WHERE id=?',(identifier,)).fetchone()
            result['subscription']=dict(row) if row else None
        return result

    def subscribe(self,body):
        if not self.status()['configured']:raise ValueError('系统推送服务尚未配置HTTPS或发送组件')
        subscription=subscription_checked(body);sid=hashlib.sha256(subscription['endpoint'].encode()).hexdigest();now=self.now()
        with self.db() as db:old=db.execute('SELECT id FROM subscriptions WHERE id=?',(sid,)).fetchone()
        baseline={} if old else self.notices.latest_cursors('live')
        with self.db() as db:
            db.execute('INSERT INTO subscriptions VALUES(?,?,?,?,?,?,NULL,NULL) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,active=1,updated=excluded.updated,last_error=NULL',
                (sid,json.dumps(subscription),json.dumps(baseline),1,now,now))
        self.wakeup.set();return dict(id=sid,subscribed=True)

    def test(self,sid,delay=15):
        with self.db() as db:
            if not db.execute('SELECT 1 FROM subscriptions WHERE id=? AND active=1',(sid,)).fetchone():raise ValueError('当前浏览器还没有启用系统通知')
            event='test:'+uuid.uuid4().hex
            payload=dict(id=event,title='势研 · 系统通知测试',body='【测试预览】这是一条系统桌面通知。未下单、未成交，资金和持仓不变。',url='/?mode=live&page=sim')
            db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,0,?,?,NULL)',(uuid.uuid4().hex,sid,event,json.dumps(payload,ensure_ascii=False),'pending',self.now()+delay,self.now()))
        self.wakeup.set();return dict(queued=True,delay_seconds=delay)

    def collect(self):
        with self.db() as db:subscriptions=[dict(r) for r in db.execute('SELECT * FROM subscriptions WHERE active=1')]
        for sub in subscriptions:
            page=self.notices.page('live',json.loads(sub['cursors']))
            with self.db() as db:
                for event in page['events']:
                    body=' · '.join(filter(None,[event.get('origin'),event.get('name'),event.get('code')]))+'\n'+event.get('text','')+'\n'+event.get('at','')
                    payload=dict(id=event['id'],title='势研 · '+event['title'],body=body[:800],url='/?mode=live&page=sim')
                    db.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?,?,?,0,?,?,NULL)',(uuid.uuid4().hex,sub['id'],event['id'],json.dumps(payload,ensure_ascii=False),'pending',self.now(),self.now()))
                db.execute('UPDATE subscriptions SET cursors=? WHERE id=?',(json.dumps(page['cursors']),sub['id']))

    def send(self,subscription,payload):
        if self.sender:return self.sender(subscription,payload)
        from pywebpush import webpush
        return webpush(subscription_info=subscription,data=json.dumps(payload,ensure_ascii=False),vapid_private_key=str(self.root/'vapid-private.pem'),vapid_claims={'sub':self.origin},ttl=600,timeout=8)

    def deliver(self,limit=10):
        with self.db() as db:
            pending=[dict(r) for r in db.execute("SELECT d.*,s.payload AS subscription_payload FROM deliveries d JOIN subscriptions s ON s.id=d.subscription WHERE d.status='pending' AND d.next_try<=? AND s.active=1 ORDER BY d.created LIMIT ?",(self.now(),limit))]
        for row in pending:
            try:
                self.send(json.loads(row['subscription_payload']),json.loads(row['payload']))
            except Exception as exc:
                response=getattr(exc,'response',None);code=getattr(response,'status_code',None)
                expired=code in (404,410);attempts=row['attempts']+1
                with self.db() as db:
                    db.execute('UPDATE deliveries SET attempts=?,next_try=?,status=?,last_error=? WHERE id=?',(attempts,self.now()+min(300,5*2**min(attempts,6)),'expired' if expired else 'failed' if attempts>=10 else 'pending','推送通道返回'+str(code) if code else '推送通道网络或加密请求失败',row['id']))
                    db.execute('UPDATE subscriptions SET active=?,last_error=? WHERE id=?',(0 if expired else 1,'订阅已失效，请重新启用' if expired else '推送多次未成功，网页未读仍保留，请测试网络' if attempts>=10 else '推送通道暂未接受，等待重试',row['subscription']))
            else:
                with self.db() as db:
                    db.execute("UPDATE deliveries SET status='accepted',attempts=attempts+1,last_error=NULL WHERE id=?",(row['id'],))
                    db.execute('UPDATE subscriptions SET last_success=?,last_error=NULL WHERE id=?',(self.now(),row['subscription']))

    def ensure(self):
        if not self.status()['configured']:return
        with self.lock:
            if self.worker and self.worker.is_alive():return
            def run():
                while True:
                    try:self.collect();self.deliver();self.last_worker_error=None
                    except Exception:self.last_worker_error='系统通知后台核对失败，保留队列等待重试'
                    self.wakeup.wait(5);self.wakeup.clear()
            self.worker=threading.Thread(target=run,daemon=True,name='system-push-notices');self.worker.start()
