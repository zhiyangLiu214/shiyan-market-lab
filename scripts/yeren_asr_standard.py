"""Resumable standard ASR submit/query queue; API key stays on this computer."""
import argparse
import hashlib
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from yeren_asr_batch import call, checked_items, write_json, ROOT, SUCCESS, PENDING, summarize_result


def identity(row):
    return hashlib.sha256(('standard:'+row['video_id']+':'+row['audio_sha256']).encode()).hexdigest()


def prepare(row):
    path=Path(row['audio_path']);raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=row['audio_sha256']:raise ValueError('音频校验不一致')
    if len(raw)>512*1024*1024:raise ValueError('音频超过标准版大小限制')
    url=urlsplit(row['audio_url'])
    if url.scheme not in ('https','http') or not url.hostname or url.username or url.password:raise ValueError('音频下载地址无效')
    fmt=row.get('format','mp3')
    if fmt not in ('mp3','m4a','wav','ogg'):raise ValueError('不支持的音频格式')
    return {'user':{'uid':'yeren-notes'},'audio':{'url':row['audio_url'],'format':fmt,'language':'zh-CN'},
            'request':{'model_name':'bigmodel','enable_itn':False,'enable_ddc':False,'enable_punc':True,'show_utterances':True}}


def quota_limit(path):
    q=json.loads(Path(path).read_text())
    if q.get('resource_id')!='volc.seedasr.auc' or not q.get('verified_at') or not 0<=time.time()-q['verified_at']<86400:
        raise ValueError('须核实模型及24小时内的剩余免费额度')
    if q.get('paid_allowed') is not False or not isinstance(q.get('safe_seconds'),(int,float)) or q['safe_seconds']<=0:
        raise ValueError('未核实可用免费额度，未提交')
    return q['safe_seconds']


def already_done(row):
    path=ROOT/'research/yeren/transcripts'/f"episode-{row['episode']:03}.json"
    if not path.exists():return False
    d=json.loads(path.read_text())
    if d.get('video_id')!=row['video_id']:raise ValueError('已有转写来源不同，请先核对')
    if not (d.get('raw_text') or d.get('result',{}).get('text')):raise ValueError('已有文件正文不完整，请先核对')
    return True


def step(rows,key,jobs,journal,limit,send=call,submit=True):
    """One sweep. Persist intent before submission; uncertain submits only query later."""
    for row in rows:
        token=identity(row);job=jobs.get(token)
        if already_done(row):continue
        if not job:
            if not submit:continue
            if sum(j['duration'] for j in jobs.values())+row['duration']>limit:raise ValueError('达到本批安全用量上限，停止新增提交')
            body=prepare(row)
            job=dict(task_id=str(uuid.uuid4()),episode=row['episode'],video_id=row['video_id'],duration=row['duration'],status='submitting')
            jobs[token]=job;write_json(journal,jobs)
            try:code,body_response=send('submit',key,'standard',job['task_id'],body)
            except (OSError,ValueError):
                job['status']='uncertain';write_json(journal,jobs);raise ValueError('提交响应不确定，已保存任务ID；下次只查询，禁止自动重提')
            job['provider_code']=code
            if code!=SUCCESS:
                job['status']='failed';write_json(journal,jobs);raise ValueError('提交失败，已停止新增任务；错误码 '+str(code))
            returned=body_response.get('task_id')
            if returned:job['task_id']=returned
            job['status']='submitted';write_json(journal,jobs);print('已提交第',row['episode'],'集',flush=True)
        if job['status'] in ('failed','complete'):continue
        try:code,body_response=send('query',key,'standard',job['task_id'],{})
        except (OSError,ValueError):continue
        job['provider_code']=code
        if code in PENDING:job['status']='processing'
        elif code==SUCCESS:
            result=summarize_result(body_response)
            path=ROOT/'research/yeren/transcripts'/f"episode-{row['episode']:03}.json"
            if path.exists():raise ValueError('结果文件已出现，未覆盖')
            utterances=result.get('utterances') or []
            write_json(path,dict(episode=row['episode'],video_id=row['video_id'],title=row['title'],source_url=row['source_url'],
                       audio_filename=Path(row['audio_path']).name,processed_full_audio=True,audio_duration_seconds=row['duration'],source_kind=row.get('source_kind','tech_collection'),provider='火山引擎标准版API',resource_id='volc.seedasr.auc',
                       task_id=job['task_id'],verification='transcribed',result=result,raw_text=result['text'],
                       segments=[u['text'] for u in utterances],retrieved_at=time.strftime('%Y-%m-%d'),
                       review_flags=['API转写，尚未核验原声、术语与画面']))
            job['status']='complete';print('已保存第',row['episode'],'集，待核验',flush=True)
        else:
            job['status']='query_error';write_json(journal,jobs)
            raise ValueError('查询异常，保留原任务ID，停止新增提交；错误码 '+str(code))
        write_json(journal,jobs)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('manifest');p.add_argument('--run',action='store_true');p.add_argument('--query-only',action='store_true')
    p.add_argument('--limit',type=int,default=1);p.add_argument('--wait-seconds',type=int,default=50)
    p.add_argument('--key-file',default=str(ROOT/'.runtime/volc-asr.key'));p.add_argument('--quota-file',default=str(ROOT/'.runtime/volc-asr-quota.json'))
    p.add_argument('--journal',default=str(ROOT/'.runtime/yeren-standard-jobs.json'));a=p.parse_args()
    rows=checked_items(json.loads(Path(a.manifest).read_text()));rows=[r for r in rows if not already_done(r)][:a.limit]
    for row in rows:prepare(row)
    print('待处理',len(rows),'集；总时长',round(sum(r['duration'] for r in rows),2),'秒',flush=True)
    if not a.run:print('检查模式，未发送API请求');return
    limit=quota_limit(a.quota_file);key=Path(a.key_file).read_text().strip()
    if not key or any(c.isspace() for c in key):raise ValueError('密钥文件格式无效')
    journal=Path(a.journal);journal.parent.mkdir(parents=True,exist_ok=True)
    import fcntl
    with journal.with_suffix('.lock').open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('同一队列正在运行，禁止并发重复提交')
        jobs=json.loads(journal.read_text()) if journal.exists() else {};deadline=time.monotonic()+a.wait_seconds
        while True:
            step(rows,key,jobs,journal,limit,submit=not a.query_only)
            if all(already_done(r) for r in rows):break
            if time.monotonic()>=deadline:print('任务处理中，已保留ID，下次运行继续查询',flush=True);break
            time.sleep(5)

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError) as exc:raise SystemExit(str(exc))
