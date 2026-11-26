"""The deployed health command must fit beside the worker in its memory cgroup."""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class WorkerHealthTests(unittest.TestCase):
    def test_deployed_command_checks_last_run_without_loading_worker_dependencies(self):
        entry = Path(__file__).with_name('sync.py').resolve()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'control').mkdir()
            (root / 'control' / 'status.json').touch()
            (root / 'maintenance-schedule.json').write_text('{"interval_seconds": 1800}')
            now = dt.datetime.now(dt.timezone.utc)
            for age, fatal, expected in ((300, False, 0), (3000, False, 1), (1, True, 1)):
                with self.subTest(age=age, fatal=fatal):
                    status = {'at': (now - dt.timedelta(seconds=age)).isoformat(), 'fatal': fatal}
                    (root / 'last-run.json').write_text(json.dumps(status))
                    result = subprocess.run(
                        [sys.executable, '-E', '-S', str(entry), '--state', str(root), '--health'],
                        env={**os.environ, 'BOOK_PROGRESS_ADMIN_CONTROL': '1'}, capture_output=True)
                    self.assertEqual(result.stderr, b'')
                    self.assertEqual(result.returncode, expected)
