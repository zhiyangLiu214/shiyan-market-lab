"""Transactional simulation operation notices and cursor-based delivery."""
import json

NOTICE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS operation_notices(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT UNIQUE,mode TEXT,account TEXT,
 code TEXT,name TEXT,kind TEXT,at TEXT,title TEXT,text TEXT,order_id TEXT,trade_id TEXT,
 qty INTEGER,price REAL,fees REAL,realized REAL,source TEXT);
CREATE INDEX IF NOT EXISTS operation_notice_mode ON operation_notices(mode,seq);
CREATE TRIGGER IF NOT EXISTS ops_order_submitted AFTER INSERT ON orders BEGIN
 INSERT OR IGNORE INTO operation_notices(event_key,mode,account,code,name,kind,at,title,text,order_id,qty,price,source)
 VALUES('order:'||NEW.id||':submitted',NEW.mode,NEW.account,NEW.code,NEW.name,'submitted',NEW.created,
 CASE NEW.side WHEN 'buy' THEN '买入委托已提交' ELSE '卖出委托已提交' END,
 printf('%d股，委托参考价%.2f；等待后续新分钟核对，尚未成交。原因：%s',NEW.qty,NEW.reference,NEW.reason),NEW.id,NEW.qty,NEW.reference,NEW.source);
END;
CREATE TRIGGER IF NOT EXISTS ops_order_status AFTER UPDATE OF status ON orders
 WHEN OLD.status!=NEW.status AND NEW.status IN ('cancelled','rejected','expired') BEGIN
 INSERT OR IGNORE INTO operation_notices(event_key,mode,account,code,name,kind,at,title,text,order_id,qty,price,source)
 VALUES('order:'||NEW.id||':'||NEW.status,NEW.mode,NEW.account,NEW.code,NEW.name,NEW.status,
 strftime('%Y-%m-%d %H:%M:%S','now','+8 hours'),
 (CASE NEW.side WHEN 'buy' THEN '买入' ELSE '卖出' END)||'委托'||(CASE NEW.status WHEN 'cancelled' THEN '已撤销' WHEN 'rejected' THEN '被拒绝' ELSE '已到期' END),
 printf('%d股，未成交。%s',NEW.qty,NEW.result),NEW.id,NEW.qty,NEW.reference,NEW.source);
END;
CREATE TRIGGER IF NOT EXISTS ops_order_wait AFTER UPDATE OF result ON orders
 WHEN OLD.status='pending' AND NEW.status='pending' AND COALESCE(OLD.result,'')!=COALESCE(NEW.result,'') BEGIN
 INSERT OR IGNORE INTO operation_notices(event_key,mode,account,code,name,kind,at,title,text,order_id,qty,price,source)
 VALUES('order:'||NEW.id||':waiting:'||NEW.result,NEW.mode,NEW.account,NEW.code,NEW.name,'waiting',
 strftime('%Y-%m-%d %H:%M:%S','now','+8 hours'),'委托仍在等待成交',
 printf('%s%d股，尚未成交。%s',CASE NEW.side WHEN 'buy' THEN '买入' ELSE '卖出' END,NEW.qty,NEW.result),NEW.id,NEW.qty,NEW.reference,NEW.source);
END;
CREATE TRIGGER IF NOT EXISTS ops_trade_filled AFTER INSERT ON trades BEGIN
 INSERT OR IGNORE INTO operation_notices(event_key,mode,account,code,name,kind,at,title,text,order_id,trade_id,qty,price,fees,realized,source)
 VALUES('trade:'||NEW.id,NEW.mode,NEW.account,NEW.code,NEW.name,NEW.side||'_filled',NEW.at,
 CASE WHEN NEW.side='buy' THEN CASE WHEN (SELECT COALESCE(SUM(qty),0) FROM lots WHERE mode=NEW.mode AND account=NEW.account AND code=NEW.code)>NEW.qty THEN '加仓已模拟成交' ELSE '买入已模拟成交' END
 ELSE CASE WHEN (SELECT COALESCE(SUM(qty),0) FROM lots WHERE mode=NEW.mode AND account=NEW.account AND code=NEW.code)>0 THEN '减仓已模拟成交' ELSE '卖出清仓已模拟成交' END END,
 printf('%d股，实际模拟成交价%.2f，费用%.2f元%s。原因：%s',NEW.qty,NEW.price,NEW.fees,CASE NEW.side WHEN 'sell' THEN printf('，本笔已实现盈亏%.2f元',NEW.realized) ELSE '' END,NEW.reason),
 NEW.order_id,NEW.id,NEW.qty,NEW.price,NEW.fees,NEW.realized,NEW.source);
