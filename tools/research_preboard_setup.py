"""Audit prior-limit consolidation patterns among historical first-board signals.

The output is descriptive. It must not be interpreted as a fill or return study.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from limit_streak_candidates import limit_price


def board(rows, index):
    if index < 1:
        return False
    return abs(rows[index][2] - limit_price(rows[index - 1][2], .1)) < .005


def classify(rows, index):
    """Return the strict Jiuyang-like setup, including the optional floor tag."""
    if index < 7 or not board(rows, index) or board(rows, index - 1):
        return None
    for prior in range(index - 6, max(-1, index - 21), -1):
        if not board(rows, prior):
            continue
        # Search the latest reclaim before a one-session lower-volume pullback.
        for reclaim in range(index - 2, prior + 3, -1):
            quiet = rows[prior + 1:reclaim]
            if not 4 <= len(quiet) <= 12:
                continue
            if any(board(rows, j) for j in range(prior + 1, index)):
                continue
            if any(abs(r[2] / r[1] - 1) > .05 or r[2] > rows[prior][2] * 1.02
                   for r in quiet):
                continue
            if any(r[4] < rows[prior][1] for r in quiet):
                continue
            if rows[reclaim][2] <= rows[prior][2]:
                continue
            pullback = rows[reclaim + 1:index]
            if len(pullback) != 1:
                continue
            return dict(prior=rows[prior][0], prior_close=rows[prior][2],
                        quiet_days=len(quiet), reclaim=rows[reclaim][0],
                        prior_open=rows[prior][1],
                        quiet_low=min(r[4] for r in quiet),
                        reclaim_close=rows[reclaim][2], reclaim_high=rows[reclaim][3],
                        pullback=pullback[0][0], pullback_close=pullback[0][2],
                        pullback_low=pullback[0][4],
                        pullback_holds_prior_open=pullback[0][4] >= rows[prior][1],
                        pullback_volume_ratio=round(pullback[0][5] / rows[reclaim][5], 3),
                        lower_close=pullback[0][2] < rows[reclaim][2],
                        lower_volume=pullback[0][5] < rows[reclaim][5],
                        breakout=rows[index][2] > rows[reclaim][3])
    return None


def classify_near(rows, index):
    """Find the main setup without requiring the prior-board open to hold."""
    if index < 7 or not board(rows, index) or board(rows, index - 1):
        return None
    for prior in range(index - 6, max(-1, index - 36), -1):
        if not board(rows, prior):
            continue
        if any(board(rows, j) for j in range(prior + 1, index)):
            continue
        for reclaim in range(index - 2, prior + 4, -1):
            quiet = rows[prior + 1:reclaim]
            pullback = rows[reclaim + 1:index]
            if not 4 <= len(quiet) <= 15 or not 1 <= len(pullback) <= 3:
                continue
            if any(abs(r[2] / r[1] - 1) > .06 or r[2] > rows[prior][2] * 1.05
                   for r in quiet):
                continue
            if rows[reclaim][2] <= rows[prior][2]:
                continue
            if not any(r[2] < rows[reclaim][2] and r[5] < rows[reclaim][5]
                       for r in pullback):
                continue
            if rows[index][2] <= rows[reclaim][3]:
                continue
            return dict(prior=rows[prior][0], prior_open=rows[prior][1],
                        prior_close=rows[prior][2], quiet_days=len(quiet),
                        quiet_low=min(r[4] for r in quiet),
                        max_quiet_body_pct=round(max(abs(r[2] / r[1] - 1) for r in quiet) * 100, 2),
                        reclaim=rows[reclaim][0], reclaim_close=rows[reclaim][2],
                        reclaim_high=rows[reclaim][3], pullback_days=len(pullback),
                        pullback_end=rows[index - 1][0],
                        pullback_low=min(r[4] for r in pullback),
                        held_prior_open=min(r[4] for r in rows[prior + 1:index]) >= rows[prior][1])
    return None


def continuation(rows, index):
    count = 1
    for later in range(index + 1, min(index + 4, len(rows))):
        if not board(rows, later):
            break
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entries', type=Path, required=True)
    parser.add_argument('--daily', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = json.loads(args.entries.read_text())
    signals = defaultdict(list)
    for entry in source['entries']:
        if entry['stage'] == 1:
            signals[entry['code']].append(entry)
    samples = []
    near_samples = []
    counts = Counter()
    for code, entries in signals.items():
        path = args.daily / f'{code}.json'
        if not path.exists():
            counts['missing_history'] += len(entries)
            continue
        history = json.loads(path.read_text())
        rows = [(r[0], *map(float, r[1:6])) for r in history['raw']]
        by_date = {r[0]: i for i, r in enumerate(rows)}
        for entry in entries:
            index = by_date.get(entry['date'])
            if index is None:
                counts['missing_signal_date'] += 1
                continue
            counts['all_first_boards'] += 1
            if index + 3 < len(rows):
                counts['first_boards_with_3_following_rows'] += 1
                length = continuation(rows, index)
                for threshold in (2, 3, 4):
                    if length >= threshold:
                        counts[f'all_reached_{threshold}'] += 1
            else:
                length = None
            near = classify_near(rows, index)
            if near is not None:
                counts['near_all'] += 1
                if near['held_prior_open']:
                    counts['near_held_prior_open'] += 1
                if length is not None:
                    counts['near_with_3_following_rows'] += 1
                    for threshold in (2, 3, 4):
                        if length >= threshold:
                            counts[f'near_reached_{threshold}'] += 1
                near_samples.append(dict(code=code, name=entry['name'], date=entry['date'],
                                         consecutive_boards_capped_4=length,
                                         source=history['source'], retrieved_at=history['retrieved_at'],
                                         **near))
            setup = classify(rows, index)
            if setup is None:
                continue
            counts['prior_board_quiet_reclaim'] += 1
            if setup['lower_close'] and setup['lower_volume']:
                counts['plus_lower_volume_pullback'] += 1
                if setup['breakout']:
                    counts['plus_breakout'] += 1
                    if length is not None:
                        counts['strict_with_3_following_rows'] += 1
                        for threshold in (2, 3, 4):
                            if length >= threshold:
                                counts[f'strict_reached_{threshold}'] += 1
            samples.append(dict(code=code, name=entry['name'], date=entry['date'],
                                source=history['source'], retrieved_at=history['retrieved_at'],
                                consecutive_boards_capped_4=length,
                                **setup))
    result = dict(start=source['start'], end=source['end'], counts=dict(counts),
                  definition={'prior_board_gap_sessions': '6-20',
                              'quiet_sessions': '4-12, body <=5%, close <= prior board close *1.02',
                              'quiet_floor': 'every daily low >= prior board open',
                              'reclaim': 'close > prior board close',
                              'pullback': 'exactly one session before new first board',
                              'breakout': 'new first board close > reclaim high'},
                  main_pattern='prior board 6-35 sessions ago; 4-15 quiet sessions with body <=6% and close <= prior board close *1.05; reclaim close above prior board close; 1-3 pullback sessions with at least one lower close and volume; new first board close above reclaim high; prior-board open is a tag, not a filter',
                  samples=sorted(samples, key=lambda r: (r['date'], r['code'])),
                  near_samples=sorted(near_samples, key=lambda r: (r['date'], r['code'])))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result['counts'], ensure_ascii=False))


if __name__ == '__main__':
    main()
