import unittest

from tools.research_rebound_short_exit import (
    fixed_exit_lines, fixed_exit_decision, three_session_deadline, replay_short_profit,
    replay_protected_rebound,
)


class ShortExitTests(unittest.TestCase):
    def position(self):
        return dict(opened='2026-11-12', average_cost=10,
                    plan={'_opened_at': '2026-11-12 15:00:00'})

    def test_three_sessions_count_entry_day_and_weekend(self):
        p = self.position()
        dates = ['2026-11-12', '2026-11-13', '2026-11-16']
        q = dict(sessions=dates, date='2026-11-16', as_of='2026-11-16 14:53')
        self.assertFalse(three_session_deadline(p, q)['sell'])
        q['as_of'] = '2026-11-16 14:54'
        self.assertTrue(three_session_deadline(p, q)['sell'])
        self.assertEqual(three_session_deadline(p, q)['days'], 3)

    def test_fixed_profit_triggers_without_waiting_for_pullback(self):
        p = self.position()
        cfg = dict(stop_loss=5, take_profit=1)
        lines = fixed_exit_lines(p, {}, cfg)
        q = dict(date='2026-11-13', price=10.1)
        result = fixed_exit_decision(p, q, cfg, lines)
        self.assertTrue(result['sell'])
        self.assertEqual(result['reason'], '超短固定止盈')
        q['price'] = 10.05
        self.assertTrue(fixed_exit_decision(p, q, cfg, lines, result['state'])['sell'])
        q['date'] = p['opened']
        self.assertFalse(fixed_exit_decision(p, q, cfg, lines)['sell'])

    def test_no_limit_up_extension_or_higher_profit_target(self):
        p = self.position()
        cfg = dict(stop_loss=5, take_profit=2)
        previous = dict(stop=11, target=12)
        lines = fixed_exit_lines(p, {}, cfg, previous)
        self.assertEqual(lines['target'], 10.2)
        self.assertEqual(lines['stop'], 9.5)

    def test_replay_sells_on_third_session_and_restores_shared_engine(self):
        import terminal_backtest as engine
        from terminal_strategy_sim import checked_config
        dates = ['2026-11-11', '2026-11-12', '2026-11-13', '2026-11-16']

        class Data:
            def daily(self, code):
                return dict(raw=[[d, 10, 10, 10, 10, 300000] for d in dates])

            def ticks(self, code, day):
                return dict(rows=[dict(time=t, price=10, vol=100000)
                                  for t in ['09:30', '14:54', '14:59']])

        spec = dict(id='test', config=checked_config(dict(
            name='test', formula='XG:C>0;', pool='market', boards=['main'])))
        matches = [dict(strategy='test', code='600000', name='样本',
                        date=dates[1], rank=1)]
        original = engine.holding_deadline
        result = replay_short_profit(spec, matches, Data(), dates[1:])
        self.assertTrue(result['complete'])
        self.assertIs(engine.holding_deadline, original)
        self.assertEqual([t['date'] for t in result['trades']], [dates[1], dates[3]])
        self.assertEqual(result['trades'][-1]['reason'], '最长持仓3个交易日到期退出')
        self.assertEqual(result['trades'][-1]['time'], '14:55')

    def test_protected_replay_retains_limit_and_momentum_functions(self):
        from unittest.mock import patch
        import terminal_backtest as engine
        original_lines, original_exit = engine.exit_lines, engine.exit_decision

        def observe(*args, **kwargs):
            self.assertIs(engine.exit_lines, original_lines)
            self.assertIs(engine.exit_decision, original_exit)
            self.assertIs(engine.holding_deadline, three_session_deadline)
            return 'checked'

        with patch.object(engine, 'replay_strategy', observe):
            self.assertEqual(replay_protected_rebound(), 'checked')

    def test_shared_protection_holds_strong_prices_and_exits_on_weakness(self):
        from terminal_strategy_exit import exit_decision
        p = dict(self.position(), code='600000')
        cfg = dict(take_profit=1, exit_patterns=['vwap_cross'])
        lines = dict(target=10.1, stop=9.5)
        points = [dict(time='10:00', price=10.1, volume=100, average=10),
                  dict(time='10:01', price=10.22, volume=200, average=10)]
        q = dict(date='2026-11-13', prev_close=10, price=10.22, points=points)
        result = exit_decision(p, q, cfg, lines)
        self.assertFalse(result['sell'])
        points.append(dict(time='10:02', price=10.21, volume=200, average=10))
        q['price'] = 10.21
        self.assertTrue(exit_decision(p, q, cfg, lines, result['state'])['sell'])
        q.update(price=11, points=[dict(time='10:00', price=11, volume=100, average=10)])
        sealed = exit_decision(p, q, cfg, lines)
        self.assertFalse(sealed['sell'])
        q.update(price=10.99, points=q['points']+[dict(time='10:01', price=10.99, volume=100, average=10)])
        broken = exit_decision(p, q, cfg, sealed['lines'], sealed['state'])
        self.assertTrue(broken['sell'])
        self.assertIn('炸板', broken['reason'])
