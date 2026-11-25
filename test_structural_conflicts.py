import datetime
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from admin_runtime import Administration, public_podcasts
from maintenance_settings import MaintenanceSchedule
from podcast_sync import PodcastCoordinator
from structural_conflicts import PREFIX, valid_retry
import series_listenarr as native
import test_admin_runtime as admin_tests
import test_series_listenarr as series_tests
import test_podcast_sync as podcast_tests


class PodcastCacheTests(unittest.TestCase):
    def setUp(self):
        self.model = podcast_tests.Tests()
        self.model.setUp()
        self.addCleanup(self.model.doCleanups)
        self.model.add_pine()
        self.model.cfg['podcasts']['pinepods_optional_enrollment'] = True
        self.episode = self.model.abs.data['media']['episodes'][0]
        self.episode['enclosure'] = {}
        self.clock('2026-11-25T14:00:00+01:00')
        self.key = '_podcast:show:episode'

    def clock(self, value):
        self.model.clock = datetime.datetime.fromisoformat(value).timestamp()
        self.model.gate['checked_at'] = self.model.clock
        self.model.write_gate()

    def run_cycle(self, expected):
        coordinator = PodcastCoordinator(self.model.b)
        with patch.object(coordinator, 'handle', wraps=coordinator.handle) as handle:
            result = coordinator.cycle(apply=True, settle=0)
        self.assertEqual(handle.call_count, expected, result)
        return result

    def test_restart_reuses_receipt_and_edited_shared_progress_is_still_transferred(self):
        result = self.run_cycle(1)
        self.assertTrue(public_podcasts(result, self.model.cfg['podcasts'], False)['episodes'][0]['can_retry_conflict'])
        self.run_cycle(0)
        self.model.abs.set(30)
        self.assertEqual(self.run_cycle(1)['synced'], 1)
        self.assertTrue(self.model.jf.writes)
        self.run_cycle(1)  # First stable local run records the conflict for the new playback.
        self.run_cycle(0)
        self.episode['enclosure'] = {'url': 'https://example.org/episode.mp3'}
        self.run_cycle(1)
        self.assertFalse(self.model.load(PREFIX + self.key))

    def test_same_native_catalog_skips_and_edited_enclosure_rechecks_before_night(self):
        import test_podcast_bulk_io as bulk
        client, rows, _, history = bulk.BulkAPI.pine(self, 1)
        client.probe = self.model.b.pinepods.probe
        self.model.b.pinepods = client
        self.model.abs.data['media']['metadata']['feedUrl'] = bulk.FEED
        self.episode['enclosure'] = {'url':bulk.URL + 'new'}
        rows[0]['Episodeurl'] = ''
        result = self.run_cycle(1)
        self.assertEqual(result['episodes'][0]['pine_reason_code'],'episode_identity_unresolved')
        self.assertTrue(result['episodes'][0]['can_retry_conflict'])
        self.run_cycle(0)
        rows[0]['Episodetitle'] = 'New candidate'
        self.run_cycle(1)
        self.run_cycle(0)
        rows[0].update(Episodeurl=bulk.URL + 'new',Episodeduration=1000,Listenduration=0)
        history.clear()
        self.run_cycle(1)
        self.assertFalse(self.model.load(PREFIX + self.key))

    def test_pending_operation_never_reuses_receipt_or_suppresses_recovery(self):
        self.run_cycle(1)
        self.model.db.execute("INSERT INTO operations VALUES(?,?,'{}','pending')", ('operation', self.key))
        self.model.db.commit()
        self.run_cycle(1)
        self.assertFalse(self.model.load(PREFIX + self.key))

    def test_once_each_night_works_across_restarts_and_only_once_per_window(self):
        self.run_cycle(1)
        self.run_cycle(0)
        for day in (25, 26):
            self.clock(f'2026-11-{day}T22:15:00+01:00')
            self.run_cycle(1)
            self.run_cycle(0)

    def test_explicit_retry_deletes_only_selected_receipt_in_shared_database(self):
        self.run_cycle(1)
        fixture = admin_tests.Fixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        manager = Administration(fixture.root, fixture.key, fixture.config, fixture.credentials, admin_tests.CandidateBridge)
        book_db = sqlite3.connect(':memory:')
        self.addCleanup(book_db.close)
        book_db.execute('CREATE TABLE state(id TEXT PRIMARY KEY,value TEXT)')
        book_db.execute('INSERT INTO state VALUES (?,?)', (PREFIX+self.key,'unrelated'))
        isolated = SimpleNamespace(db=book_db, _shared_podcast_db=self.model.b.db)
        command = manager.store.submit('conflict.retry', {'type':'podcast','id':self.key})['id']
        self.assertTrue(manager.process_one(isolated))
        self.assertEqual(manager.store.command(command)['status'], 'success')
        self.assertEqual(book_db.execute('SELECT value FROM state').fetchone()[0], 'unrelated')
        self.assertIs(isolated.db, book_db)
        self.run_cycle(1)
        self.assertTrue(valid_retry({'type':'podcast','id':'_podcast:feed.9:ep#12'}))
        self.assertFalse(valid_retry({'type':'podcast','id':'_podcast:feed/9:12'}))


class SeriesCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.schedule = MaintenanceSchedule(self.root, {})
        self.api = series_tests.NativeAPI()
        self.occupied = True
        original = self.api.call
        def api_call(route, body=None, query=None):
            if route == 'filesystem/browse' and self.occupied:
                parent, name = self.api.new.rsplit('/', 1)
                if query['path'] in ('/audio-dramas', parent):
                    self.api.calls.append((route, body, query))
                    child = parent if query['path'] == '/audio-dramas' else self.api.new
                    return dict(currentPath=query['path'], items=[
                        dict(path=child, name=child.rsplit('/', 1)[1], isDirectory=True)])
            return original(route, body, query)
        self.api.call = api_call
        for p in (patch.object(native, 'PENDING', self.root/'journal.json'),
                  patch.object(native, 'MAINTENANCE_LOCKS', ()),
                  patch.dict(native.os.environ, {'SERIES_SETTINGS':str(self.schedule.control/'settings.json'),
                                                  'SERIES_DRAMA_MOUNT':''})):
            p.start()
            self.addCleanup(p.stop)

    def run_at(self, stamp):
        instant = datetime.datetime.fromisoformat(stamp).timestamp()
        self.api.calls.clear()
        with patch('listenarr_conflicts.time.time', return_value=instant):
            result = native.run(self.api, True)
        return result, sum(entry[0]=='filesystem/browse' for entry in self.api.calls)

    def test_target_conflict_no_daytime_reprobes_and_recovers_at_night(self):
        date = '2026-11-25T'
        first, reads = self.run_at(date + '10:00:00+01:00')
        self.assertGreater(reads, 0)
        self.assertEqual(first['listenarr_conflicts'][0]['id'],'7')
        for hour in range(11,22):
            self.assertEqual(self.run_at(date + f'{hour}:00:00+01:00')[1], 0)
        self.assertGreater(self.run_at(date + '22:00:00+01:00')[1], 0)
        self.assertEqual(self.run_at(date + '23:00:00+01:00')[1], 0)
        self.occupied = False
        result, reads = self.run_at('2026-11-26T22:00:00+01:00')
        self.assertEqual(result['moved'], 1)
        self.assertFalse(result.get('listenarr_conflicts'))

    def test_manual_retry_and_changed_catalog_reprobe_within_same_day(self):
        now = '2026-11-25T10:00:00+01:00'
        first, _ = self.run_at(now)
        (self.schedule.status_dir/'status.json').write_text(json.dumps(dict(first,status='completed')))
        self.schedule.retry_listenarr('7', 'command1')
        self.assertGreater(self.run_at(now)[1], 0)
        self.assertEqual(self.run_at(now)[1], 0)
        self.api.catalog_file_count = 2
        self.assertGreater(self.run_at(now)[1], 0)
