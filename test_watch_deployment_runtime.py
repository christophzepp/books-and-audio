"""Watcher scheduling/publication regressions; no Docker or server access."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import watch_deployment as watcher
from test_watch_deployment import config, containers, row
from test_podcast_deployment import config as podcast_config, containers as podcast_containers


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'config').mkdir()
        (self.root / 'state').mkdir()
        self.dest = self.root / 'state/deployment.json'

    def configure(self, value):
        (self.root / 'config/config.json').write_text(json.dumps(value))

    def gate(self):
        return json.loads(self.dest.read_text())

    def runner(self):
        return Mock(side_effect=[Mock(stdout=json.dumps(containers())),
                                 Mock(stdout=json.dumps({'count': 1, 'rows': [row()]}))])

    def all_checks(self, stack, factory=None):
        self.configure({'bookbridge_deployment': {}, 'podcast_deployment': {'pinepods': {}},
                        'folder_deployment': {}})
        checks = [('check_deployment', None), ('check_bookbridge_deployment', None),
                  ('check_podcast_deployment', {'abs': {'verified': True}}),
                  ('check_pinepods_deployment', True),
                  ('check_folder_deployment', ({'worker': [{'verified': True}]}, {}))]
        for name, result in checks:
            stack.enter_context(patch.object(watcher, name,
                side_effect=factory(name, result) if factory else lambda *args, result=result: result))

    def test_independent_checks_run_together_with_bounded_parallelism(self):
        # Five independent groups with cumulative timeout budgets above the
        # receipt TTL must start together. Sequential scheduling breaks the
        # barrier and cannot produce a successful receipt.
        barrier = threading.Barrier(5)
        calls, mutex = [], threading.Lock()

        def factory(name, result):
            def check(*args):
                with mutex:
                    calls.append(name)
                barrier.wait(timeout=2)
                return result
            return check

        with ExitStack() as stack:
            self.all_checks(stack, factory)
            with patch.object(watcher, 'ThreadPoolExecutor', wraps=watcher.ThreadPoolExecutor) as executor:
                self.assertEqual(watcher.main(self.root, Mock(), clock=lambda: 1000), 0)
                self.assertEqual(executor.call_args.kwargs['max_workers'], 5)
        self.assertEqual(len(set(calls)), 5)
        self.assertTrue(all(self.gate()[flag] for flag in
                            ('ok', 'bookbridge_ok', 'podcasts_ok', 'pinepods_ok', 'folder_gate_ok')))

    def test_slow_success_keeps_earliest_timestamp_and_existing_ttl(self):
        self.configure(config())
        self.assertEqual(watcher.main(self.root, self.runner(), clock=Mock(side_effect=[1000, 1149])), 0)
        self.assertEqual(self.gate()['checked_at'], 1000)
        self.assertTrue(self.gate()['ok'])
        self.assertEqual(watcher.RECEIPT_MAX_AGE, 150)

    def test_expired_success_is_not_published_as_fresh(self):
        with ExitStack() as stack:
            self.all_checks(stack)
            self.assertEqual(watcher.main(self.root, Mock(), clock=Mock(side_effect=[1000, 1151])), 1)
        gate = self.gate()
        self.assertEqual(gate['checked_at'], 1000)
        for flag in ('ok', 'bookbridge_ok', 'podcasts_ok', 'pinepods_ok', 'folder_gate_ok'):
            self.assertFalse(gate[flag])
        for field in ('podcast_mounts', 'folder_mounts', 'folder_errors'):
            self.assertNotIn(field, gate)
        self.assertEqual(gate['reason'], 'Deployment verification expired before publication')

    def test_clock_rollback_or_invalid_finish_fails_closed(self):
        for end in (999, True, float('nan'), float('inf')):
            with self.subTest(end=end):
                self.configure(config())
                self.assertEqual(watcher.main(self.root, self.runner(), clock=Mock(side_effect=[1000, end])), 1)
                self.assertFalse(self.gate()['ok'])

    def test_one_failed_real_check_does_not_borrow_other_groups_success(self):
        cfg = config()
        cfg.update(podcast_config())
        cfg['podcast_deployment']['worker']['image'] = 'sha256:' + 'f' * 64
        self.configure(cfg)

        def inspect(args, **kwargs):
            self.assertEqual(kwargs['timeout'], 20)
            self.assertTrue(kwargs['check'])
            if args == ['docker', 'inspect', 'audiobookshelf', 'bookorbit', 'bookbridge']:
                return Mock(stdout=json.dumps(containers()))
            if args == watcher.snapshot_command(True):
                return Mock(stdout=json.dumps({'count': 1, 'rows': [row()]}))
            if args == ['docker', 'inspect', 'audiobookshelf', 'book-progress-sync', 'jellyfin']:
                return Mock(stdout=json.dumps(podcast_containers()))
            self.fail('Unexpected process command')

        self.assertEqual(watcher.main(self.root, inspect, clock=lambda: 1000), 0)
        self.assertTrue(self.gate()['ok'])
        self.assertFalse(self.gate()['podcasts_ok'])
        self.assertNotIn('podcast_mounts', self.gate())
        self.assertIn('image/state changed', self.gate()['podcasts_reason'])

    def test_overlapping_run_neither_inspects_nor_overwrites_receipt(self):
        self.configure(config())
        self.dest.write_text('{"previous": true}')
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []

        def check(*args):
            entered.set()
            if not release.wait(timeout=3):
                raise AssertionError('Test did not release watcher')

        def run():
            try:
                results.append(watcher.main(self.root, Mock(), clock=lambda: 1000))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(watcher, 'check_deployment', side_effect=check):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=2))
                runner, clock = Mock(), Mock(return_value=2000)
                self.assertEqual(watcher.main(self.root, runner, clock), 1)
                runner.assert_not_called()
                clock.assert_not_called()
                self.assertEqual(self.gate(), {'previous': True})
            finally:
                release.set()
                thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [0])
        self.assertEqual(self.gate()['checked_at'], 1000)
        self.assertEqual((self.root / 'state/.deployment.lock').stat().st_mode & 0o777, 0o600)

    def test_unique_private_temp_file_preserves_atomic_replacement(self):
        self.configure(config())
        self.dest.write_text('{"previous": true}')
        legacy = self.dest.with_suffix('.tmp')
        legacy.write_text('unrelated temporary file')
        replace = watcher.os.replace
        seen = []

        def inspect_replace(source, destination):
            seen.append(source)
            self.assertEqual(self.gate(), {'previous': True})
            self.assertEqual(destination, self.dest)
            self.assertNotEqual(source, legacy)
            self.assertEqual(source.stat().st_mode & 0o777, 0o600)
            self.assertTrue(json.loads(source.read_text())['ok'])
            replace(source, destination)

        with patch.object(watcher.os, 'replace', side_effect=inspect_replace):
            self.assertEqual(watcher.main(self.root, self.runner(), clock=lambda: 1000), 0)
        self.assertEqual(len(seen), 1)
        self.assertTrue(self.gate()['ok'])
        self.assertEqual(self.dest.stat().st_mode & 0o777, 0o600)
        self.assertEqual(legacy.read_text(), 'unrelated temporary file')
        self.assertEqual(list((self.root / 'state').glob('.deployment-*.tmp')), [])

    def test_failed_publication_keeps_old_receipt_cleans_temp_and_releases_lock(self):
        self.configure(config())
        for function in ('dump', 'replace'):
            with self.subTest(function=function):
                self.dest.write_text('{"previous": true}')
                owner = watcher.json if function == 'dump' else watcher.os
                with patch.object(owner, function, side_effect=OSError('write failed')):
                    with self.assertRaises(OSError):
                        watcher.main(self.root, self.runner(), clock=lambda: 1000)
                self.assertEqual(self.gate(), {'previous': True})
                self.assertEqual(list((self.root / 'state').glob('.deployment-*.tmp')), [])
                self.assertEqual(watcher.main(self.root, self.runner(), clock=lambda: 1000), 0)


if __name__ == '__main__':
    unittest.main()
