"""Offline fallback for public clips whose web recognition did not complete."""
import argparse,json,subprocess
from pathlib import Path
import numpy as np
import imageio_ffmpeg
import mlx_whisper
ROOT=Path(__file__).resolve().parents[1]

def transcribe(ep, review_dir=None):
    out=(Path(review_dir) if review_dir else ROOT/'research/yeren/transcripts')/f'episode-{ep:03}.json'
    if out.exists():raise ValueError('已有转写文件，不自动覆盖')
    source=ROOT/'research/yeren/audio'/f'episode-{ep:03}.mp3'
    decoded=subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-loglevel','error','-i',str(source),'-f','f32le','-ar','16000','-ac','1','pipe:1'],capture_output=True,check=True)
    audio=np.frombuffer(decoded.stdout,dtype=np.float32).copy()
    result=mlx_whisper.transcribe(audio,path_or_hf_repo='mlx-community/whisper-small-mlx',language='zh',temperature=0,condition_on_previous_text=False,word_timestamps=True,verbose=False)
    catalog=json.loads((ROOT/('research/yeren_tech_catalog.json' if ep<=59 else 'research/yeren/works-catalog.json')).read_text())['episodes']
    cat=next(r for r in catalog if r['episode']==ep)
    data=dict(episode=ep,video_id=cat['video_id'],title=cat['title'],source_url=cat.get('url') or cat['source_url'],audio_filename=source.name,
        audio_duration_seconds=len(audio)/16000,processed_full_audio=True,provider='本地MLX Whisper small',verification='transcribed',segments=[r['text'].strip() for r in result['segments']],
        raw_text=result['text'].strip(),timed_segments=result['segments'],retrieved_at='2026-11-12',
        review_flags=['本地识别补充，未核验原声；须重点核对术语、数字、否定句和漏词'])
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(data,ensure_ascii=False,indent=2));print('本地转写完成',ep,'语段',len(result['segments']),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('episodes',nargs='+',type=int);p.add_argument('--review-dir');a=p.parse_args()
    for ep in a.episodes:transcribe(ep,a.review_dir)
