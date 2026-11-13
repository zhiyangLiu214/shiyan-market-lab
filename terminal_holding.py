"""Shared five-session holding rule, using exchange sessions and actual fills."""
import threading
import time

import requests

from terminal_paper import money


class TradingSessions:
    def __init__(self):
        self.lock = threading.Lock()
        self.dates = []
        self.checked = 0
        self.retry_after = 0

    def load(self, today):
        with self.lock:
            if time.monotonic() < self.retry_after:
                return []
            if time.monotonic() - self.checked > 600 or not self.dates:
                try:
                    session = requests.Session()
                    session.trust_env = False
                    response = session.get(
                        'https://web.ifzq.gtimg.cn/appstock/app/kline/kline',
                        params={'param': 'sh000001,day,,,500'}, timeout=5)
                    response.raise_for_status()
                    rows = response.json()['data']['sh000001']['day']
                    dates = sorted({row[0] for row in rows})
                    if not dates:
                        raise ValueError('交易日历为空')
                    self.dates = dates
                    self.checked = time.monotonic()
                except (requests.RequestException, ValueError, KeyError):
                    self.retry_after = time.monotonic() + 60
                    return []
            # Callers supply today only after verifying an actual traded quote.
            return sorted({d for d in self.dates if d <= today} | {today})


def holding_deadline(position, quote, enabled=True):
    result = dict(sell=False, days=None, reason='', deferred=False)
    if not enabled:
        return result
    pending = position.get('plan', {}).get('_holding_exit_reason')
    if pending:
        result.update(sell=True, reason=pending)
        return result
    opened = min((row['day'] for row in position.get('lots', []) if row['qty'] > 0),
                 default=position.get('plan', {}).get('_opened_at', '')[:10])
    dates = sorted(set(quote.get('sessions', [])))
    today = quote.get('date', '')
    if not opened or opened not in dates or today not in dates:
        result['reason'] = '最长持仓等待交易日历核验'
        return result
    held = sum(opened <= day <= today for day in dates)
    result['days'] = held
    if held < 5 or held == 5 and quote.get('as_of', '')[11:16] < '14:54':
        return result

    bars = {row['date']: row for row in quote['daily'].get('bars', [])}
    previous_days = [day for day in dates if day < today]
    rate = .2 if position['code'].startswith(('30', '688')) else .1
    sealed_now = bool(quote.get('prev_close') and
                      abs(quote['price'] - money(quote['prev_close'] * (1 + rate))) < .005)
    sealed_yesterday = False
    if len(previous_days) >= 2:
        prior, before = bars.get(previous_days[-1]), bars.get(previous_days[-2])
        sealed_yesterday = bool(prior and before and prior['date'] >= opened and
                                abs(prior['close'] - money(before['close'] * (1 + rate))) < .005)
    if sealed_now and sealed_yesterday:
        result.update(deferred=True, reason=f'持仓第{held}个交易日，连续封板延持，开板触发退出')
    else:
        result.update(sell=True, reason=f'最长持仓退出：第{held}个交易日，未保持连续封板')
    return result
