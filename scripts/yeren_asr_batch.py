"""Volcengine ASR batch queue. No browser credentials, no automatic note verification.
Official API: https://docs.volcengine.com/docs/doubao-voice/task-submission-http-1
Default is a dry run; paid use is not enabled by this tool.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
API='https://openspeech.bytedance.com/api/v3/auc/bigmodel/'
MODELS={'standard':'volc.seedasr.auc','flash':'volc.bigasr.auc_turbo'}
SUCCESS='20000000'
PENDING={'20000001','20000002'}


def write_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));os.replace(tmp,path)


def checked_items(manifest):
    items=[];seen=set()
    for row in manifest:
        if not isinstance(row,dict) or not row.get('video_id') or row['video_id'] in seen:raise ValueError('视频编号缺失或重复')
        if not row.get('title') or not row.get('source_checked'):raise ValueError('须先核实标题、集数与视频来源')
        if not isinstance(row.get('duration'),(int,float)) or not 0<row['duration']<=18000:raise ValueError('须提供实际媒体时长')
        ep=row.get('episode')
        valid_episode=isinstance(ep,int) and (60<=ep<=100000 and row.get('author_uid')=='86057203756' if row.get('source_kind')=='author_works' else 1<=ep<=59)
        if not valid_episode:raise ValueError('视频目录编号或作者来源无效')
        seen.add(row['video_id']);items.append(row)
    return items


def signature(row,model):
    material={k:row.get(k) for k in ('video_id','duration','audio_sha256','audio_url')}
    return hashlib.sha256(json.dumps([model,material],sort_keys=True).encode()).hexdigest()


def headers(key,model,task_id):
    return {'Content-Type':'application/json','X-Api-Key':key,'X-Api-Resource-Id':MODELS[model],
            'X-Api-Request-Id':task_id,'X-Api-Sequence':'-1'}


def call(path,key,model,task_id,body):
    req=urllib.request.Request(API+path,data=json.dumps(body).encode(),headers=headers(key,model,task_id),method='POST')
    # This endpoint is fixed; never send the API key to redirected hosts.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    opener=urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req,timeout=60) as res:
            return res.headers.get('X-Api-Status-Code',''),json.load(res)
    except urllib.error.HTTPError as exc:
        return str(exc.code),{}  # do not echo request headers or provider error payloads


def audio_body(row,model):
    if model=='standard':
        url=row.get('audio_url','')
        if not url.startswith('https://'):raise ValueError('标准版须有已核实的HTTPS音频地址')
        return {'url':url,'format':row.get('format','mp3'),'language':'zh-CN'}
    path=Path(row['audio_path']);raw=path.read_bytes()
    if len(raw)>100*1024*1024 or row['duration']>7200:raise ValueError('极速版文件超过限制')
    if row.get('audio_sha256')!=hashlib.sha256(raw).hexdigest():raise ValueError('音频文件发生变化，请重新核实来源')
    return {'data':base64.b64encode(raw).decode(),'format':'mp3','language':'zh-CN'}


def summarize_result(body):
    result=body.get('result',{})
    if isinstance(result,list):result=result[0] if result else {}
    if not isinstance(result,dict) or not result.get('text'):raise ValueError('未取得有效转写正文')
    return result


def reserve(jobs,row,model,limit):
    key=signature(row,model)
    if key in jobs:return key,False
    used=sum(j['duration'] for j in jobs.values())
    if used+row['duration']>limit:raise ValueError('已达到核实的免费额度安全上限，停止提交')
    jobs[key]={'task_id':str(uuid.uuid4()),'model':model,'video_id':row['video_id'],'episode':row['episode'],
               'title':row['title'],'duration':row['duration'],'status':'reserved','created':time.time()}
    return key,True


def process(items,model,key_file,quota_file,journal):
    quota=json.loads(Path(quota_file).read_text())
    if quota.get('resource_id')!=MODELS[model] or not quota.get('verified_at') or time.time()-float(quota['verified_at'])>86400:
        raise ValueError('免费额度与模型尚未核验，或核验已过期')
    if quota.get('paid_allowed') is not False:raise ValueError('本批处理只允许免费额度')
    limit=float(quota.get('safe_seconds',0))
    if limit<=0:raise ValueError('没有可确认的免费额度，未发起调用')
    key=Path(key_file).read_text().strip()
    if not key or any(c.isspace() for c in key):raise ValueError('本机密钥文件格式无效')
    journal=Path(journal);jobs=json.loads(journal.read_text()) if journal.exists() else {}
    for row in items:
        token=signature(row,model);job=jobs.get(token)
        if job and job['status']=='complete':continue
        if job and job['status'] in ('uncertain','failed','reserved'):
            # A lost response may still be billable. Never blindly resubmit.
            print(f"第{row['episode']}集：已有未确认提交，保留任务ID，不重复扣用量",flush=True);continue
        if not job:
            body={'user':{'uid':'yeren-notes'},'audio':audio_body(row,model),'request':{'model_name':'bigmodel',
                  'enable_itn':False,'enable_punc':True,'enable_ddc':False,'show_utterances':True}}
            token,_=reserve(jobs,row,model,limit);job=jobs[token];write_json(journal,jobs)
            try:code,response=call('submit' if model=='standard' else 'recognize/flash',key,model,job['task_id'],body)
            except (OSError,ValueError):
                job['status']='uncertain';write_json(journal,jobs);raise ValueError('提交响应未确认，停止新增任务；请先查询原任务ID')
            job['provider_code']=code
            if code!=SUCCESS:
                job['status']='failed';write_json(journal,jobs);raise ValueError('API提交失败，已停止，不自动改用其他模型或收费方案')
            job['status']='submitted';write_json(journal,jobs)
        else:response={}
        if model=='standard':
            for _ in range(12):
                try:code,response=call('query',key,model,job['task_id'],{})
                except (OSError,ValueError):time.sleep(5);continue
                if code==SUCCESS:break
                if code not in PENDING:
                    job['provider_code']=code;write_json(journal,jobs);raise ValueError('查询返回异常；任务保留，可继续查询，不重复提交')
                time.sleep(5)
            else:
                print(f"第{row['episode']}集：处理中；下次运行继续查询",flush=True);continue
        result=summarize_result(response)
        out=ROOT/'research/yeren/transcripts'/f"episode-{row['episode']:03}.json"
        write_json(out,dict(episode=row['episode'],video_id=row['video_id'],title=row['title'],
                  source_url='https://www.douyin.com/video/'+row['video_id'],verification='transcribed',
                  provider='volcengine',resource_id=MODELS[model],task_id=job['task_id'],result=result))
        job['status']='complete';job['output']=str(out);write_json(journal,jobs)
        print(f"第{row['episode']}集：转写已保存，待核验原声",flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('manifest');p.add_argument('--run',action='store_true')
    p.add_argument('--model',choices=MODELS,default='standard');p.add_argument('--key-file',default=str(ROOT/'.runtime/volc-asr.key'))
    p.add_argument('--quota-file',default=str(ROOT/'.runtime/volc-asr-quota.json'));p.add_argument('--journal',default=str(ROOT/'.runtime/yeren-asr-jobs.json'))
    a=p.parse_args();items=checked_items(json.loads(Path(a.manifest).read_text()))
    print(f"计划：{len(items)}集，已核实总时长{sum(i['duration'] for i in items):.1f}秒；模式{a.model}",flush=True)
    if a.run:process(items,a.model,a.key_file,a.quota_file,a.journal)
    else:print('仅检查清单，未发送API请求。')

if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,OSError) as exc:raise SystemExit(str(exc))
