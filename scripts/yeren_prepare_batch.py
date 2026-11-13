"""Assemble local speech batches with exact PCM offsets for later transcript splitting."""
import argparse,json,wave,subprocess
from pathlib import Path
import imageio_ffmpeg
ROOT=Path(__file__).resolve().parents[1]
AUDIO=ROOT/'research/yeren/audio';OUT=ROOT/'research/yeren/batches'

def make_batch(episodes,name):
    OUT.mkdir(parents=True,exist_ok=True);wav_path=OUT/(name+'.wav');mp3_path=OUT/(name+'.mp3');sections=[];cursor=0
    with wave.open(str(wav_path),'wb') as target:
        target.setnchannels(1);target.setsampwidth(2);target.setframerate(16000)
        for episode in episodes:
            p=AUDIO/f'episode-{episode:03}.mp3'
            if not p.exists():raise ValueError(f'缺少第{episode}集音频')
            raw=subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-loglevel','error','-i',str(p),'-f','s16le','-ar','16000','-ac','1','pipe:1'],capture_output=True,check=True).stdout
            frames=len(raw)//2;start=cursor;target.writeframes(raw);cursor+=frames
            sections.append(dict(episode=episode,start_ms=round(start/16),end_ms=round(cursor/16),source=str(p)))
            target.writeframes(bytes(32000));cursor+=16000
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-loglevel','error','-y','-i',str(wav_path),'-b:a','48k',str(mp3_path)],check=True)
    if mp3_path.stat().st_size>18_000_000:raise ValueError('音频批次过大')
    meta=dict(file=str(mp3_path),duration_ms=round(cursor/16),sections=sections,verification='transcribed',split_requires='ASR返回的语句或逐词时间戳；无法对齐时不自动归属观点')
    (OUT/(name+'.json')).write_text(json.dumps(meta,ensure_ascii=False,indent=2));wav_path.unlink();return meta

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('episodes',help='逗号分隔集数');p.add_argument('--name',default='batch-test');a=p.parse_args()
    meta=make_batch([int(e) for e in a.episodes.split(',')],a.name);print(a.name,meta['duration_ms']/1000,'秒',len(meta['sections']),'集')
