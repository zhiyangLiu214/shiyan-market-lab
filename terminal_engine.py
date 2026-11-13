"""Explainable daily-bar factors. No future bars or inferred order-flow data."""
from __future__ import annotations

import math
import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

TZ = ZoneInfo('Asia/Shanghai')
FACTOR_VERSION = 'daily-v1.0'
PLAN_VERSION = 'pressure-v1.0'


def now_text():
    return datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')


def validate_code(code):
    if not re.fullmatch(r'(?:[03689]\d{5}|4\d{5})', code or ''):
        raise ValueError('请输入沪深京 A 股六位数字代码')
    return code


def normalize_weights(values):
    weights = [float(v) for v in values]
    if len(weights) != 3 or any(not math.isfinite(v) or v < 0 or v > 100 for v in weights):
        raise ValueError('权重须为 0～100 之间的三个数字')
    total = sum(weights)
    if total <= 0:
        raise ValueError('至少保留一项非零权重')
    return [v / total for v in weights]


def normalize_bars(frame):
    aliases = {'日期': 'date', '时间': 'date', '开盘': 'open', '收盘': 'close',
               '最高': 'high', '最低': 'low', '成交量': 'volume', '成交额': 'amount'}
    data = frame.rename(columns=aliases).copy()
    required = ['date', 'open', 'close', 'high', 'low', 'volume']
    if any(key not in data for key in required):
        raise ValueError('行情缺少必要的 OHLCV 字段')
    data['date'] = pd.to_datetime(data['date'], errors='coerce')
    for key in required[1:] + (['amount'] if 'amount' in data else []):
        data[key] = pd.to_numeric(data[key], errors='coerce')
    data = data.replace([float('inf'), float('-inf')], float('nan')).dropna(subset=required)
    data = data[(data['close'] > 0) & (data['open'] > 0) & (data['low'] > 0)
                & (data['high'] >= data[['open', 'close', 'low']].max(axis=1))
                & (data['low'] <= data[['open', 'close']].min(axis=1)) & (data['volume'] >= 0)]
    return data.sort_values('date').drop_duplicates('date', keep='last').reset_index(drop=True)


def pressure_candidates(history, period):
    """Only past bars: local peaks require two later bars already available."""
    levels = []
    for window in ((20, 60, 120) if period == '日线' else (5, 15, 30, 60)):
        if len(history) < window:
            continue
        sample = history.tail(window)
        row = sample.loc[sample.high.idxmax()]
        levels.append(dict(price=float(row.high), date=row.date.strftime('%Y-%m-%d %H:%M'),
                           basis=f'前 {window} {"个交易日" if period == "日线" else "根分钟线"}最高价'))
    sample = history.tail(120)
    for i in range(2, len(sample)-2):
        row = sample.iloc[i]
        if row.high > sample.high.iloc[i-2:i].max() and row.high >= sample.high.iloc[i+1:i+3].max():
            levels.append(dict(price=float(row.high), date=row.date.strftime('%Y-%m-%d %H:%M'),
                               basis=f'{period}摆动高点（左右各 2 根确认）'))
    return levels


def pressure_plan(reference, stop, candidates, basis, date, period):
    """Nearby historical resistance first; risk multiples remain separate projections."""
    risk = reference - stop if reference is not None and stop is not None else None
    zones = []
    if reference is not None:
        tolerance = max(.01, reference * .003)
        for level in sorted(candidates, key=lambda x: x['price']):
            value = round(level['price'], 2)
            if value <= reference:
                continue  # Broken or below-reference highs cannot be upside targets.
            if zones and value - zones[-1]['price'] <= tolerance:
                zones[-1]['high'] = max(zones[-1]['high'], value)
                if level['basis'] not in zones[-1]['basis']:
                    zones[-1]['basis'] += '；' + level['basis']
            else:
                zones.append(dict(price=value, high=value, basis=level['basis'], date=level['date']))
    levels = zones[:2]
    first = levels[0]['price'] if levels else None
    second = levels[1]['price'] if len(levels) > 1 else None
    rr = round((first-reference)/risk, 2) if first is not None and risk and risk > 0 else None
    assessment = ('未触发分时信号' if reference is None else
                  '暂无明确上方压力位' if first is None else
                  '首压力空间小于止损距离' if rr is not None and rr < 1 else
                  '首压力盈亏比低于 1.5' if rr is not None and rr < 1.5 else
                  '上方存在压力空间，等待量价确认')
    return dict(reference=reference, stop=stop, target1=first, target2=second, rr=rr,
                rr2=round((second-reference)/risk, 2) if second is not None and risk and risk > 0 else None,
                upside=round((first/reference-1)*100, 2) if first is not None else None,
                rr_target1=round(reference+risk*2, 2) if risk and risk > 0 else None,
                rr_target2=round(reference+risk*3, 2) if risk and risk > 0 else None,
                levels=levels, assessment=assessment, basis=basis, date=date, period=period,
                target_basis='历史压力区域下沿；相邻高点价差不超过参考价的 0.3% 时合并',
                version=PLAN_VERSION)


