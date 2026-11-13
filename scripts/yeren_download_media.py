"""Download only previously observed public media URLs; never reads browser cookies."""
import json
from pathlib import Path
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor,as_completed
import imageio_ffmpeg
ROOT=Path(__file__).resolve().parents[1]

def download(row):
    episode=row['episode'];out=ROOT/'research/yeren/audio'/f'episode-{episode:03}.mp3'
    if out.exists() and out.stat().st_size>1000:return episode,'already prepared'
    urls=row.get('audio_urls') or row.get('media_urls') or []
    urls=sorted(urls,key=lambda u:'zjcdn.com' not in u)
    raw=out.with_suffix('.source');last='unavailable'
    for url in urls[:2]:
        if not url.startswith('https://'):continue
        try:
            req=urllib.request.Request(url,headers={'Referer':'https://www.douyin.com/','User-Agent':'Mozilla/5.0'})
            with urllib.request.urlopen(req,timeout=30) as r,raw.open('wb') as f:
                total=0
                while True:
                    data=r.read(512*1024)
                    if not data:break
                    total+=len(data)
                    if total>100*1024*1024:raise ValueError('size limit')
                    f.write(data)
            tmp=out.with_suffix('.part.mp3')
            result=subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-loglevel','error','-y','-i',str(raw),'-vn','-ac','1','-ar','16000','-b:a','48k',str(tmp)],capture_output=True,timeout=120)
            if result.returncode:raise ValueError('audio conversion failed')
            tmp.replace(out);return episode,'prepared'
        except Exception as exc:last=type(exc).__name__
    return episode,last

if __name__=='__main__':
    import sys
    rows=json.loads(Path(sys.argv[1]).read_text())
    with ThreadPoolExecutor(max_workers=2) as ex:
        for future in as_completed([ex.submit(download,r) for r in rows]):print(*future.result(),flush=True)
