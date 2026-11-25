import json
import os
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from admin_runtime import Administration
from maintenance_settings import FIELDS, MaintenanceSchedule, ScheduleError, admin_health_age, podcast_window
from test_admin_runtime import CandidateBridge, Fixture


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'interval_seconds': 88, 'auto_discovery': {'enabled': True, 'abs_library_id': 'existing'}}
        self.schedule = MaintenanceSchedule(self.root, self.config)

    def test_public_defaults_preserve_existing_cadence_and_override_only_changed_keys(self):
        self.assertEqual(self.schedule.public()['values']['interval_seconds'], 88)
        result = self.schedule.update({'expected_revision': 0, 'changes': {'series_enabled': True,
                                     'series_interval_seconds': 7200, 'book_discovery_seconds': 1800}})
        self.assertEqual(result['revision'], 1)
        snapshot = self.root / 'series-control/settings.json'
        self.assertEqual(json.loads(snapshot.read_text()), {'enabled': True, 'interval_seconds': 7200})
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        before = snapshot.read_bytes()
        restarted = MaintenanceSchedule(self.root, dict(self.config, interval_seconds=90))
        effective = restarted.effective(restarted.base)
        self.assertEqual(effective['interval_seconds'], 90)
        self.assertEqual(effective['auto_discovery'], dict(self.config['auto_discovery'], interval_seconds=1800))
        self.assertEqual(effective['series_organizer'], {'enabled': True, 'interval_seconds': 7200})
        with self.assertRaisesRegex(ScheduleError, 'inzwischen'):
            restarted.update({'expected_revision': 0, 'changes': {'series_enabled': False}})
        self.assertEqual(snapshot.read_bytes(), before)

    def test_invalid_intervals_do_not_change_loaded_settings_or_publish_controls(self):
        snapshot = self.root / 'series-control/settings.json'
        before = (self.schedule.path.read_bytes(), snapshot.read_bytes())
        for changes in ({'series_enabled': 1}, {'interval_seconds': 0}, {'podcast_end_hour': 22},
                        {'podcast_timezone': 'unknown/invalid'}, {'podcast_start_hour': True},
                        {'series_interval_seconds': 1}, {'integrity_window_utc': {'start_hour': 8}},
                        {'ebook_retry_max_seconds': 30}, {'folder_validation_interval_seconds': 10},
                        {'interval_seconds': None}, {'path': '/tmp'}):
            with self.subTest(changes=changes), self.assertRaises(ScheduleError):
                self.schedule.update({'expected_revision': 0, 'changes': changes})
            self.assertEqual(self.schedule.revision, 0)
            self.assertEqual(before, (self.schedule.path.read_bytes(), snapshot.read_bytes()))

    def test_window_uses_local_time_in_winter_and_summer_and_a_single_id_after_midnight(self):
        def check(moment, config=None):
            return podcast_window(config or {}, datetime.fromisoformat(moment).timestamp())
        self.assertEqual(check('2026-11-25T20:59:00+00:00')[0], False)
        self.assertEqual(check('2026-11-25T21:00:00+00:00'), (True, '2026-11-25'))
        self.assertEqual(check('2026-11-25T23:00:00+00:00')[0], False)
        self.assertEqual(check('2026-06-01T20:00:00+00:00'), (True, '2026-06-01'))
        cfg = {'podcast_maintenance': {'start_hour': 22, 'end_hour': 4, 'timezone': 'Europe/Berlin'}}
        for stamp in ('2026-10-24T23:00:00+00:00', '2026-10-25T00:30:00+00:00', '2026-10-25T01:30:00+00:00'):
            self.assertEqual(check(stamp, cfg), (True, '2026-10-24'))

    def test_health_allows_configured_poll_interval_and_still_detects_stale_worker(self):
        self.schedule.update({'expected_revision': 0, 'changes': {'interval_seconds': 1800}})
        heartbeat = self.root / 'control/status.json'
        heartbeat.parent.mkdir()
        heartbeat.touch()
        os.utime(heartbeat, (900, 900))
        self.assertGreater(admin_health_age(self.root, 1000), 1800)
        self.assertEqual(admin_health_age(self.root, 1300), 0)

    def test_gui_exposes_every_supported_schedule_field(self):
        class Inputs(HTMLParser):
            def handle_starttag(self, tag, attrs):
                attr = dict(attrs)
                if tag == 'input' and 'data-schedule' in attr:
                    fields.add(attr['data-schedule'])
                if tag == 'input' and attr.get('id') in ('integrity-start', 'integrity-end'):
                    window_fields.add(attr['id'])
        fields, window_fields = set(), set()
        Inputs().feed((Path(__file__).parent / 'admin_static/index.html').read_text())
        self.assertEqual(window_fields, {'integrity-start', 'integrity-end'})
        self.assertEqual(fields | {'integrity_window_utc'}, set(FIELDS))


class ScheduleCommandTests(Fixture):
    def test_worker_applies_revisable_change_even_when_media_unavailable_and_keeps_progress(self):
        before = self.saved_state()
        self.config['interval_seconds'] = 88
        admin = Administration(self.root, self.key, self.config, self.credentials, CandidateBridge)
        ident = admin.store.submit('schedule.update', {'expected_revision': 0,
            'changes': {'podcast_start_hour': 21, 'series_enabled': True}})['id']
        self.assertTrue(admin.process_one(None))
        self.assertEqual(admin.store.command(ident)['status'], 'success')
        cfg, _, _ = admin.effective()
        self.assertEqual(cfg['podcast_maintenance']['start_hour'], 21)
        self.assertEqual(cfg['interval_seconds'], 88)
        admin.publish(None)
        self.assertTrue(admin.store.status()['schedule']['values']['series_enabled'])
        self.assertEqual(self.saved_state(), before)
