"""Research-only immediate profit exits and a three-session holding deadline.

The wrapper patches globals only in its isolated worker process. Never use it
in the website process or concurrently in threads. Execution, fees, T+1 and
liquidity checks remain in the historical replay engine.
"""
from unittest.mock import patch

from terminal_paper import money


def fixed_exit_lines(position, quote, config, previous=None):
    cost = position['average_cost']
    adjustment = position.get('cash_dividend_per_share', 0)
    return dict(opened=position['plan']['_opened_at'], anchor=None,
                stop=money(cost*(1-config['stop_loss']/100)-adjustment),
                target=money(cost*(1+config['take_profit']/100)-adjustment))


def fixed_exit_decision(position, quote, config, lines, state=None):
    state = dict(state or {})
    reason = state.get('exit_reason', '')
    if quote['date'] > position['opened']:
        if not reason and quote['price'] <= lines['stop']:
            reason = '超短固定止损'
        if not reason and quote['price'] >= lines['target']:
            reason = '超短固定止盈'
    if reason:
        state['exit_reason'] = reason
    return dict(sell=bool(reason), reason=reason, state=state,
                lines=lines, notices=[], metrics={})


def three_session_deadline(position, quote, enabled=True):
    sessions = sorted(set(quote.get('sessions', [])))
    opened, today = position['opened'], quote['date']
    if opened not in sessions or today not in sessions:
        raise ValueError('三日到期规则缺少交易日历')
    days = sum(opened <= day <= today for day in sessions)
    sell = bool(enabled and (days > 3 or days == 3 and quote['as_of'][11:16] >= '14:54'))
    return dict(sell=sell, days=days, deferred=False,
                reason='最长持仓3个交易日到期退出' if sell else '')


def replay_short_profit(*args, **kwargs):
    import terminal_backtest as engine
    with patch.object(engine, 'exit_lines', fixed_exit_lines), \
            patch.object(engine, 'exit_decision', fixed_exit_decision), \
            patch.object(engine, 'holding_deadline', three_session_deadline):
        return engine.replay_strategy(*args, **kwargs)


def replay_protected_rebound(*args, **kwargs):
    """Restore shared limit-up/momentum protection, retaining the three-day deadline."""
    import terminal_backtest as engine
    with patch.object(engine, 'holding_deadline', three_session_deadline):
        return engine.replay_strategy(*args, **kwargs)
