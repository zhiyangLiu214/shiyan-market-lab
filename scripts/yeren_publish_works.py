"""Publish additional author works, separately from the 59-episode collection; preserve edits."""
import argparse,json,urllib.request
from pathlib import Path
from yeren_publish_notes import FIELDS,same
from yeren_site import site_access
ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--publish',action='store_true');a=p.parse_args()
    drafts=json.loads((ROOT/'research/yeren/works-note-drafts.json').read_text())
    if not a.publish:print('补充作品草稿',len(drafts),'条；未写入网站');return
    url,headers=site_access()
    def request(body=None):
        req=urllib.request.Request(url,headers=headers,data=json.dumps(body).encode() if body else None,method='POST' if body else 'GET')
        with urllib.request.urlopen(req,timeout=25) as res:result=json.load(res)
        if not result.get('ok'):raise ValueError('笔记保存失败')
        return result['data']
    items={n['id']:n for n in request()['items']}
    logfile=ROOT/'.runtime/yeren-published-works.json';log=json.loads(logfile.read_text()) if logfile.exists() else {}
    for vid,draft in drafts.items():
        note_id='video-'+vid;old=items.get(note_id)
        if old and same(old,draft):log[vid]=draft;continue
        if old and (not log.get(vid) or not same(old,log[vid])):
            print(vid,'保留已有编辑，未覆盖');continue
        payload=dict(draft,id=note_id)
        if old:payload['revision']=old['revision']
        saved=request({'action':'save','note':payload});log[vid]=draft
        logfile.write_text(json.dumps(log,ensure_ascii=False,indent=2));print(vid,'已保存',saved['verification'],flush=True)
    logfile.write_text(json.dumps(log,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