def analyze(frame, code, name='', board='', weights=(.4, .3, .3), demo=False, score_only=False):
    data = normalize_bars(frame)
    if len(data) < 35:
        raise ValueError('有效日线不足 35 根，暂不计算三维评分')
    weights = normalize_weights(weights)
    closes, volumes = data.close, data.volume
    latest, previous = data.iloc[-1], data.iloc[-2]
    price = round(float(latest.close), 2)
    ma = {n: float(closes.tail(n).mean()) for n in (5, 10, 20)}
    # Comparison excludes today's volume; stable denominator for a current-day bar.
    avg_volume = float(volumes.iloc[-6:-1].mean())
    volume_ratio = float(latest.volume / avg_volume) if avg_volume > 0 else None
    dif = closes.ewm(span=12, adjust=False).mean() - closes.ewm(span=26, adjust=False).mean()
    hist = 2 * (dif - dif.ewm(span=9, adjust=False).mean())
    recent = data.iloc[-21:-1]
    support, resistance = float(recent.low.min()), float(recent.high.max())
    breakout = price > resistance
    structure_score = 85 if breakout else (65 if price >= ma[20] else 30)
    change5 = (price / float(closes.iloc[-6]) - 1) * 100
    trend_score = min(100, max(0, 40 + change5 * 2.5 + (20 if price >= ma[20] else 0)))
    alignment = ma[5] > ma[10] > ma[20]
    momentum_score = (40 if alignment else 10) + (30 if hist.iloc[-1] > 0 else 0)
    momentum_score += (30 if volume_ratio is not None and volume_ratio >= 1.2 else 10)
    momentum_score = min(100, momentum_score)
    scores = [structure_score, round(trend_score, 1), momentum_score]
    score = round(sum(v * w for v, w in zip(scores, weights)), 1)
    reasons = [f'收盘 {price:.2f}，前 20 日最高 {resistance:.2f}；' + ('已突破' if breakout else '尚未突破'),
               f'近 5 日涨幅 {change5:+.2f}%，MA20 {ma[20]:.2f}',
               ('均线多头排列' if alignment else '均线尚未多头排列') +
               f'；MACD 柱 {hist.iloc[-1]:.3f}；量比 ' + (f'{volume_ratio:.2f}' if volume_ratio is not None else '未知')]
    if score_only:
        return dict(score=score,scores=scores,weights=weights,reasons=reasons,
                    volume_ratio=round(volume_ratio,2) if volume_ratio is not None else None)
    pct = (price / float(previous.close) - 1) * 100
    total_volume = float(volumes.tail(5).sum())
    # AkShare daily volume is in lots, amount in CNY. Never fabricate VWAP when amount is missing.
    amount5 = data.amount.tail(5) if 'amount' in data else None
    vwap = (float(amount5.sum()) / (total_volume * 100)
            if amount5 is not None and amount5.notna().all() and total_volume > 0 else None)
    # A single versioned daily plan shared by every screen and chart.
    stop = round(min(price * .97, max(support, price * .92)), 2)
    plan = pressure_plan(price, stop, pressure_candidates(data.iloc[:-1], '日线'),
                         '末根日线收盘价', latest.date.strftime('%Y-%m-%d'), '日线')
    plan.update(entry_low=round(price * .99, 2), entry_high=price,
                as_of=datetime.now(TZ).strftime('%H:%M')
                if latest.date.strftime('%Y-%m-%d') == datetime.now(TZ).strftime('%Y-%m-%d') else '15:00')
    signals = []
    rolling_ma20 = closes.rolling(20).mean()
    prior_volume = volumes.shift(1).rolling(5).mean()
    # Every historical signal uses only values available on that bar.
    event = ((closes > rolling_ma20) & (closes.shift(1) <= rolling_ma20.shift(1))
             & (volumes >= prior_volume * 1.2))
    for index in data.index[event]:
        signals.append({'date': data.loc[index, 'date'].strftime('%Y-%m-%d'),
                        'price': round(float(closes.loc[index]), 2), 'kind': '放量上穿 MA20'})
    bars = []
    for i, row in data.tail(120).iterrows():
        bars.append({'date': row.date.strftime('%Y-%m-%d'),
                     **{key: round(float(row[key]), 3) for key in ['open', 'close', 'high', 'low', 'volume']},
                     **{f'ma{n}': round(float(closes.rolling(n).mean().loc[i]), 3)
                        for n in (5, 10, 20) if pd.notna(closes.rolling(n).mean().loc[i])}})
    return {'code': code, 'name': name or code, 'board': board or '未提供板块',
            'price': round(price, 2), 'pct': round(pct, 2), 'date': latest.date.strftime('%Y-%m-%d'),
            'amplitude': round((float(latest.high) - float(latest.low)) / float(previous.close) * 100, 2),
            'scores': scores, 'score': score, 'weights': weights, 'reasons': reasons,
            'ma': {str(k): round(v, 2) for k, v in ma.items()}, 'volume_ratio': round(volume_ratio, 2) if volume_ratio is not None else None,
            'vwap5': round(vwap, 2) if vwap is not None else None,
            'support': round(support, 2), 'resistance': round(resistance, 2),
            'macd': round(float(hist.iloc[-1]), 4), 'change5': round(change5, 2),
            'plan': plan, 'bars': bars, 'signals': signals[-12:], 'factor_version': FACTOR_VERSION,
            'source': '合成演示数据' if demo else '东方财富日线 / AkShare，不复权',
            'retrieved_at': now_text(), 'demo': demo}


