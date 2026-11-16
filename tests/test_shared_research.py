import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.shared_research import candidates, checked_path, file_record


class SharedResearchFilesTest(unittest.TestCase):
    def test_only_replay_inputs_are_selected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / 'daily').mkdir()
            (root / 'daily' / '000001.json').write_text(json.dumps({
                'source': 'test source', 'retrieved_at': '2026-11-15T17:00:00+08:00',
                'raw': [['2026-11-15', 10]]}))
            (root / 'secrets.json').write_text('{}')
            rows = list(candidates(root))
            self.assertEqual([p.name for p in rows], ['000001.json'])
            relative, sha, source, _, retrieved_at, raw = file_record(root, rows[0])
            self.assertEqual(relative, 'daily/000001.json')
            self.assertEqual(sha, hashlib.sha256(raw).hexdigest())
            self.assertEqual(source, 'test source')
            self.assertEqual(retrieved_at, '2026-11-15T17:00:00+08:00')

    def test_reject_path_escape_and_unrelated_data(self):
        for value in ('../credentials.json', '/etc/passwd', 'daily/../secrets.json',
                      'daily/nested/a.json', 'strategy-sim/strategies.sqlite3'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                checked_path(value)

    def test_manifest_order_matches_database_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / 'ticks').mkdir()
            (root / 'actions').mkdir()
            for relative in ('ticks/000002.json', 'roster.json', 'actions/000001.json'):
                (root / relative).write_text('{}')
            self.assertEqual([p.relative_to(root).as_posix() for p in candidates(root)],
                             ['actions/000001.json', 'roster.json', 'ticks/000002.json'])

    def test_reject_symlinked_data_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / 'daily').symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                list(candidates(root))


if __name__ == '__main__':
    unittest.main()
