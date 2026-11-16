"""Freeze causal limit-entry hypotheses and audit local execution evidence.

No network calls, orders, account databases, or automatic strategy updates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import uuid
from collections import Counter
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from terminal_backtest import minute_points
from terminal_paper import FEE_SCHEDULE

VERSION = 'limit-entry-validation-v1'
TZ = ZoneInfo('Asia/Shanghai')
RULES = {
    'capacity_two': {
        'name': '二进三容量换手型', 'role': 'execution_audit',
        'conditions': ['昨日二板', '首板、二板均非一字', '1 < 二板量/首板量 < 2',
                       '昨日估算流通市值 >= 100亿元'],
        'entry': '次日开盘低于涨停，盘中触板后委托；触板不等于成交',
    },
    'controlled_break': {
        'name': '两连板后温和休整', 'role': 'execution_audit',
        'conditions': ['原两连板首次收盘断板', '当日量/此前5日均量 <= 1.5',
                       '收盘 >= 含当日MA3', '收盘在当日高低区间上半部', '非一字跌停'],
        'entry': '信号已知后的下一市场交易日开盘；涨跌停开盘排除，需核验开盘成交',
    },
    'repair_first': {
        'name': '一进二低位修复观察组', 'role': 'observation_only',
        'conditions': ['昨日首板且非一字', '首板前60日位置 <= 30%', '首板前20日涨幅 < 0',
                       '按研究分类优先级排除窄平台突破：前10日区间<=15%且首板突破前20日高'],
        'entry': '只观察，不生成成交或收益',
    },
}
ENTRY_FIELDS = ('stage', 'first_word', 'stage_word', 'stage_volprev', 'stage_floatcap',
                'pre_pos60', 'pre_ret20', 'pre_range10', 'first_break20')
POST_FIELDS = ('height', 'vol5', 'ma3', 'pos', 'downword')


def stamp():
    return datetime.now(TZ).isoformat(timespec='seconds')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_new(path, value):
    # Existing experiment files are immutable; a new run needs a new directory.
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)


def numeric(row, key):
    value = row.get(key)
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def select_signals(study):
    """Whitelist S-close inputs. Future labels and B-date fields are never read."""
    signals = []
    for row in study.get('entries', []):
        strategy = None
        if (row.get('stage') == 2 and row.get('first_word') is False
                and row.get('stage_word') is False
                and numeric(row, 'stage_volprev') and 1 < row['stage_volprev'] < 2
                and numeric(row, 'stage_floatcap') and row['stage_floatcap'] >= 100):
            strategy = 'capacity_two'
        elif (row.get('stage') == 1 and row.get('first_word') is False
              and all(numeric(row, k) for k in ('pre_pos60', 'pre_ret20', 'pre_range10'))
              and row['pre_pos60'] <= 30 and row['pre_ret20'] < 0
              and not (row['pre_range10'] <= 15 and row.get('first_break20') is True)):
            strategy = 'repair_first'
        if strategy:
            signals.append(signal(row, strategy, ENTRY_FIELDS))
    for row in study.get('posts', []):
        if (row.get('kind') == '首次断板' and row.get('height') == 2
                and numeric(row, 'vol5') and row['vol5'] <= 1.5 and row.get('ma3') is True
                and numeric(row, 'pos') and row['pos'] >= .5 and row.get('downword') is False):
            signals.append(signal(row, 'controlled_break', POST_FIELDS))
    signals.sort(key=lambda s: (s['signal_date'], s['strategy'], s['code']))
    ids = [s['id'] for s in signals]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate signal IDs')
    return signals


def signal(row, strategy, fields):
    code, day = str(row['code']), row['date']
    if not re.fullmatch(r'(?:00|60)\d{4}', code):
        raise ValueError('This protocol only accepts Shanghai/Shenzhen main-board codes')
    datetime.strptime(day, '%Y-%m-%d')
    return dict(id=f'{strategy}:{code}:{day}', strategy=strategy, code=code,
                name=row.get('name', code), signal_date=day,
                role=RULES[strategy]['role'],
                features={k: row[k] for k in fields if k in row})


def freeze(study_path, calendar_path, output):
    study = read_json(study_path)
    if study.get('complete') != study.get('universe') or not study.get('universe'):
        raise ValueError('Incomplete research universe; do not freeze an apparently complete experiment')
    dates = read_json(calendar_path)
    if dates != sorted(set(dates)):
        raise ValueError('Calendar must contain sorted unique market sessions')
    for day in dates:
        datetime.strptime(day, '%Y-%m-%d')
    signals = select_signals(study)
    if any(s['signal_date'] not in dates for s in signals):
        raise ValueError('Signal day absent from supplied market calendar')
    if not dates or dates[0] > study['start'] or dates[-1] < study['end']:
        raise ValueError('Calendar does not cover the study interval')
    if any(not study['start'] <= s['signal_date'] <= study['end'] for s in signals):
        raise ValueError('Signal outside declared source interval')
    canonical_hash(signals)  # Validate JSON/finite values before creating a directory.
    output = Path(output)
    if output.exists():
        raise ValueError('Run directory already exists; no overwrite/resume of frozen rules')
    output.mkdir(parents=True)
    code_paths = ['tools/limit_entry_validation.py', 'terminal_backtest.py', 'terminal_strategy_exit.py']
    repo = Path(__file__).resolve().parents[1]
    protocol = dict(version=VERSION, frozen_at=stamp(), rules=RULES,
                    code_sha256={name: digest(repo / name) for name in code_paths},
                    fee_schedule=dict(FEE_SCHEDULE), intended_risk=dict(initial_cash=100000,
                    max_positions=5, usual_exposure_pct=70, holding_limit=True,
                    max_holding_sessions=5, continuous_limit_extension=True),
                    intended_exits=dict(take_profit_pct=10, stop_loss_pct=5,
                    speed_window_minutes=1, exit_module='terminal_strategy_exit.exit_decision',
                    next_minute_execution=True, t_plus_one=True),
                    historical_status='exploratory_reused_history',
                    forward_status='not_running',
                    execution_status='data_and_entry_evidence_audit_only',
                    warnings=['冻结不是启动线上账户；审计不生成收益',
                              '开盘或触板研究假设不等于已成交；没有L2排队不能确认封板买入',
                              '前后期均已参与探索，不宣称未触碰样本外验证'])
    manifest = dict(created_at=stamp(), protocol=protocol,
                    protocol_sha256=canonical_hash(protocol), study_sha256=digest(study_path),
                    calendar_sha256=digest(calendar_path), signals_sha256=canonical_hash(signals),
                    signal_counts=dict(Counter(s['strategy'] for s in signals)),
                    universe=study['universe'], calendar=dates,
                    source_start=study['start'], source_end=study['end'])
    write_new(output / 'manifest.json', manifest)
    write_new(output / 'signals.json', signals)
    return manifest


class LocalEvidence:
    def __init__(self, daily_dirs, ticks_dir):
        self.daily_dirs = [Path(p) for p in daily_dirs]
        self.ticks_dir = Path(ticks_dir)
        self.cache = {}

    def daily(self, code):
        if code not in self.cache:
            matches = [p / (code + '.json') for p in self.daily_dirs if (p / (code + '.json')).exists()]
            if not matches:
                raise ValueError('Daily history missing')
            payload = read_json(matches[0])
            if not payload.get('source') or not payload.get('retrieved_at'):
                raise ValueError('Daily provenance missing')
            rows = payload['rows']
            if [r['date'] for r in rows] != sorted({r['date'] for r in rows}):
                raise ValueError('Daily history unordered or duplicated')
            self.cache[code] = (dict((r['date'], r) for r in rows),
                                dict(source=payload['source'], retrieved_at=payload['retrieved_at'],
                                     sha256=digest(matches[0])))
        return self.cache[code]

    def ticks(self, code, day, raw):
        path = self.ticks_dir / f'{code}-{day}.json'
        if not path.exists():
            return dict(status='missing', day=day)
        try:
            payload = read_json(path)
            if payload.get('day') != day or not payload.get('source') or not payload.get('retrieved_at'):
                raise ValueError('Tick provenance/date missing or mismatched')
            bar = {k: float(raw[k]) for k in ('open', 'high', 'low', 'close', 'preclose')}
            bar['volume'] = float(raw['volume']) / 100  # BaoStock shares -> TDX lots.
            points = minute_points(payload, bar)
            ticks = [r for r in payload['rows'] if '09:25' <= r['time'] <= '15:00' and r['vol'] > 0]
            if abs(max(r['price'] for r in ticks) - bar['high']) > .011:
                raise ValueError('Tick high differs from daily high')
            if abs(min(r['price'] for r in ticks) - bar['low']) > .011:
                raise ValueError('Tick low differs from daily low')
            return dict(status='verified', day=day, source=payload['source'],
                        retrieved_at=payload['retrieved_at'], sha256=digest(path),
                        points=points, ticks=ticks)
        except (ValueError, TypeError, KeyError) as exc:
            return dict(status='invalid', day=day, reason=str(exc))


def limit_price(preclose, rate):
    return float((Decimal(str(preclose)) * (1 + Decimal(str(rate)))).quantize(
        Decimal('.01'), rounding=ROUND_HALF_UP))


def entry_evidence(strategy, raw, checked):
    """Audit evidence, never turn a touch into a ledger fill."""
    if raw.get('tradestatus') != '1' or not raw.get('volume') or float(raw['volume']) <= 0:
        return dict(status='suspended_or_no_trade')
    if raw.get('isST') != '0':
        return dict(status='excluded_st')
    values = [float(raw[k]) for k in ('open', 'high', 'low', 'close', 'preclose', 'volume')]
    if not all(math.isfinite(v) and v > 0 for v in values):
        raise ValueError('Daily price/volume is nonfinite or nonpositive')
    o, h, l, c = values[:4]
    if h < max(o, l, c) or l > min(o, h, c):
        raise ValueError('Daily OHLC range is inconsistent')
    upper, lower = [limit_price(raw['preclose'], r) for r in (.1, -.1)]
    if strategy == 'capacity_two':
        if o == h == l == c == upper:
            return dict(status='excluded_one_word')
        if o >= upper:
            return dict(status='opening_limit_special_case')
        if h < upper - .005:
            return dict(status='no_limit_touch')
        if checked['status'] != 'verified':
            return dict(status='entry_ticks_unverified')
        ticks = checked['ticks']
        touched = next((r for r in ticks if r['time'] >= '09:30' and r['price'] >= upper - .005), None)
        if not touched:
            return dict(status='minute_touch_not_found')
        through = next((r for r in ticks if r['time'] > touched['time'] and r['price'] < upper - .005), None)
        return dict(status='price_through_observed' if through else 'queue_fill_unconfirmed',
                    limit_price=upper, touch_time=touched['time'],
                    through_time=through['time'] if through else None,
                    fill_confirmed=False, note='后续成交穿价仅为保守执行情景的证据，不确认真实订单成交')
    if not lower < o < upper:
        return dict(status='excluded_limit_open')
    if checked['status'] != 'verified':
        return dict(status='entry_ticks_unverified')
    first = next((r for r in checked['ticks'] if r['time'] in ('09:25', '09:30')), None)
    if not first or abs(first['price'] - o) > .005:
        return dict(status='opening_trade_unverified')
    return dict(status='opening_trade_observed', opening_price=o,
                opening_time=first['time'], fill_confirmed=False,
                note='核验到开盘成交，未模拟数量、委托先后或组合现金')


def audit(run_dir, daily_dirs, ticks_dir):
    run_dir = Path(run_dir)
    manifest, signals = read_json(run_dir / 'manifest.json'), read_json(run_dir / 'signals.json')
    if manifest['protocol_sha256'] != canonical_hash(manifest['protocol']):
        raise ValueError('Frozen protocol changed')
    if manifest['signals_sha256'] != canonical_hash(signals):
        raise ValueError('Frozen signals changed')
    repo = Path(__file__).resolve().parents[1]
    if any(digest(repo / name) != sha for name, sha in manifest['protocol']['code_sha256'].items()):
        raise ValueError('Execution code changed after freeze')
    if manifest['protocol']['version'] != VERSION:
        raise ValueError('Protocol version mismatch')
    evidence = LocalEvidence(daily_dirs, ticks_dir)
    dates = manifest['calendar']; index = {d: i for i, d in enumerate(dates)}
    results, missing, counts = [], set(), Counter()
    for item in signals:
        if item['role'] == 'observation_only':
            continue
        result = dict(id=item['id'], strategy=item['strategy'], code=item['code'],
                      signal_date=item['signal_date'], checked_at=stamp())
        i = index[item['signal_date']] + 1
        if i >= len(dates):
            result['status'] = 'right_censored_calendar'
        else:
            try:
                daily, provenance = evidence.daily(item['code'])
                result.update(daily_provenance=provenance, entry_day=dates[i])
                raw = daily.get(dates[i])
                if raw is None:
                    result['status'] = 'entry_daily_missing'
                else:
                    # Reject provably ineligible daily cases before requesting minute evidence.
                    preliminary = entry_evidence(item['strategy'], raw, dict(status='missing'))
                    if preliminary['status'] != 'entry_ticks_unverified':
                        result.update(preliminary)
                    else:
                        checked = evidence.ticks(item['code'], dates[i], raw)
                        result.update(entry_evidence(item['strategy'], raw, checked))
                        result['entry_tick_status'] = checked['status']
                        result['entry_tick_reason'] = checked.get('reason')
                        result['entry_tick_sha256'] = checked.get('sha256')
                        if checked['status'] == 'missing':
                            missing.add((item['code'], dates[i]))
                        future = []
                        for day in dates[i + 1:i + 5]:
                            if day not in daily:
                                future.append(dict(day=day, status='daily_missing'))
                                continue
                            check = evidence.ticks(item['code'], day, daily[day])
                            future.append({k: check[k] for k in ('day', 'status', 'reason', 'sha256') if k in check})
                            if check['status'] == 'missing':
                                missing.add((item['code'], day))
                        result.update(exit_data=future, full_five_session_data=(len(future) == 4
                                      and all(v['status'] == 'verified' for v in future)
                                      and checked['status'] == 'verified'))
            except (ValueError, KeyError, TypeError) as exc:
                result.update(status='data_error', reason=str(exc))
        counts[result['status']] += 1
        results.append(result)
    report = dict(version=VERSION, completed_at=stamp(), status='entry_and_minute_data_audit_completed',
                  protocol_sha256=manifest['protocol_sha256'], signal_counts=manifest['signal_counts'],
                  audited=len(results), counts=dict(counts),
                  full_five_session_data=sum(r.get('full_five_session_data', False) for r in results),
                  missing_tick_days=[dict(code=c, day=d) for c, d in sorted(missing)], results=results,
                  actual_fills=0, pnl_status='not_calculated',
                  next_step='补齐分钟缺口；在冻结条件下核验成交情景及原1分钟退出，再做独立资金组合验证',
                  forward_status='not_running')
    audit_path = run_dir / 'audit.json'
    if audit_path.exists():
        audit_path = run_dir / ('audit-' + datetime.now(TZ).strftime('%Y%m%dT%H%M%S')
                                + '-' + uuid.uuid4().hex[:8] + '.json')
    report['snapshot_file'] = audit_path.name
    write_new(audit_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('freeze'); p.add_argument('--study', required=True)
    p.add_argument('--calendar', required=True); p.add_argument('--output', required=True)
    p = commands.add_parser('audit'); p.add_argument('--run', required=True)
    p.add_argument('--daily-dir', action='append', required=True); p.add_argument('--ticks-dir', required=True)
    args = parser.parse_args()
    if args.command == 'freeze':
        value = freeze(args.study, args.calendar, args.output)
        summary = dict(protocol_sha256=value['protocol_sha256'], signal_counts=value['signal_counts'])
    else:
        value = audit(args.run, args.daily_dir, args.ticks_dir)
        summary = {k: value[k] for k in ('status', 'audited', 'counts', 'full_five_session_data', 'pnl_status', 'snapshot_file')}
        summary['missing_tick_days'] = len(value['missing_tick_days'])
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
