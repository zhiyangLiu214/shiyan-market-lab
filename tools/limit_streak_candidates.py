"""Screen first- and second-board stocks from point-in-time daily history.

This tool lists next-session candidates. It does not place orders or infer fills.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
MAIN_CODE = re.compile(r'(?:00|60)\d{4}\Z')
RANKINGS = ('pattern', 'turnover', 'floatcap', 'code')


def limit_price(preclose, rate):
    return float((Decimal(str(preclose)) * (1 + Decimal(str(rate)))).quantize(
        Decimal('.01'), rounding=ROUND_HALF_UP))


def number(row, key):
    try:
        value = float(row[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'Missing or invalid {key}') from exc
    if not math.isfinite(value):
        raise ValueError(f'Nonfinite {key}')
    return value


def load_history(path, as_of):
    data = json.loads(path.read_text(encoding='utf-8'))
    if not data.get('source') or not data.get('retrieved_at'):
        raise ValueError('Source or retrieval time missing')
    retrieved = datetime.fromisoformat(data['retrieved_at'])
    if retrieved.tzinfo is None:
        retrieved = retrieved.replace(tzinfo=TZ)
    if retrieved.astimezone(TZ) < datetime.combine(datetime.fromisoformat(as_of).date(),
                                                   time(15), TZ):
        raise ValueError('Daily source was retrieved before signal-day close')
    rows = data['rows']
    dates = [r['date'] for r in rows]
    if dates != sorted(set(dates)):
        raise ValueError('Daily rows unordered or duplicated')
    return data, {r['date']: r for r in rows if r['date'] <= as_of}


def adjusted_window(rows, first_day):
    """Use the historical preclose ratio to keep pre-board comparisons adjusted."""
    active = [r for r in rows if r['tradestatus'] == '1' and number(r, 'volume') > 0]
    first = next((i for i, r in enumerate(active) if r['date'] == first_day), None)
    if first is None or first < 60:
        return None
    adjusted = None
    prices = []
    for row in active[:first]:
        close, preclose = number(row, 'close'), number(row, 'preclose')
        if close <= 0 or preclose <= 0:
            raise ValueError('Invalid adjusted price history')
        adjusted = close if adjusted is None else adjusted * close / preclose
        factor = adjusted / close
        prices.append((adjusted, number(row, 'high') * factor,
                       number(row, 'low') * factor))
    window = prices[-60:]
    low, high = min(p[2] for p in window), max(p[1] for p in window)
    return round(100 * (window[-1][0] - low) / (high - low), 2) if high > low else None


def is_limit_board(row):
    if row.get('tradestatus') != '1' or row.get('isST') != '0':
        return False
    close, high = number(row, 'close'), number(row, 'high')
    cap = limit_price(number(row, 'preclose'), .1)
    return abs(close - cap) < .005 and abs(high - cap) < .005


def is_one_word(row):
    prices = [number(row, key) for key in ('open', 'high', 'low', 'close')]
    return max(prices) - min(prices) < .005


def classify(code, name, data, by_date, as_of, previous, before_previous):
    today, yesterday, earlier = (by_date.get(day) for day in
                                 (as_of, previous, before_previous))
    if today is None or yesterday is None or earlier is None:
        return None
    if yesterday.get('tradestatus') != '1' or not yesterday.get('volume'):
        return None
    if not is_limit_board(today):
        return None
    second = is_limit_board(yesterday)
    if second and earlier.get('tradestatus') != '1':
        return None
    if second and is_limit_board(earlier):
        return None
    first_day = previous if second else as_of
    first = yesterday if second else today
    if first.get('isST') != '0':
        return None
    volume, previous_volume = number(today, 'volume'), number(yesterday, 'volume')
    if volume <= 0 or previous_volume <= 0:
        return None
    active = [r for r in by_date.values() if r.get('tradestatus') == '1'
              and r.get('date') <= first_day]
    if len(active) <= 5:
        return None
    position = adjusted_window(list(by_date.values()), first_day)
    turnover = number(today, 'turn')
    cap_estimate = (number(today, 'close') * volume / turnover / 1_000_000
                    if turnover > 0 else None)
    ratio = volume / previous_volume if second else None
    first_turn = number(first, 'turn')
    if second:
        tier = ('缩量且估算流通市值<100亿元' if ratio < 1 and cap_estimate is not None
                and cap_estimate < 100 else '缩量二板' if ratio < 1 else '其他二板')
        priority = 2 if tier.startswith('缩量且') else 1 if ratio < 1 else 0
    else:
        tier = ('前60日高位且换手>=10%' if position is not None and position >= 70
                and first_turn >= 10 else '前60日高位' if position is not None
                and position >= 70 else '其他首板')
        priority = 2 if tier.startswith('前60日高位且') else 1 if tier == '前60日高位' else 0
    return dict(code=code, name=name, stage=2 if second else 1,
                signal_date=as_of, first_board_date=first_day,
                priority=priority, observation_tag=tier,
                first_board_one_word=is_one_word(first),
                second_board_one_word=is_one_word(today) if second else None,
                first_board_turnover_pct=round(first_turn, 4),
                signal_turnover_pct=round(turnover, 4),
                second_to_first_volume=round(ratio, 4) if ratio is not None else None,
                estimated_float_cap_100m=round(cap_estimate, 2) if cap_estimate else None,
                pre_first_position_60_pct=position,
                signal_close=number(today, 'close'),
                signal_volume_shares=volume,
                source=data['source'], retrieved_at=data['retrieved_at'])


def sort_key(row, ranking):
    if ranking == 'turnover':
        return (-row['signal_turnover_pct'], row['code'])
    if ranking == 'floatcap':
        value = row['estimated_float_cap_100m']
        return (value is None, value if value is not None else float('inf'), row['code'])
    if ranking == 'code':
        return (row['code'],)
    if row['stage'] == 1:
        position = row['pre_first_position_60_pct']
        return (-row['priority'], -(position if position is not None else -1), row['code'])
    return (-row['priority'], row['second_to_first_volume'], row['code'])


def screen(daily_dirs, calendar_path, as_of, ranking='pattern', roster_path=None):
    if ranking not in RANKINGS:
        raise ValueError('Unsupported ranking')
    datetime.strptime(as_of, '%Y-%m-%d')
    calendar = json.loads(Path(calendar_path).read_text(encoding='utf-8'))
    if calendar != sorted(set(calendar)) or as_of not in calendar:
        raise ValueError('Invalid calendar or signal date absent')
    index = calendar.index(as_of)
    if index < 2:
        raise ValueError('At least two prior market sessions are required')
    previous, before_previous = calendar[index - 1], calendar[index - 2]
    paths = {}
    for folder in daily_dirs:
        for path in sorted(Path(folder).glob('*.json')):
            if MAIN_CODE.fullmatch(path.stem):
                paths.setdefault(path.stem, path)
    if roster_path:
        payload = json.loads(Path(roster_path).read_text(encoding='utf-8'))
        rows = payload['roster'] if isinstance(payload, dict) else payload
        roster = {r['code']: r.get('name') or r['code'] for r in rows
                  if MAIN_CODE.fullmatch(r['code'])}
    else:
        roster = {code: code for code in paths}
    candidates, skipped = [], Counter()
    for code, name in sorted(roster.items()):
        path = paths.get(code)
        if path is None:
            skipped['missing_daily_file'] += 1
            continue
        try:
            data, by_date = load_history(path, as_of)
            item = classify(code, name, data, by_date, as_of,
                            previous, before_previous)
            if item:
                item['daily_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
                candidates.append(item)
            elif as_of not in by_date:
                skipped['no_signal_day'] += 1
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            skipped[str(exc)] += 1
    groups = {}
    for stage, label in ((1, '一进二'), (2, '二进三')):
        group = sorted((r for r in candidates if r['stage'] == stage),
                       key=lambda r: sort_key(r, ranking))
        groups[label] = group
    status = 'partial' if skipped else 'complete' if roster_path else 'scope_unverified'
    return dict(as_of=as_of, next_session=calendar[index + 1] if index + 1 < len(calendar) else None,
                ranking=ranking, universe=len(roster), available_daily_files=len(paths),
                skipped=dict(skipped), status=status,
                groups=groups,
                entry_routes=['集合竞价预先委托', '开盘连续竞价', '盘中触板委托'],
                note='候选及优先标签只使用信号日及之前数据；买入方式、成交和第三天收益需另行核验')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--daily-dir', action='append', required=True)
    parser.add_argument('--calendar', required=True)
    parser.add_argument('--roster', help='JSON array or object with roster array; verifies universe coverage')
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--ranking', choices=RANKINGS, default='pattern')
    args = parser.parse_args()
    print(json.dumps(screen(args.daily_dir, args.calendar, args.as_of, args.ranking, args.roster),
                     ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