DEMO_GROUPS = [
    ('算力设备', 3.68, [('000001', '示例·云算科技'), ('000002', '示例·光域互联'), ('000003', '示例·数维信息'), ('000004', '示例·芯际科技')]),
    ('高端制造', 2.83, [('000005', '示例·精工智造'), ('000006', '示例·远航装备'), ('000007', '示例·新程机电'), ('000008', '示例·星河动力')]),
    ('创新医药', 2.16, [('000009', '示例·生命科学'), ('000010', '示例·新源生物'), ('000011', '示例·康研医药'), ('000012', '示例·安和医疗')]),
]


def demo_history(code):
    rng = random.Random(int(code))
    dates = pd.bdate_range(end='2026-11-09', periods=130)
    price = 12 + int(code) % 35
    rows = []
    for i, date in enumerate(dates):
        opening = price * (1 + rng.uniform(-.012, .012))
        price = max(1, opening * (1 + rng.uniform(-.023, .028) + (.009 if i > 105 else 0)))
        volume = rng.randint(20000, 80000) * (1.8 if i > 124 else 1)
        rows.append({'date': date, 'open': opening, 'close': price,
                     'high': max(opening, price) * (1 + rng.uniform(.002, .018)),
                     'low': min(opening, price) * (1 - rng.uniform(.002, .018)),
                     'volume': volume, 'amount': volume * 100 * (opening + price) / 2})
    return pd.DataFrame(rows)


def select_mainline_roles(stocks):
    """Choose distinct roles using quotes, never a technical-factor rank."""
    pool = [dict(s) for s in stocks if s.get('price', 0) > 0 and s.get('pct', 0) > 0]
    if not pool:
        return []
    leader = max(pool, key=lambda s: (s['pct'], s.get('amount', 0), s['code']))
    leader.update(role='龙头候选', role_reason='样本内涨幅领先，观察板块带动与强度持续性。')
    selected = [leader]
    remaining = [s for s in pool if s['code'] != leader['code']]
    if remaining:
        trend = max(remaining, key=lambda s: (s.get('amount', 0), s.get('volume_ratio', 0), s['pct']))
        trend.update(role='趋势龙候选', role_reason='剩余样本中成交额领先；趋势持续性仍需结合量价确认。')
        selected.append(trend)
        remaining = [s for s in remaining if s['code'] != trend['code']]
    if remaining:
        follow = min(remaining, key=lambda s: (s['pct'], -s.get('amount', 0)))
        follow.update(role='补涨观察', role_reason='同板块正涨幅、相对涨幅较低；仅作补涨观察，不代表即将上涨。')
        selected.append(follow)
    return selected


