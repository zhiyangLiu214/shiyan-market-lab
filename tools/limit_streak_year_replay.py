"""Daily-open portfolio scenario for first- and second-board candidates.

The open price is a hypothetical fill. Auction volume, queue priority and
intraday board-order execution are not inferred from daily OHLC.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from terminal_paper import fees, money
from terminal_backtest import dividend_tax_rate
from tools.limit_streak_candidates import (MAIN_CODE, RANKINGS, classify,
                                           is_limit_board, limit_price,
                                           load_history, sort_key)

INITIAL = 100000.0
POSITION_PCT = .10
EXPOSURE_CAP = .70
MAX_POSITIONS = 5


def fee_total(gross, side):
    return money(sum(fees(gross, side).values()))


def bar_price(row, key):
    value = float(row[key])
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'Invalid {key} price')
    return value


def market_limit(row, side):
    rate = .05 if row.get('isST') == '1' else .1
    return limit_price(row['preclose'], rate if side == 'upper' else -rate)


def verified_dividend(directory, code, day, opened, prior_close, preclose):
    if directory is None:
        raise ValueError('Dividend evidence directory not supplied')
    path = Path(directory) / (code + '.json')
    if not path.exists():
        raise ValueError('Dividend evidence missing')
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not payload.get('source') or not payload.get('retrieved_at'):
        raise ValueError('Dividend provenance missing')
    matches = [r for r in payload['rows'] if (r.get('EX_DIVIDEND_DATE') or '')[:10] == day]
    if len(matches) != 1:
        raise ValueError('Dividend ex-date not uniquely verified')
    event = matches[0]
    record = (event.get('EQUITY_RECORD_DATE') or '')[:10]
    if not record or opened > record or event.get('BONUS_IT_RATIO') or event.get('BONUS_RATIO'):
        raise ValueError('Share entitlement or share distribution cannot be verified')
    amount = event.get('PRETAX_BONUS_RMB')
    if amount is None:
        raise ValueError('Dividend amount missing')
    per_share = float(amount) / 10
    if per_share <= 0 or abs(prior_close - preclose - per_share) > .011:
        raise ValueError('Dividend does not reconcile with ex-date reference price')
    return dict(per_share=per_share, record_date=record, source=payload['source'],
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def load_inputs(daily_dirs, roster_path, calendar_path, start, end):
    calendar_path = Path(calendar_path)
    calendar = json.loads(calendar_path.read_text(encoding='utf-8'))
    if calendar != sorted(set(calendar)) or start not in calendar or end not in calendar:
        raise ValueError('Invalid calendar or interval absent')
    first, last = calendar.index(start), calendar.index(end)
    if first < 1 or first >= last:
        raise ValueError('The interval needs a prior signal day and at least two sessions')
    roster_path = Path(roster_path)
    payload = json.loads(roster_path.read_text(encoding='utf-8'))
    roster_rows = payload['roster'] if isinstance(payload, dict) else payload
    roster = {row['code']: row.get('name') or row['code'] for row in roster_rows
              if MAIN_CODE.fullmatch(row['code'])}
    paths = {}
    for folder in daily_dirs:
        for path in sorted(Path(folder).glob('*.json')):
            if MAIN_CODE.fullmatch(path.stem):
                paths.setdefault(path.stem, path)
    missing = sorted(set(roster) - set(paths))
    if missing:
        raise ValueError(f'Daily files missing for {len(missing)} roster stocks')
    selected, histories, source_counts = defaultdict(list), {}, Counter()
    for code, name in sorted(roster.items()):
        data, rows = load_history(paths[code], end)
        source_counts[data['source']] += 1
        kept = False
        for i in range(first - 1, last):
            day = calendar[i]
            today = rows.get(day)
            if not today or not is_limit_board(today):
                continue
            if i < 2:
                continue
            item = classify(code, name, data, rows, day, calendar[i - 1], calendar[i - 2])
            if item:
                selected[day].append(item)
                kept = True
        if kept:
            histories[code] = rows
    provenance = dict(start=start, end=end, universe=len(roster),
                      roster_sha256=hashlib.sha256(roster_path.read_bytes()).hexdigest(),
                      calendar_sha256=hashlib.sha256(calendar_path.read_bytes()).hexdigest(),
                      source_counts=dict(source_counts),
                      selection_counts=dict(Counter(r['stage'] for group in selected.values() for r in group)))
    return calendar[first - 1:last + 1], selected, histories, provenance


def continuation_paths(dates, signals, histories):
    """Outcome labels for later boards; never used to choose a signal-day stock."""
    buckets = defaultdict(lambda: dict(counts=Counter(), touch_returns=defaultdict(list),
                                       sealed_returns=defaultdict(list)))
    for index in range(1, len(dates) - 1):
        signal_day, entry_day, third_day = dates[index - 1:index + 2]
        for item in signals.get(signal_day, []):
            history = histories[item['code']]
            entry, third = history.get(entry_day), history.get(third_day)
            groups = ['all', 'tag=' + item['observation_tag']]
            if item['priority'] >= 2:
                groups.append('priority2')
            for group in groups:
                key = ('一进二' if item['stage'] == 1 else '二进三', group)
                bucket = buckets[key]
                count = bucket['counts']
                count['signal'] += 1
                if not entry or entry.get('tradestatus') != '1' or entry.get('isST') != '0':
                    count['entry_missing_or_untradable'] += 1
                    continue
                opening = bar_price(entry, 'open')
                if not market_limit(entry, 'lower') + .005 < opening < market_limit(entry, 'upper') - .005:
                    count['opening_at_limit'] += 1
                    continue
                if not third or third.get('tradestatus') != '1' or third.get('isST') != '0':
                    count['third_missing_or_untradable'] += 1
                    continue
                count['three_day_observable'] += 1
                touched = bar_price(entry, 'high') >= market_limit(entry, 'upper') - .005
                sealed = is_limit_board(entry)
                if touched:
                    count['entry_day_touch'] += 1
                    if bar_price(third, 'open') > opening:
                        count['touch_then_third_open_above_buy'] += 1
                    if bar_price(third, 'close') > opening:
                        count['touch_then_third_close_above_buy'] += 1
                    if bar_price(third, 'high') >= market_limit(third, 'upper') - .005:
                        count['touch_then_next_board_touch'] += 1
                    if is_limit_board(third):
                        count['touch_then_next_board_sealed'] += 1
                    for field in ('open', 'close', 'high'):
                        price = bar_price(third, field)
                        bucket['touch_returns'][field].append((price / opening - 1) * 100)
                if sealed:
                    count['entry_day_sealed'] += 1
                    if bar_price(third, 'open') > bar_price(entry, 'close'):
                        count['third_open_premium_vs_board_close'] += 1
                    if bar_price(third, 'open') > opening:
                        count['third_open_above_buy'] += 1
                    if bar_price(third, 'close') > opening:
                        count['third_close_above_buy'] += 1
                    if bar_price(third, 'high') >= market_limit(third, 'upper') - .005:
                        count['next_board_touch'] += 1
                    if is_limit_board(third):
                        count['next_board_sealed'] += 1
                    for field in ('open', 'close', 'high'):
                        price = bar_price(third, field)
                        bucket['sealed_returns'][field].append((price / opening - 1) * 100)
    return {f'{strategy}:{group}': dict(
                counts=dict(value['counts']),
                **{f'{path}_third_{field}_return_median_pct':
                   round(median(value[f'{path}_returns'][field]), 3)
                   if value[f'{path}_returns'][field] else None
                   for path in ('touch', 'sealed') for field in ('open', 'close', 'high')})
            for (strategy, group), value in buckets.items()}


def matches_cohort(item, cohort):
    if cohort == 'all':
        return True
    high_first = item['stage'] == 1 and item['priority'] >= 1
    small_shrink = item['stage'] == 2 and item['priority'] == 2
    capacity = (item['stage'] == 2 and not item['first_board_one_word']
                and not item['second_board_one_word']
                and item['second_to_first_volume'] is not None
                and 1 < item['second_to_first_volume'] < 2
                and item['estimated_float_cap_100m'] is not None
                and item['estimated_float_cap_100m'] >= 100)
    if cohort == 'A':
        return high_first
    if cohort == 'B':
        return small_shrink
    if cohort == 'C':
        return capacity
    return cohort == 'combined' and (high_first or small_shrink or capacity)


def simulate(dates, signals, histories, stage, ranking='pattern', slippage_bps=0,
             dividend_dir=None, min_priority=0, exit_mode='open'):
    if (ranking not in RANKINGS or stage not in (1, 2, 'both') or slippage_bps < 0
            or min_priority not in (0, 1, 2) or exit_mode not in ('open', 'target2')):
        raise ValueError('Invalid strategy or execution setting')
    slip = slippage_bps / 10000
    cash, receivable, peak = INITIAL, 0., INITIAL
    positions, trades, equity, skipped = {}, [], [], Counter()
    errors, action_crossings = [], []
    for index in range(1, len(dates)):
        day, previous = dates[index], dates[index - 1]
        # Sell existing holdings before the next set of opening-price buys.
        for code, position in list(positions.items()):
            row = histories[code].get(day)
            if row is None:
                errors.append(dict(day=day, code=code, reason='Held stock daily bar missing'))
                continue
            prior = histories[code].get(previous)
            if row.get('tradestatus') != '1':
                skipped['sell_suspended'] += 1
                continue
            if prior and abs(bar_price(row, 'preclose') - bar_price(prior, 'close')) > .011:
                try:
                    event = verified_dividend(dividend_dir, code, day, position['buy_date'],
                                              bar_price(prior, 'close'), bar_price(row, 'preclose'))
                except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    errors.append(dict(day=day, code=code, reason=str(exc)))
                    continue
                position['dividend_gross'] = money(position.get('dividend_gross', 0) +
                                                   position['qty'] * event['per_share'])
                action_crossings.append(dict(day=day, code=code, status='verified_cash_dividend',
                                             **event))
            if day == position['buy_date']:
                continue
            opening = bar_price(row, 'open')
            lower = market_limit(row, 'lower')
            if exit_mode == 'target2':
                target = money(position['buy_price'] * 1.02)
                high = bar_price(row, 'high')
                closing = bar_price(row, 'close')
                if opening >= target and opening > lower + .005:
                    reference, exit_reason = opening, 'target_open'
                elif high > target + .005:
                    reference, exit_reason = target, 'target_crossed_daily_high'
                elif closing > lower + .005:
                    reference, exit_reason = closing, 'close_fallback'
                else:
                    skipped['sell_limit_down_close'] += 1
                    continue
            elif opening > lower + .005:
                reference, exit_reason = opening, 'next_open'
            else:
                skipped['sell_limit_down_open'] += 1
                continue
            price = money(reference * (1 - slip))
            gross = money(price * position['qty'])
            cost = fee_total(gross, 'sell')
            proceeds = money(gross - cost)
            dividend_net = money(position.get('dividend_gross', 0) *
                                 (1 - dividend_tax_rate(position['buy_date'], day)))
            receivable = money(receivable + dividend_net)
            pnl = money(proceeds + dividend_net - position['total_cost'])
            cash = money(cash + proceeds)
            trades.append(dict(date=day, code=code, name=position['name'], side='sell',
                               qty=position['qty'], price=price, fees=cost, pnl=pnl,
                               dividend_receivable=dividend_net,
                               exit_reason=exit_reason,
                               buy_date=position['buy_date'], buy_price=position['buy_price'],
                               signal_stage=position['stage'],
                               bought_day_touch=position['bought_day_touch'],
                               third_session=day == position['target_sell_date']))
            del positions[code]
        if errors:
            break
        open_values = {}
        for code in positions:
            row = histories[code].get(day)
            if row is None:
                errors.append(dict(day=day, code=code, reason='Held stock cannot be valued at open'))
                break
            key = 'open' if row.get('tradestatus') == '1' else 'close'
            open_values[code] = money(positions[code]['qty'] * bar_price(row, key))
        if errors:
            break
        if index + 1 >= len(dates):
            skipped['end_of_window'] += sum(1 for r in signals.get(previous, [])
                                            if (stage == 'both' or r['stage'] == stage)
                                            and r['priority'] >= min_priority)
        else:
            candidates = [r for r in signals.get(previous, [])
                          if (stage == 'both' or r['stage'] == stage)
                          and r['priority'] >= min_priority]
            candidates.sort(key=lambda r: (0 if stage != 'both' or r['stage'] == 2 else 1,
                                           sort_key(r, ranking)))
            for item in candidates:
                code = item['code']
                if code in positions:
                    skipped['already_held'] += 1
                    continue
                if len(positions) >= MAX_POSITIONS:
                    skipped['position_limit'] += 1
                    continue
                row = histories[code].get(day)
                if not row or row.get('tradestatus') != '1' or row.get('isST') != '0':
                    skipped['not_tradable_or_st'] += 1
                    continue
                opening = bar_price(row, 'open')
                if not market_limit(row, 'lower') + .005 < opening < market_limit(row, 'upper') - .005:
                    skipped['opening_at_price_limit'] += 1
                    continue
                pending = sum(money(p.get('dividend_gross', 0) * .8)
                              for p in positions.values())
                nav = money(cash + receivable + pending + sum(open_values.values()))
                remaining_exposure = max(0, nav * EXPOSURE_CAP - sum(open_values.values()))
                budget = min(nav * POSITION_PCT, remaining_exposure, cash)
                price = money(opening * (1 + slip))
                qty = math.floor(budget / price / 100) * 100
                while qty >= 100 and money(qty * price) + fee_total(money(qty * price), 'buy') > budget:
                    qty -= 100
                if qty < 100:
                    skipped['lot_or_cash_limit'] += 1
                    continue
                gross = money(price * qty)
                cost = fee_total(gross, 'buy')
                cash = money(cash - gross - cost)
                open_values[code] = gross
                target = dates[index + 1] if index + 1 < len(dates) else None
                positions[code] = dict(name=item['name'], qty=qty, buy_date=day,
                                       buy_price=price, total_cost=money(gross + cost),
                                       stage=item['stage'], target_sell_date=target,
                                       bought_day_touch=bar_price(row, 'high') >= market_limit(row, 'upper') - .005)
                trades.append(dict(date=day, code=code, name=item['name'], side='buy',
                                   qty=qty, price=price, fees=cost, signal_date=previous,
                                   signal_stage=item['stage'], observation_tag=item['observation_tag']))
        close_value = 0
        for code, position in positions.items():
            row = histories[code].get(day)
            if not row:
                errors.append(dict(day=day, code=code, reason='Held stock cannot be valued at close'))
                break
            close_value += money(position['qty'] * bar_price(row, 'close'))
        if errors:
            break
        pending = sum(money(p.get('dividend_gross', 0) * .8)
                      for p in positions.values())
        nav = money(cash + receivable + pending + close_value)
        peak = max(peak, nav)
        equity.append(dict(date=day, nav=nav, cash=cash, dividend_receivable=receivable + pending,
                           drawdown_pct=round((peak - nav) / peak * 100, 4),
                           positions=len(positions)))
    sells = [t for t in trades if t['side'] == 'sell']
    unresolved = [dict(code=code, buy_date=p['buy_date'], qty=p['qty'])
                  for code, p in positions.items()]
    period_complete = not errors and len(equity) == len(dates) - 1
    status = ('partial_price_scenario' if not period_complete else
              'complete_marked_open_positions' if unresolved else 'complete_price_scenario')
    return dict(stage=stage, ranking=ranking, slippage_bps=slippage_bps,
                exit_mode=exit_mode,
                min_priority=min_priority, status=status,
                initial_cash=INITIAL, final_nav=equity[-1]['nav'] if period_complete else None,
                return_pct=round((equity[-1]['nav'] / INITIAL - 1) * 100, 4)
                           if period_complete else None,
                max_drawdown_pct=max((r['drawdown_pct'] for r in equity), default=None),
                buys=len(trades) - len(sells), sells=len(sells),
                win_rate_pct=round(100 * sum(t['pnl'] > 0 for t in sells) / len(sells), 2) if sells else None,
                completed_chain_count=sum(t['bought_day_touch'] and t['third_session'] and t['pnl'] > 0
                                          for t in sells),
                dividend_receivable=receivable,
                total_fees=money(sum(t['fees'] for t in trades)),
                skipped=dict(skipped), action_crossings=action_crossings,
                unresolved=unresolved, errors=errors, trades=trades, equity=equity)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--daily-dir', action='append', required=True)
    parser.add_argument('--roster', required=True)
    parser.add_argument('--calendar', required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--ranking', choices=RANKINGS, default='pattern')
    parser.add_argument('--cohort', choices=('all', 'A', 'B', 'C', 'combined'), default='all')
    parser.add_argument('--slippage-bps', type=float, default=0)
    parser.add_argument('--min-priority', type=int, choices=(0, 1, 2), default=0)
    parser.add_argument('--exit-mode', choices=('open', 'target2'), default='open')
    parser.add_argument('--dividend-dir', help='Historical dividend implementation records')
    parser.add_argument('--output', help='Private JSON result; must not already exist')
    args = parser.parse_args()
    dates, signals, histories, provenance = load_inputs(args.daily_dir, args.roster,
                                                         args.calendar, args.start, args.end)
    signals = {day: [item for item in group if matches_cohort(item, args.cohort)]
               for day, group in signals.items()}
    provenance['cohort'] = args.cohort
    provenance['cohort_counts'] = dict(Counter(item['stage'] for group in signals.values()
                                               for item in group))
    scenarios = [simulate(dates, signals, histories, stage, args.ranking, args.slippage_bps,
                          args.dividend_dir, args.min_priority, args.exit_mode)
                 for stage in (1, 2, 'both')]
    report = dict(version='limit-streak-year-open-v3', provenance=provenance,
                  assumptions=dict(buy='next-session daily open price proxy',
                                   sell=('following-session open; wait if at lower limit'
                                         if args.exit_mode == 'open' else
                                         'following-session +2% limit sell if daily high crosses; '
                                         'otherwise daily close proxy'),
                                   max_positions=MAX_POSITIONS, position_pct=POSITION_PCT,
                                   exposure_cap=EXPOSURE_CAP, lot=100,
                                   auction_fill_confirmed=False, board_fill_confirmed=False),
                  continuation_paths=continuation_paths(dates, signals, histories),
                  scenarios=scenarios)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        print(json.dumps(dict(output=str(path), provenance=provenance,
                              continuation_paths=report['continuation_paths'],
                              scenarios=[{k: v for k, v in s.items() if k not in ('trades', 'equity')}
                                         for s in scenarios]), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
