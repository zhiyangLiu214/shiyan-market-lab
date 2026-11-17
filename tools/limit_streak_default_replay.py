"""Replay A/B/C candidates with the site's default minute exit rules.

Entries at the opening auction remain a price and liquidity proxy, not a
confirmed order fill. Every held trading day needs validated transaction data.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from terminal_backtest import HistoricalData, dividend_tax_rate, minute_points
from terminal_holding import holding_deadline
from terminal_paper import money
from terminal_strategy_exit import exit_decision
from terminal_strategy_sim import exit_lines
from tools.limit_streak_candidates import RANKINGS, sort_key
from tools.limit_streak_year_replay import (EXPOSURE_CAP, INITIAL, MAX_POSITIONS,
                                            POSITION_PCT, bar_price, fee_total,
                                            load_inputs, market_limit,
                                            matches_cohort, verified_dividend)

CONFIG = dict(take_profit=10, stop_loss=5, exit_patterns=['vwap_cross'],
              holding_limit=True)


def auction_points(source, code, day, row):
    payload = source.ticks(code, day)
    points = minute_points(payload, dict(volume=bar_price(row, 'volume') / 100,
                                          close=bar_price(row, 'close')))
    prices = [tick['price'] for tick in payload['rows'] if tick['vol'] > 0
              and '09:25' <= tick['time'] <= '15:00']
    if (abs(max(prices) - bar_price(row, 'high')) > .021
            or abs(min(prices) - bar_price(row, 'low')) > .021):
        raise ValueError('Transaction high/low does not match daily bar')
    auction = [tick for tick in payload['rows'] if tick['time'] == '09:25'
               and tick['vol'] > 0]
    if not auction or abs(auction[-1]['price'] - bar_price(row, 'open')) > .005:
        raise ValueError('Opening auction print does not match daily open')
    return points, sum(tick['vol'] for tick in auction) * 100


def replay(dates, signals, histories, source, cohort, dividend_dir, ranking='pattern'):
    if cohort not in ('A', 'B', 'C', 'combined') or ranking not in RANKINGS:
        raise ValueError('Unknown cohort or ranking')
    cash, receivable, peak = INITIAL, 0., INITIAL
    positions, trades, equity, skipped, errors = {}, [], [], Counter(), []
    used_ticks = set()
    for index in range(1, len(dates)):
        day, prior_day = dates[index], dates[index - 1]
        held_at_open = set(positions)
        for code, position in positions.items():
            row = histories[code].get(day)
            prior = histories[code].get(prior_day)
            if row is None or prior is None:
                errors.append(dict(day=day, code=code, reason='Held stock daily bar missing'))
                break
            if row.get('tradestatus') == '1' and abs(bar_price(row, 'preclose') - bar_price(prior, 'close')) > .011:
                try:
                    event = verified_dividend(dividend_dir, code, day, position['buy_date'],
                                              bar_price(prior, 'close'), bar_price(row, 'preclose'))
                    position['dividend_gross'] = money(position.get('dividend_gross', 0)
                                                       + position['qty'] * event['per_share'])
                    position['cash_dividend_per_share'] = position.get('cash_dividend_per_share', 0) + event['per_share']
                    if position.get('lines'):
                        for key in ('stop', 'target'):
                            position['lines'][key] = money(position['lines'][key] - event['per_share'])
                except (ValueError, KeyError, TypeError) as exc:
                    errors.append(dict(day=day, code=code, reason=str(exc)))
                    break
        if errors:
            break
        open_values = {code: money(position['qty'] * bar_price(histories[code][day],
                       'open' if histories[code][day].get('tradestatus') == '1' else 'close'))
                       for code, position in positions.items()}
        candidates = [item for item in signals.get(prior_day, [])
                      if matches_cohort(item, cohort)] if index + 1 < len(dates) else []
        candidates.sort(key=lambda item: (0 if item['stage'] == 2 else 1,
                                           sort_key(item, ranking)))
        for item in candidates:
            code = item['code']
            if code in positions or len(positions) >= MAX_POSITIONS:
                skipped['held_or_position_limit'] += 1
                continue
            row = histories[code].get(day)
            if not row or row.get('tradestatus') != '1' or row.get('isST') != '0':
                skipped['entry_not_tradable'] += 1
                continue
            opening = bar_price(row, 'open')
            if not market_limit(row, 'lower') + .005 < opening < market_limit(row, 'upper') - .005:
                skipped['opening_at_limit'] += 1
                continue
            pending = sum(money(p.get('dividend_gross', 0) * .8)
                          for p in positions.values())
            nav = money(cash + receivable + pending + sum(open_values.values()))
            budget = min(nav * POSITION_PCT, nav * EXPOSURE_CAP - sum(open_values.values()), cash)
            qty = math.floor(budget / opening / 100) * 100
            while qty >= 100 and money(qty * opening) + fee_total(money(qty * opening), 'buy') > budget:
                qty -= 100
            if qty < 100:
                skipped['lot_or_cash_limit'] += 1
                continue
            try:
                _, auction_volume = auction_points(source, code, day, row)
                used_ticks.add((code, day))
            except (ValueError, KeyError, TypeError) as exc:
                errors.append(dict(day=day, code=code, reason=str(exc)))
                break
            if qty > auction_volume * .05:
                skipped['opening_liquidity_limit'] += 1
                continue
            gross = money(opening * qty)
            fee = fee_total(gross, 'buy')
            cash = money(cash - gross - fee)
            open_values[code] = gross
            positions[code] = dict(code=code, name=item['name'], qty=qty,
                                   buy_date=day, average_cost=(gross + fee) / qty,
                                   total_cost=money(gross + fee), plan={'_opened_at':day + ' 09:25:00'},
                                   exit_state={}, lines=None, pending_exit=None,
                                   dividend_gross=0., cash_dividend_per_share=0.)
            trades.append(dict(side='buy', date=day, code=code, qty=qty, price=opening,
                               fees=fee, signal_date=prior_day, cohort=cohort,
                               observation_tag=item['observation_tag']))
        if errors:
            break
        for code, position in list(positions.items()):
            row = histories[code][day]
            if row.get('tradestatus') != '1':
                skipped['sell_suspended'] += 1
                continue
            try:
                points, _ = auction_points(source, code, day, row)
                used_ticks.add((code, day))
            except (ValueError, KeyError, TypeError) as exc:
                errors.append(dict(day=day, code=code, reason=str(exc)))
                break
            past = [dict(date=when, close=bar_price(bar, 'close'))
                    for when, bar in histories[code].items() if when < day]
            past.sort(key=lambda bar: bar['date'])
            quote = dict(date=day, prev_close=bar_price(row, 'preclose'),
                         daily={'bars': past}, sessions=dates[:index + 1])
            position['lines'] = exit_lines(position, quote, CONFIG, position.get('lines'))
            pending = position.get('pending_exit')
            for point_index, point in enumerate(points):
                quote.update(price=point['price'], as_of=day + ' ' + point['time'],
                             points=points[:point_index + 1])
                if pending and code in held_at_open:
                    lower = market_limit(row, 'lower')
                    if point['price'] > lower + .005 and position['qty'] <= point['volume'] * 100 * .05:
                        price = max(lower, money(point['price'] * .9995))
                        gross = money(price * position['qty'])
                        fee = fee_total(gross, 'sell')
                        dividend_net = money(position['dividend_gross'] *
                                             (1 - dividend_tax_rate(position['buy_date'], day)))
                        receivable = money(receivable + dividend_net)
                        cash = money(cash + gross - fee)
                        trades.append(dict(side='sell', date=day, time=point['time'],
                                           code=code, qty=position['qty'], price=price,
                                           fees=fee, pnl=money(gross - fee + dividend_net - position['total_cost']),
                                           dividend_receivable=dividend_net,
                                           buy_date=position['buy_date'], reason=pending))
                        del positions[code]
                        break
                decision = exit_decision(position, quote, CONFIG, position['lines'],
                                         position.get('exit_state'))
                position['lines'], position['exit_state'] = decision['lines'], decision['state']
                if decision['sell']:
                    pending = decision['reason']
                elif point['time'] >= '14:54':
                    deadline = holding_deadline(position, quote, True)
                    if deadline['sell']:
                        pending = deadline['reason']
            if code in positions:
                position['pending_exit'] = pending
        if errors:
            break
        close_value = sum(money(p['qty'] * bar_price(histories[code][day], 'close'))
                          for code, p in positions.items())
        pending_dividends = sum(money(p.get('dividend_gross', 0) * .8)
                                for p in positions.values())
        nav = money(cash + receivable + pending_dividends + close_value)
        peak = max(peak, nav)
        equity.append(dict(date=day, nav=nav, cash=cash, positions=len(positions),
                           drawdown_pct=round((peak - nav) / peak * 100, 4)))
    complete = not errors and len(equity) == len(dates) - 1
    sells = [trade for trade in trades if trade['side'] == 'sell']
    status = ('partial_missing_evidence' if not complete else
              'complete_marked_open_positions' if positions else 'complete_price_scenario')
    return dict(cohort=cohort, status=status,
                return_pct=round((equity[-1]['nav'] / INITIAL - 1) * 100, 4) if complete else None,
                final_nav=equity[-1]['nav'] if complete else None,
                max_drawdown_pct=max((point['drawdown_pct'] for point in equity), default=None)
                if complete else None,
                buys=len(trades) - len(sells), sells=len(sells),
                win_rate_pct=round(100 * sum(trade['pnl'] > 0 for trade in sells) / len(sells), 2)
                if sells else None,
                tick_stock_days=len(used_ticks), tick_keys=sorted(f'{code}-{day}' for code, day in used_ticks),
                skipped=dict(skipped), errors=errors,
                unresolved=[dict(code=code, buy_date=p['buy_date'], qty=p['qty'])
                            for code, p in positions.items()], trades=trades, equity=equity)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--daily-dir', action='append', required=True)
    parser.add_argument('--roster', required=True)
    parser.add_argument('--calendar', required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--ranking', choices=RANKINGS, default='pattern')
    parser.add_argument('--cohort', choices=('A', 'B', 'C', 'combined'), required=True)
    parser.add_argument('--dividend-dir', required=True)
    parser.add_argument('--ticks-dir', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    dates, signals, histories, provenance = load_inputs(args.daily_dir, args.roster,
                                                         args.calendar, args.start, args.end)
    source = HistoricalData(Path(args.ticks_dir).parent, args.start, args.end)
    result = replay(dates, signals, histories, source, args.cohort,
                    args.dividend_dir, args.ranking)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(dict(version='limit-streak-default-minute-v1', provenance=provenance,
                       config=CONFIG, buy='auction price and <=5% auction volume proxy',
                       result=result), stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({key: value for key, value in result.items()
                      if key not in ('trades', 'equity', 'tick_keys')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
