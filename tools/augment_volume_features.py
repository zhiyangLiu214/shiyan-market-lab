"""Add same-clock volume and price-volume relationships without future bars."""
import argparse
import copy
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from terminal_entry_features import volume_price_features
from terminal_backtest import write_json
from terminal_paper import clock




def volume_patterns(features):
    v=features.get('same_time_volume_ratio_5');change=features.get('change_today');position=features.get('range_position_20')
    if v is None or change is None:return []
    tags=[]
    if v<.8 and position is not None and position<=40:tags.append('低位缩量')
    if 1<=v<1.8 and change>0 and features.get('close_location',0)>=.7:tags.append('温和放量上涨')
    if v>=1.8 and ((features.get('change_5') or 0)>=8 or (position is not None and position>=80)):tags.append('急涨高位放量')
    if v<1 and change>0:tags.append('价涨量缩')
    if v>1 and change<0:tags.append('价跌量增')
    if v<1 and change<0:tags.append('缩量回调')
    if v>=1.2 and (features.get('distance_high_20') or 0)>0:tags.append('放量突破')
    if v>=1.5 and (abs(change)<1 or features.get('upper_shadow',0)>=.4):tags.append('放量滞涨或长上影')
    return tags


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--study',required=True);p.add_argument('--data',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    source=Path(a.study);out=Path(a.output);out.mkdir(parents=True,exist_ok=False)
    original=json.loads((source/'study.json').read_text());rows=[json.loads(l) for l in (source/'samples.jsonl').open()]
    grouped=defaultdict(list)
    for r in rows:grouped[r['code']].append(r)
    errors=[]
    with (out/'samples.jsonl').open('w') as stream:
        for code,items in sorted(grouped.items()):
            daily=json.loads((Path(a.data)/'daily'/(code+'.json')).read_text());bar_data=json.loads((Path(a.data)/'bars'/(code+'.json')).read_text());bars=bar_data['rows'] if isinstance(bar_data,dict) else bar_data
            for row in items:
                if row.get('features'):
                    try:
                        row['features'].update(volume_price_features(daily,bars,row['date']));row['volume_patterns']=volume_patterns(row['features'])
                    except Exception as exc:errors.append(dict(code=code,date=row['date'],error=str(exc)))
                stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
    manifest=copy.deepcopy(original);manifest.update(created=clock(),version='candidate-volume-patterns-v1',source_study=str(source),
        source_samples_sha256=hashlib.sha256((source/'samples.jsonl').read_bytes()).hexdigest(),volume_feature_errors=errors,
        assumptions=original['assumptions']+['同期量比仅比较过去交易日14:30累计量；最后30分钟仅用已完成的14:00至14:30段',
           '量价形态可重叠，描述性分组不当作自动买点；成交量不是主力净流入'])
    write_json(out/'study.json',manifest);print('Augmented',len(rows),'samples; errors',len(errors),flush=True)


if __name__=='__main__':main()
