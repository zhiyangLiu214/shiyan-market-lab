"""Point-in-time bottom-reversal research rules, independent of live pools.

Volume on rising minutes is a price/volume proxy, not aggressive buy volume.
All thresholds are initial hypotheses, not fitted profitability claims.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta


def optimistic_late_shape(history, opening, early_high, daily_low, last_interval_high,
                          parameters=None):
    """Necessary envelope only, never a signal or selection-time price.

    A future-inclusive range can safely reject an impossible shape, provided
    the actual signal is always rebuilt using strictly earlier trades. Use
    the smallest possible low and a close at the earlier high if possible:
    a still higher close raises the minimum high and hurts wick share.
    """
    close = min(last_interval_high, max(early_high, opening+1e-7))
    return shape(history+[dict(open=opening, high=max(early_high, close),
                               low=daily_low, close=close)],
                 parameters or Parameters())


def late_partial(tick_data, opening, checkpoint, cutoff='14:50', parameters=None):
    """Build a late-day bar strictly before cutoff, checking a known 14:30 prefix.

    Later sampling is not independently verified minute OHLC. Keep that
    limitation explicit in any replay using this bar.
    """
    p = parameters or Parameters()
    if cutoff != '14:50':
        raise ValueError('本轮冻结的晚盘时点为14:50')
    path_evidence(tick_data, opening, checkpoint, '14:30', p)
    rows = [r for r in tick_data['rows'] if r.get('vol', 0) > 0
            and '09:25' <= r['time'] < cutoff]
    late = {r['time'] for r in rows if '14:30' <= r['time'] < cutoff}
    if len(late) < 18 or not rows or rows[-1]['time'] != '14:49':
        raise ValueError('14:30至14:50采样分钟覆盖不足或末分钟缺失')
    prices = [float(r['price']) for r in rows]
    return dict(open=opening, high=max([opening]+prices), low=min(prices),
                close=prices[-1], volume=sum(float(r['vol']) for r in rows))


@dataclass(frozen=True)
class Parameters:
    low_window: int = 5
    wick_share: float = .40
    close_location: float = .70
    minimum_dip: float = .01
    minimum_observed_minutes: int = 180
    minimum_rebound_minutes: int = 30
    price_tolerance: float = .021
    volume_tolerance: float = .01


def shape(bars, parameters=Parameters()):
    """Include the reversal day in the configured window; permit a tied low."""
    if len(bars) < parameters.low_window:
        return False
    b = bars[-1]
    o, h, l, c = (float(b[k]) for k in ('open', 'high', 'low', 'close'))
    if o <= 0 or h <= l or not l <= min(o, c) <= max(o, c) <= h:
        return False
    lower = min(o, c)-l
    upper = h-max(o, c)
    prior_wicks = [min(x['open'], x['close'])-x['low'] for x in bars[-3:-1]]
    return bool(c > o and l <= min(x['low'] for x in bars[-parameters.low_window:-1])
                and lower >= max(prior_wicks) and lower > upper
                and lower/(h-l) >= parameters.wick_share
                and (c-l)/(h-l) >= parameters.close_location
                and (o-l)/o >= parameters.minimum_dip)


def strengthening(bars, current_volume, prior_same_time_volumes, parameters=Parameters()):
    """Yesterday is a complete daily bar; today is the selection-time bar."""
    if len(bars) < parameters.low_window+1 or not shape(bars[:-1], parameters):
        return dict(base=False, close_break=False, holds_low=False)
    if len(prior_same_time_volumes) != 2 or any(v <= 0 for v in prior_same_time_volumes):
        raise ValueError('缺前两交易日相同时点成交量')
    b = bars[-1]
    resistance = max(x['high'] for x in bars[-3:-1])
    base = (b['close'] > b['open'] and b['close'] > bars[-2]['close']
            and b['high'] > resistance and current_volume > max(prior_same_time_volumes))
    return dict(base=bool(base), close_break=bool(base and b['close'] > resistance),
                holds_low=bool(b['low'] >= bars[-2]['low']),
                volume_multiple=current_volume/max(prior_same_time_volumes),
                breakout_pct=(b['close']/resistance-1)*100)


def path_evidence(tick_data, opening, partial, cutoff='14:30', parameters=Parameters()):
    """Use only minute-labelled trades strictly before the cutoff minute.

    Input timestamps identify the start of a sampled minute. Excluding cutoff
    trades avoids including unknown trades from the minute beginning at 14:30.
    No full-day price, volume, or future rows validate the selection-time path.
    """
    ticks = [r for r in tick_data['rows'] if r.get('vol', 0) > 0 and '09:25' <= r['time'] < cutoff]
    if not ticks or any(b['time'] < a['time'] for a, b in zip(ticks, ticks[1:])):
        raise ValueError('选股时点前分笔为空或顺序异常')
    total = sum(float(r['vol']) for r in ticks)
    if partial['volume'] <= 0 or abs(total/partial['volume']-1) > parameters.volume_tolerance:
        raise ValueError('时点前分笔与30分钟累计成交量不一致')
    prices = [float(r['price']) for r in ticks]
    tol = parameters.price_tolerance
    if (abs(prices[-1]-partial['close']) > tol
            or abs(min(prices)-partial['low']) > tol
            or abs(max([opening]+prices)-partial['high']) > tol):
        raise ValueError('时点前采样价格与30分钟高低收不一致')
    byminute = {}
    auction_volume = 0.
    closing_auction_volume = 0.
    for r in ticks:
        if r['time'] < '09:30':
            auction_volume += r['vol']
            continue
        if r['time'] == '15:00':
            closing_auction_volume += r['vol']
            continue
        if not ('09:30' <= r['time'] <= '11:30' or '13:00' <= r['time'] < '15:00'):
            raise ValueError('连续竞价分笔时间异常')
        # Some feeds label the morning's final executions exactly 11:30.
        # Merge that endpoint into the last morning minute, never add a
        # fictitious extra trading minute or discard legitimate volume.
        stamp = '11:29' if r['time'] == '11:30' else r['time']
        v = byminute.setdefault(stamp, dict(close=0., volume=0., low=float('inf')))
        v['close'] = float(r['price'])
        v['volume'] += float(r['vol'])
        v['low'] = min(v['low'], float(r['price']))
    if len(byminute) < parameters.minimum_observed_minutes:
        raise ValueError('有效成交分钟覆盖不足')
    times = sorted(byminute)
    low_time = min(times, key=lambda t: byminute[t]['low'])
    all_times = []
    for hour, minute, count in [(9, 30, 120), (13, 0, 120)]:
        start = datetime(2000, 1, 1, hour, minute)
        all_times += [(start+timedelta(minutes=i)).strftime('%H:%M') for i in range(count)]
    rebound_minutes = sum(low_time < t < cutoff for t in all_times)
    prior = opening
    up = down = flat = 0.
    for t in times:
        v = byminute[t]
        if v['close'] > prior:
            up += v['volume']
        elif v['close'] < prior:
            down += v['volume']
        else:
            flat += v['volume']
        prior = v['close']
    before = [byminute[t]['volume'] for t in times if t < low_time]
    after = [byminute[t]['volume'] for t in times if t > low_time]
    recovery_activity = ((sum(after)/len(after))/(sum(before)/len(before))
                         if before and after and sum(before) > 0 else None)
    sequence = (min(prices) < opening and prices[-1] > opening
                and abs(byminute[low_time]['low']-min(prices)) <= tol
                and rebound_minutes >= parameters.minimum_rebound_minutes)
    return dict(sequence=bool(sequence), volume_confirmed=bool(sequence and down > 0 and up > down),
                up_volume=up, down_volume=down, flat_volume=flat, auction_volume=auction_volume,
                closing_auction_volume=closing_auction_volume,
                directional_balance=(up-down)/(up+down) if up+down else 0.,
                up_down_ratio=up/down if down else None,
                recovery_activity=recovery_activity, low_time=low_time,
                rebound_minutes=rebound_minutes, observed_minutes=len(times),
                parameters=asdict(parameters))


def recovery_leg_evidence(tick_data, opening, previous_close, partial,
                          cutoff='14:30', parameters=Parameters()):
    """v2: compare mean volume before the low and on the recovery below yesterday's close.

    Boundary minutes (the low and the first crossing of yesterday's close)
    are excluded: sampled minute volume cannot be split into before/after
    crossing volume. Post-crossing volume never qualifies the recovery.
    """
    if previous_close <= 0:
        raise ValueError('昨收参考价无效')
    evidence = path_evidence(tick_data, opening, partial, cutoff, parameters)
    minutes = {}
    for row in tick_data['rows']:
        t = row['time']
        if not (row.get('vol', 0) > 0 and '09:30' <= t < cutoff and t != '15:00'):
            continue
        t = '11:29' if t == '11:30' else t
        bar = minutes.setdefault(t, dict(volume=0., prices=[]))
        bar['volume'] += float(row['vol'])
        bar['prices'].append(float(row['price']))
    low_time = evidence['low_time']
    crossing = next((t for t in sorted(minutes) if t > low_time
                     and max(minutes[t]['prices']) >= previous_close), None)
    down = [b['volume'] for t, b in sorted(minutes.items()) if t < low_time]
    recovery = [b['volume'] for t, b in sorted(minutes.items())
                if t > low_time and (crossing is None or t < crossing)]
    after = [b['volume'] for t, b in sorted(minutes.items()) if crossing and t > crossing]
    ratio = ((sum(recovery)/len(recovery))/(sum(down)/len(down))
             if len(down) >= 3 and len(recovery) >= 3 and sum(down) > 0 else None)
    recovered = (crossing is not None or partial['close'] > min(
        p for b in minutes.values() for p in b['prices']))
    boundary_ambiguous = max(minutes[low_time]['prices']) >= previous_close
    confirmed = bool(evidence['sequence'] and not boundary_ambiguous and recovered
                     and ratio is not None and ratio > 1)
    return dict(evidence, rule_version='recovery-leg-v2',
                volume_confirmed=confirmed, previous_close=previous_close,
                recovery_crossing_minute=crossing,
                pre_low_volume=sum(down), pre_low_minutes=len(down),
                recovery_volume=sum(recovery), recovery_minutes=len(recovery),
                post_crossing_volume=sum(after), recovery_mean_volume_ratio=ratio,
                low_minute_crossing_ambiguous=boundary_ambiguous,
                boundary_policy='exclude_low_and_first_crossing_minute')