END;
CREATE TRIGGER IF NOT EXISTS ops_account_started AFTER INSERT ON runs BEGIN
 INSERT OR IGNORE INTO operation_notices(event_key,mode,account,code,name,kind,at,title,text)
 VALUES('run:'||NEW.mode||':'||NEW.started,NEW.mode,'','','','started',NEW.started,'模拟账户已启动','已启动前向模拟，不补填历史成交。');
END;
CREATE TRIGGER IF NOT EXISTS ops_account_toggled AFTER UPDATE OF enabled ON runs WHEN OLD.enabled!=NEW.enabled BEGIN
 INSERT INTO operation_notices(mode,account,code,name,kind,at,title,text)
 VALUES(NEW.mode,'','','',CASE NEW.enabled WHEN 1 THEN 'resumed' ELSE 'paused' END,strftime('%Y-%m-%d %H:%M:%S','now','+8 hours'),
 CASE NEW.enabled WHEN 1 THEN '自动模拟已恢复' ELSE '自动模拟已暂停' END,NEW.status);
END;
CREATE TRIGGER IF NOT EXISTS ops_settings AFTER INSERT ON paper_events BEGIN
 INSERT INTO operation_notices(mode,account,code,name,kind,at,title,text)
 VALUES(NEW.mode,'','','','settings',NEW.at,'模拟账户设置已更新',NEW.text);
END;
'''


class TradeNoticeService:
    def __init__(self,paper,strategies):
        self.paper,self.strategies=paper,strategies

    def sources(self,mode):
        streams=[('paper',self.paper,'模拟交易')]
        with self.strategies.db() as db:
            rows=db.execute('SELECT id,config,mode FROM strategies WHERE mode IN (?,?)',(mode,'archived-'+mode)).fetchall()
        names={row['id']:json.loads(row['config'])['name'] for row in rows}
        for row in rows:
            path=self.strategies.root/(row['id']+'.sqlite3')
            if not path.is_file():continue
            if row['mode']==mode:book=self.strategies.book(row['id'])
            else:
                from terminal_paper import PaperLedger
                book=PaperLedger(path)
            streams.append(('strategy:'+row['id'],book,'策略：'+names[row['id']]))
        return streams,names

    def latest_cursors(self,mode):
        result={};streams,_=self.sources(mode)
        for stream,book,_ in streams:
            with book.db() as db:result[stream]=db.execute('SELECT COALESCE(MAX(seq),0) FROM operation_notices WHERE mode=?',(mode,)).fetchone()[0]
        with self.strategies.db() as db:result['signals']=db.execute('SELECT COALESCE(MAX(rowid),0) FROM notices WHERE mode=?',(mode,)).fetchone()[0]
        return result

    def page(self,mode,cursors=None,limit=100):
        if not isinstance(cursors,dict) or len(cursors)>150:raise ValueError('提醒游标格式无效')
        if any(not isinstance(k,str) or type(v) is not int or not 0<=v<=2**63-1 for k,v in cursors.items()):raise ValueError('提醒游标无效')
        limit=max(1,min(100,limit));events=[];next_cursor=dict(cursors);more=False
        streams,names=self.sources(mode)
        for stream,book,label in streams:
            with book.db() as db:
                page=[dict(r) for r in db.execute('SELECT * FROM operation_notices WHERE mode=? AND seq>? ORDER BY seq LIMIT ?',(mode,cursors.get(stream,0),limit+1))]
            more=more or len(page)>limit
            for row in page[:limit]:
                row.update(id=stream+':'+str(row['seq']),stream=stream,origin=label if stream!='paper' else {'main':'A号·今日主线','scan':'B号·三维选股'}.get(row['account'],label),phase='operation')
                events.append(row);next_cursor[stream]=row['seq']
        with self.strategies.db() as db:
            page=[dict(r) for r in db.execute('SELECT rowid AS seq,* FROM notices WHERE mode=? AND rowid>? ORDER BY rowid LIMIT ?',(mode,cursors.get('signals',0),limit+1))]
        more=more or len(page)>limit
        for row in page[:limit]:
            row.update(id='signals:'+row['id'],stream='signals',origin='策略：'+names.get(row['strategy'],'已归档策略'),title='持仓信号提醒（尚非成交）',phase='signal')
            events.append(row);next_cursor['signals']=row['seq']
        events.sort(key=lambda r:(r['at'],r['stream'],r['seq']))
        return dict(events=events,cursors=next_cursor,has_more=more)
