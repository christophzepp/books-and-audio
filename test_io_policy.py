import datetime as dt
import unittest
from types import SimpleNamespace

from io_policy import integrity_due, settings
from io_metrics import increment, snapshot


class IOPolicyTests(unittest.TestCase):
    def test_defaults_do_not_add_background_full_reads(self):
        self.assertFalse(integrity_due({}, 1000, None))
        self.assertEqual(settings({})['folder_validation_interval_seconds'], 0)
        self.assertEqual(settings({})['ebook_discovery_interval_seconds'], 300)

    def test_invalid_configuration_does_not_silently_relax_checks(self):
        for value in ([], None, {'unknown': 1}, {'ebook_cache_max_bytes': True},
                      {'folder_validation_interval_seconds': 1},
                      {'ebook_verify_interval_seconds': float('nan')},
                      {'ebook_candidate_stable_seconds': 1},
                      {'integrity_window_utc': {'start_hour': 3, 'end_hour': 3}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings({'io_policy': value})

    def test_due_and_clock_rollback(self):
        cfg = {'io_policy': {'ebook_verify_interval_seconds': 600}}
        self.assertFalse(integrity_due(cfg, 1000, 500))
        for last in (400, None, float('nan'), 1001, True):
            self.assertTrue(integrity_due(cfg, 1000, last))

    def test_background_window_uses_utc_and_supports_midnight(self):
        for start, end, yes, no in ((3, 5, 4, 6), (23, 2, 0, 12)):
            cfg = {'io_policy': {'ebook_verify_interval_seconds': 60,
                                'integrity_window_utc': {'start_hour': start, 'end_hour': end}}}
            at = lambda hour: dt.datetime(2026, 11, 8, hour, tzinfo=dt.timezone.utc).timestamp()
            self.assertTrue(integrity_due(cfg, at(yes), None))
            self.assertFalse(integrity_due(cfg, at(no), None))

    def test_metrics_are_in_memory_and_snapshots_are_detached(self):
        owner = SimpleNamespace()
        increment(owner, 'ebook_idle_skips')
        increment(owner, 'ebook_idle_skips', 2)
        copy = snapshot(owner)
        copy['ebook_idle_skips'] = 90
        self.assertEqual(snapshot(owner), {'ebook_idle_skips': 3})


if __name__ == '__main__':
    unittest.main()