def scan_options(body):
    pct = float(body.get('min_board_pct', 2))
    count = int(body.get('min_companies', 5))
    if not math.isfinite(pct) or not 0 <= pct <= 15 or count not in (5, 6, 7, 8):
        raise ValueError('板块涨幅门槛应为 0～15%，达标家数应为 5～8 家')
    return {'min_board_pct': pct, 'min_companies': count}


def qualifies_momentum(detail):
    ma = detail['ma']
    return (ma['5'] > ma['10'] > ma['20'] and detail['macd'] > 0
            and detail['volume_ratio'] is not None and detail['volume_ratio'] >= 1.2)


DEMO_SCAN_GROUPS = [
    (name, pct, stocks + [(f'{1000 + i * 10 + j:06d}', f'示例·共振{i + 1}{j + 1}') for j in range(4)])
    for i, (name, pct, stocks) in enumerate(DEMO_GROUPS)
] + [('新能源材料', 2.45, [(f'{101 + j:06d}', f'示例·新材{j + 1}') for j in range(8)])]


def demo_minutes(code):
    daily = demo_history(code)
    opening, ending = float(daily.iloc[-1].open), float(daily.iloc[-1].close)
    day = daily.iloc[-1].date.strftime('%Y-%m-%d')
    times = list(pd.date_range(day + ' 09:31', day + ' 11:30', freq='min'))
    times += list(pd.date_range(day + ' 13:01', day + ' 15:00', freq='min'))
    rng = random.Random(int(code) + 73)
    rows, previous = [], opening
    for i, stamp in enumerate(times):
        progress = i / (len(times) - 1)
        price = opening + (ending - opening) * progress + math.sin(progress * 22) * opening * .006 * (1 - progress)
        volume = rng.randint(200, 1100) * (4 if i in (20, 55, 150) else 1)
        rows.append({'时间': stamp, '开盘': previous, '收盘': price,
                     '最高': max(price, previous), '最低': min(price, previous),
                     '成交量': volume, '成交额': volume * 100 * (price + previous) / 2})
        previous = price
    return pd.DataFrame(rows)


