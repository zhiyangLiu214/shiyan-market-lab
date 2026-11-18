import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

from research_preboard_forward import board_context, find_signals, independent_board


def rows(closes, boards=(), lows=None, volumes=None):
    result = []
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index else close
        result.append(dict(date=f'2026-01-{index + 1:02}', session=index,
                           open=previous, high=max(previous, close),
                           low=(lows or {}).get(index, min(previous, close)),
                           close=close, preclose=previous,
                           volume=(volumes or {}).get(index, 100),
                           st=False, board=index in boards))
    return result


class PreboardForwardTests(unittest.TestCase):
    def test_signals_exist_before_outcome_and_floor_is_optional(self):
        history = rows([10, 11, 10.8, 10.7, 10.8, 10.9, 11.02, 10.9],
                       boards=(1,), lows={3: 9.8}, volumes={6: 200, 7: 100})
        first = find_signals(history[:7], '2026-01-01', '2026-01-07')
        second = find_signals(history[:8], '2026-01-01', '2026-01-08')
        self.assertEqual([(stage, history[index]['date']) for index, stage, _ in first],
                         [('reclaim', '2026-01-07')])
        self.assertEqual([(stage, history[index]['date']) for index, stage, _ in second],
                         [('reclaim', '2026-01-07'), ('pullback', '2026-01-08')])
        self.assertFalse(first[0][2]['held_prior_open'])

    def test_prior_streak_or_large_rally_is_not_independent_board(self):
        streak = rows([10, 11, 12.1, 11.9, 11.8, 11.7, 11.6, 12.76],
                      boards=(1, 2, 7))
        self.assertFalse(independent_board(streak, 2))
        self.assertTrue(board_context(streak, 7)['recent_consecutive_limits'])
        self.assertFalse(independent_board(streak, 7))
        rally = rows([10, 10.5, 11, 11.7, 12.1, 12.2, 12, 13.2], boards=(7,))
        self.assertTrue(board_context(rally, 7)['recent_large_rally'])
        self.assertFalse(independent_board(rally, 7))


if __name__ == '__main__':
    unittest.main()
