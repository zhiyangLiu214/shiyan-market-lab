"""Publish reviewed drafts to the existing notebook; do not overwrite user edits."""
import argparse,json,urllib.request
from pathlib import Path
from yeren_site import site_access
ROOT=Path(__file__).resolve().parents[1]
FIELDS=('title','category','summary','conditions','actions','invalidation','risk','source_url','time_range','verification','tags')

def same(current,previous):return all((current.get(k) or '')==(previous.get(k) or '') for k in FIELDS)

def main():
 p=argparse.ArgumentParser();p.add_argument('--publish',action='store_true');a=p.parse_args()
 drafts=json.loads((ROOT/'research/yeren/notes-drafts.json').read_text());logfile=ROOT/'.runtime/yeren-published-notes.json';log=json.loads(logfile.read_text()) if logfile.exists() else {}
 if not a.publish:print('可发布',len(drafts),'条草稿；未发送请求');return
 url,headers=site_access()
 def request(body=None):
  req=urllib.request.Request(url,headers=headers,data=json.dumps(body).encode() if body else None,method='POST' if body else 'GET')
  with urllib.request.urlopen(req,timeout=25) as res:result=json.load(res)
  if not result.get('ok'):raise ValueError('笔记接口未保存成功')
  return result['data']
 items={n['id']:n for n in request()['items']}
 for ep,draft in sorted(drafts.items(),key=lambda x:int(x[0])):
  note_id=f'catalog-{int(ep):03}';old=items.get(note_id);previous=log.get(ep)
  if old and same(old,draft):log[ep]=draft;continue
  if not old:print(ep,'跳过：目录尚未同步');continue
  if not (old['verification']=='catalog' and not old['summary']) and not (previous and same(old,previous)):
   print(ep,'保留已有编辑，未覆盖');continue
  saved=request({'action':'save','note':dict(draft,id=old['id'],revision=old['revision'])});log[ep]=draft
  logfile.write_text(json.dumps(log,ensure_ascii=False,indent=2));print(ep,'已保存',saved['verification'],flush=True)
 logfile.write_text(json.dumps(log,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