def intraday_analysis(frame, code, demo=False):
    # Minute feeds may use zero for open; it is not an OHLC daily bar.
    data = frame.rename(columns={'时间': 'date', '收盘': 'close', '最低': 'low', '最高': 'high',
                                 '成交量': 'volume', '成交额': 'amount'}).copy()
    if any(key not in data for key in ('date', 'close', 'volume', 'amount')):
        raise ValueError('分时数据缺少价格、量或成交额')
    data['date'] = pd.to_datetime(data['date'], errors='coerce')
    if 'low' not in data:
        data['low'] = float('nan')
    high_available = 'high' in data
    if not high_available:
        data['high'] = data['close']
    for key in ('close', 'low', 'high', 'volume', 'amount'):
        data[key] = pd.to_numeric(data[key], errors='coerce')
    data = data.replace([float('inf'), float('-inf')], float('nan')).dropna(subset=['date', 'close', 'volume'])
    data = data[(data.close > 0) & (data.volume >= 0)].sort_values('date').drop_duplicates('date', keep='last')
    stamps = data.date.dt.strftime('%H:%M')
    data = data[((stamps >= '09:30') & (stamps <= '11:30'))
                | ((stamps >= '13:00') & (stamps <= '15:00'))]
    if not demo:
        # The currently forming minute is not a confirmed event bar.
        cutoff = datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)
        data = data[data.date < cutoff]
    if data.empty:
        raise ValueError('没有有效交易时段内的分时数据')
    data = data[data.date.dt.date == data.date.max().date()].reset_index(drop=True)
    if 'amount' not in data or data.amount.isna().any() or (data.amount < 0).any():
        raise ValueError('缺少有效成交额，无法计算分时均价')
    average = data.amount.cumsum() / (data.volume.cumsum() * 100).replace(0, float('nan'))
    baseline = data.volume.shift().rolling(5, min_periods=5).mean()
    signals, last_signal, first_signal_index = [], {}, None
    for i in range(6, len(data)):
        row, prev = data.iloc[i], data.iloc[i - 1]
        if not math.isfinite(average.iloc[i]):
            continue
        kinds = []
        if baseline.iloc[i] > 0 and row.volume >= baseline.iloc[i] * 2 and row.close > prev.close:
            kinds.append('放量上攻')
        if prev.close <= average.iloc[i - 1] and row.close > average.iloc[i]:
            kinds.append('上穿均价')
        if (abs(row.low / average.iloc[i] - 1) <= .002 and row.close > prev.close
              and row.close >= average.iloc[i] and prev.close >= average.iloc[i - 1]):
            kinds.append('均价附近承接')
        # A causal approximation: only earlier minutes, never the future morning low.
        prior = data.iloc[max(0, i-20):i]
        weak_before = any(data.close.iloc[j] < average.iloc[j]
                          for j in range(max(0, i-5), i) if pd.notna(average.iloc[j]))
        if (weak_before and prev.close <= average.iloc[i-1] and row.close > average.iloc[i]
                and row.close > float(prior.close.min()) * 1.002
                and baseline.iloc[i] > 0 and row.volume >= baseline.iloc[i] * 1.1):
            kinds.append('弱转强')
        for kind in kinds:
            if i - last_signal.get(kind, -10) < 8 or len(signals) >= 24:
                continue
            if first_signal_index is None:
                first_signal_index = i
            signals.append({'time': row.date.strftime('%H:%M'), 'price': round(float(row.close), 2),
                            'confirmed_at': row.date.strftime('%H:%M'), 'kind': kind,
                            'reason': f'价格 {row.close:.2f} / 当时均价 {average.iloc[i]:.2f}；前5根均量 {baseline.iloc[i]:.0f} 手'})
            last_signal[kind] = i
    reference = signals[0]['price'] if signals else None
    # Freeze resistance at the first signal; later intraday highs must not rewrite it.
    prior = data.iloc[:first_signal_index] if first_signal_index is not None else data.iloc[:0]
    prior = prior[prior.high.notna() & (prior.high >= prior.close)]
    candidates = pressure_candidates(prior, '分钟线')
    if not high_available:
        for level in candidates:
            level['basis'] += '（缺最高价，采用分钟收盘价）'
    plan = pressure_plan(reference, round(reference*.97, 2) if reference else None, candidates,
                         '首个已触发分时信号价格', data.iloc[-1].date.strftime('%Y-%m-%d'), '分钟线')
    plan.update(target=plan['target1'], as_of=signals[0]['time'] if signals else None)
    opening = data.head(5)
    opening_complete = (len(opening) == 5 and opening.iloc[0].date.strftime('%H:%M') in ('09:30', '09:31')
                        and all((opening.date.iloc[i]-opening.date.iloc[i-1]).total_seconds() == 60
                                for i in range(1, 5)))
    opening_info = dict(available=opening_complete, label='开盘前5分钟量能（非集合竞价）',
                        volume=float(opening.volume.sum()) if opening_complete else None,
                        amount=float(opening.amount.sum()) if opening_complete else None,
                        start=opening.iloc[0].date.strftime('%H:%M'),
                        end=opening.iloc[-1].date.strftime('%H:%M'))
    return {'code': code, 'date': data.iloc[-1].date.strftime('%Y-%m-%d'), 'demo': demo,
            'as_of': data.iloc[-1].date.strftime('%Y-%m-%d %H:%M'), 'adjustment': 'none',
            'signal_version': 'minute-causal-v2', 'opening': opening_info,
            'source': '合成分时样本' if demo else '东方财富分钟行情 / AkShare', 'retrieved_at': now_text(),
            'points': [{'time': row.date.strftime('%H:%M'), 'price': round(float(row.close), 3),
                        'volume': float(row.volume), 'amount': float(row.amount),
                        'average': round(float(average.iloc[i]), 3) if pd.notna(average.iloc[i]) else None}
                       for i, row in data.iterrows()],
            'signals': signals,
            'plan': plan}
