"""Audit full audio files and transcript identity; sparse ASR is a review flag, not a rejection."""
import json,re,subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import imageio_ffmpeg
ROOT=Path(__file__).resolve().parents[1]


def inspect(row):
    ep=row['episode'];audio=ROOT/'research/yeren/audio'/f'episode-{ep:03}.mp3'
    t=json.loads((ROOT/'research/yeren/transcripts'/f'episode-{ep:03}.json').read_text())
    run=subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-i',str(audio),'-f','null','-'],capture_output=True,timeout=40)
    m=re.search(rb'Duration: (\d+):(\d+):(\d+\.\d+)',run.stderr)
    duration=int(m[1])*3600+int(m[2])*60+float(m[3]) if m else None
    expected=row.get('duration') or None
    utterances=t.get('result',{}).get('utterances') or t.get('timed_segments') or []
    end=max((u.get('end_time',0)/1000 if 'end_time' in u else u.get('end',0) for u in utterances),default=None)
    flags=[]
    if duration and len(t.get('raw_text',''))/duration<1.5:flags.append('语音文字稀疏，须复核全音轨；不可据此认定无干货')
    if duration and end and end<duration*.85:flags.append('最后识别语句距音轨结束较远，可能为音乐/静默或漏识，须复核')
    if not utterances:flags.append('旧网页转写未保留时间戳；无法自动验证末段覆盖')
    if not expected:flags.append('源目录未提供时长，已从完整本地音轨测得')
    if expected and duration and abs(expected-duration)>1:flags.append('源时长与音轨时长不一致，需核验')
    return dict(episode=ep,video_id=t['video_id'],source_identity_matches=t['video_id']==row['video_id'],audio_seconds=duration,
        source_seconds=expected,full_audio_decode_ok=run.returncode==0,last_speech_seconds=end,transcript_chars=len(t.get('raw_text','')),
        original_verified=False,flags=flags)

if __name__=='__main__':
    rows=json.loads((ROOT/'research/yeren/download_manifest.json').read_text())
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(inspect,rows))
    output=dict(date='2026-11-12',policy='完整音轨识别后按内容及时间段提取；不按标题、开头或转写字数跳过视频；无时间戳不推定末段已核验',items=results)
    (ROOT/'research/yeren/full-audio-audit.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))
    print('全音轨',sum(r['full_audio_decode_ok'] for r in results),'/',len(results),'来源匹配',sum(r['source_identity_matches'] for r in results))
    print('原声已核验',sum(r['original_verified'] for r in results))
