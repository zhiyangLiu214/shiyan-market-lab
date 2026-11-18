"""Find pre-board setups at signal close, then inspect subsequent daily bars.

This is a historical price study. Signal-close fills and intraday trigger fills are
not assumed, and no observed future bar is used to select a candidate.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from limit_streak_candidates import limit_price


def load_rows(path, calendar_index):
    data = json.loads(path.read_text())
    rows = []
    for raw in data['rows']:
        if raw['tradestatus'] != '1' or not raw['volume'] or float(raw['volume']) <= 0:
            continue
        if raw['date'] not in calendar_index:
            continue
        row = dict(date=raw['date'], session=calendar_index[raw['date']],
                   st=raw['isST'] == '1',
                   **{key: float(raw[key]) for key in
                      ('open', 'high', 'low', 'close', 'preclose', 'volume')})
        row['board'] = (not row['st'] and
                        abs(row['close'] - limit_price(row['preclose'], .1)) < .005 and
                        abs(row['high'] - row['close']) < .005)
        rows.append(row)
    return data, rows


def contiguous(rows, start, end):
    """Require uninterrupted market sessions and no rights-adjustment break."""
    return all(rows[j]['session'] == rows[j - 1]['session'] + 1 and
               abs(rows[j]['preclose'] - rows[j - 1]['close']) <= .02
               for j in range(start + 1, end + 1))


def board_context(rows, index):
    recent_streak = (index > 0 and rows[index - 1]['board'] and
                     contiguous(rows, index - 1, index)) or any(
                         rows[j]['board'] and rows[j - 1]['board'] and
                         contiguous(rows, j - 1, j)
                         for j in range(max(1, index - 20), index))
    rally5 = any(contiguous(rows, end - 5, end) and
                 rows[end]['close'] / rows[end - 5]['close'] >= 1.20
                 for end in range(max(5, index - 20), index))
    rally10 = any(contiguous(rows, end - 10, end) and
                  rows[end]['close'] / rows[end - 10]['close'] >= 1.30
                  for end in range(max(10, index - 20), index))
    return dict(recent_consecutive_limits=recent_streak,
                recent_large_rally=bool(rally5 or rally10))


def independent_board(rows, index):
    context = board_context(rows, index)
    return rows[index]['board'] and not any(context.values())


def future(rows, signal, horizon):
    """Use only complete, uninterrupted market-session horizons."""
    end = signal + horizon
    if end >= len(rows) or not contiguous(rows, signal, end):
        return None
    segment = rows[signal + 1:end + 1]
    first = next((i for i, row in enumerate(segment) if row['board']), None)
    streak = 0
    independent = False
    context = None
    if first is not None:
        actual_index = signal + first + 1
        context = board_context(rows, actual_index)
        independent = not any(context.values())
        for row in segment[first:]:
            if not row['board']:
                break
            streak += 1
    return dict(first_limit_date=segment[first]['date'] if first is not None else None,
                first_limit_delay=first + 1 if first is not None else None,
                limit_within_horizon=first is not None,
                first_limit_is_independent=independent,
                first_limit_context=context,
                consecutive_limits_after_first=streak,
                breakout_high=max(row['high'] for row in segment),
                min_low=min(row['low'] for row in segment),
                final_close=segment[-1]['close'])


def find_signals(rows, start, end):
    signals = []
    for prior in range(1, len(rows)):
        first = rows[prior]
        if not first['board'] or rows[prior - 1]['board'] or not independent_board(rows, prior):
            continue
        if not contiguous(rows, prior - 1, prior):
            continue
        quiet = []
        for reclaim in range(prior + 1, min(prior + 17, len(rows))):
            row = rows[reclaim]
            if not contiguous(rows, reclaim - 1, reclaim) or row['st'] or row['board']:
                break
            if row['close'] > first['close']:
                if 4 <= len(quiet) <= 15 and start <= row['date'] <= end:
                    base = dict(prior_board_date=first['date'], prior_open=first['open'],
                                prior_close=first['close'], quiet_days=len(quiet),
                                quiet_low=min(q['low'] for q in quiet),
                                held_prior_open=min(q['low'] for q in quiet) >= first['open'],
                                reclaim_date=row['date'], reclaim_close=row['close'],
                                reclaim_high=row['high'])
                    signals.append((reclaim, 'reclaim', base))
                    for pullback in range(reclaim + 1, min(reclaim + 4, len(rows))):
                        current = rows[pullback]
                        if not contiguous(rows, pullback - 1, pullback) or current['st'] or current['board']:
                            break
                        if current['close'] < row['close'] and current['volume'] < row['volume']:
                            if current['date'] <= end:
                                signals.append((pullback, 'pullback', base))
                            break
                break
            if abs(row['close'] / row['open'] - 1) > .06 or row['close'] > first['close'] * 1.05:
                break
            quiet.append(row)
            if len(quiet) > 15:
                break
    return signals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--daily-primary', type=Path, required=True)
    parser.add_argument('--daily-fallback', type=Path, required=True)
    parser.add_argument('--calendar', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--start', default='2025-10-01')
    parser.add_argument('--end', default='2026-11-13')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    calendar = json.loads(args.calendar.read_text())
    calendar_index = {date: i for i, date in enumerate(calendar)}
    counts = Counter()
    candidates = []
    for stock in plan['roster']:
        code = stock['code']
        path = args.daily_primary / f'{code}.json'
        if not path.exists():
            path = args.daily_fallback / f'{code}.json'
        if not path.exists():
            counts['missing_history'] += 1
            continue
        data, rows = load_rows(path, calendar_index)
        counts['stocks_with_history'] += 1
        for index, stage, base in find_signals(rows, args.start, args.end):
            row = rows[index]
            later = future(rows, index, 5)
            if later is None:
                counts[f'{stage}_incomplete_5d'] += 1
            else:
                counts[f'{stage}_complete_5d'] += 1
                if later['breakout_high'] > base['reclaim_high']:
                    counts[f'{stage}_breakout_5d'] += 1
                if later['limit_within_horizon']:
                    counts[f'{stage}_any_limit_5d'] += 1
                    if later['first_limit_is_independent']:
                        counts[f'{stage}_independent_first_5d'] += 1
                    else:
                        counts[f'{stage}_resumption_5d'] += 1
                    for threshold in (2, 3, 4):
                        if later['consecutive_limits_after_first'] >= threshold:
                            counts[f'{stage}_any_limit_streak_{threshold}'] += 1
                            if later['first_limit_is_independent']:
                                counts[f'{stage}_independent_streak_{threshold}'] += 1
            candidates.append(dict(code=code, name=stock['name'], stage=stage,
                                   signal_date=row['date'], signal_close=row['close'],
                                   signal_volume=row['volume'], source=data['source'],
                                   retrieved_at=data['retrieved_at'], **base,
                                   future_5d=later))
    for stage in ('reclaim', 'pullback'):
        counts[f'{stage}_unique_stocks'] = len({c['code'] for c in candidates
                                                if c['stage'] == stage})
    result = dict(start=args.start, end=args.end, universe=len(plan['roster']),
                  counts=dict(counts), candidates=sorted(candidates,
                    key=lambda c: (c['signal_date'], c['code'], c['stage'])),
                  definition='Prior 10% board must not follow a consecutive-limit run or any 5-session gain >=20% / 10-session gain >=30% within the prior 20 market sessions. Then 4-15 non-board quiet sessions with candle body <=6%; first close above prior board close is reclaim. First lower-close, lower-volume session within 3 market sessions is pullback. Prior-board open is a label only. Future limits after recent streak/rally are labeled resumption, not independent first boards. Outcomes use the next five uninterrupted market sessions.')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result['counts'], ensure_ascii=False))


if __name__ == '__main__':
    main()
