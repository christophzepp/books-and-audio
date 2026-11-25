import copy
from contextlib import contextmanager, ExitStack
from datetime import datetime
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import test_podcast_sync as fixture
from pinepods_client import EpisodeUnavailable
from podcast_media_queue import FileCheckScheduled, PodcastFileChecks, PREFIX, require_file_check
from podcast_sync import PodcastCoordinator, preview
from podcast_variant_proof import VariantProof


class FileQueueTests(unittest.TestCase):
    load = fixture.Tests.load
    save = fixture.Tests.save
    write_record = fixture.Tests.write_record
    write_gate = fixture.Tests.write_gate
    cycle = fixture.Tests.cycle
    add_pine = fixture.Tests.add_pine
    optional_pine = fixture.Tests.optional_pine

    def setUp(self):
        fixture.Tests.setUp(self)
        self.pine = self.optional_pine()
        self.cfg['podcasts']['pinepods_variant_status_only'] = True
        self.media = self.root.resolve()/'media'
        self.episode = self.media/'Show'/'episode.mp3'
        self.episode.parent.mkdir(parents=True)
        self.episode.write_bytes(b'm' * 1000)
        os.utime(self.episode, ns=(fixture.STAMP * 1000000,) * 2)
        self.cfg['podcasts']['worker_root'] = str(self.media)
        self.gate['podcast_mounts']['worker']['destination'] = str(self.media)
        self.db.execute('CREATE TABLE events(at TEXT,book TEXT,kind TEXT,detail TEXT)')
        self.at(10)

    def at(self, hour, minute=0, day=25):
        self.clock = datetime(2026,11,day,hour,minute,tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
        self.gate['checked_at'] = self.clock
        self.write_gate()

    @contextmanager
    def watch_media(self):
        calls = []
        def counted(name):
            original = getattr(Path,name)
            def call(path, *args, **kwargs):
                if path.is_relative_to(self.media):
                    calls.append((name,path))
                return original(path,*args,**kwargs)
            return call
        with ExitStack() as stack:
            for name in ('resolve','stat','open'):
                stack.enter_context(patch.object(Path,name,counted(name)))
            yield calls

    def test_100_polls_preserve_enrollment_and_resume_with_current_positions(self):
        self.abs.set(20)
        self.assertTrue(self.cycle()['ok'])
        key = '_podcast:show:episode'
        old = copy.deepcopy(self.load(key))
        self.abs.set(45)
        self.at(21,45)
        with self.watch_media() as files, patch.object(self.pine,'read_episode',side_effect=EpisodeUnavailable('duration_mismatch')):
            statements = []
            self.db.set_trace_callback(statements.append)
            try:
                for _ in range(100):
                    result = self.cycle()
                    self.assertTrue(result['ok'],result)
                    self.assertEqual(result['file_checks_pending'],1)
                    self.assertEqual(result['pine_waiting'],1)
                    self.assertEqual(result['episodes'][0]['pine_reason_code'],'media_check_scheduled')
            finally:
                self.db.set_trace_callback(None)
        self.assertEqual(files,[])
        self.assertEqual(sum(statement.startswith('INSERT') and PREFIX in statement for statement in statements),1)
        self.assertEqual(self.load(key),old)
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],20*fixture.TICKS)
        # Publisher fixed its metadata meanwhile; the queue carries no stale
        # goal. The new ABS position reaches both peers in the very next poll.
        self.abs.set(85)
        with self.watch_media() as files:
            result = self.cycle()
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['file_checks_pending'],0)
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],85*fixture.TICKS)
        self.assertEqual(self.pine.state['position'],85)
        self.assertEqual(files,[])

    def test_retry_backoff_expires_but_same_window_wakes_only_once(self):
        self.pine.inspect_episode_metadata = lambda *args: dict(duration=404,downloaded=False,completed=False,position=0)
        with patch.object(self.pine,'read_episode',side_effect=EpisodeUnavailable('duration_mismatch')), \
                patch('pine_metadata_repair.PineMetadataRepair._measure',
                      return_value={'known':False,'reason':'probe_timeout'}) as measure:
            with self.watch_media() as files:
                self.assertEqual(self.cycle()['file_checks_pending'],1)
                self.assertEqual(files,[])
                self.at(22)
                self.assertEqual(self.cycle()['file_checks_pending'],1)
                self.assertIn(('stat',self.episode), files)
            self.at(22,31)
            # Rebuilding the coordinator reloads its already claimed window from SQLite.
            with self.watch_media() as files:
                for _ in range(5):
                    self.assertEqual(self.cycle()['file_checks_pending'],1)
                    self.assertTrue(preview(self.b)['ok'])
                self.at(6,day=26)
                self.assertEqual(self.cycle()['file_checks_pending'],1)
                self.assertEqual(files,[])
            self.assertEqual(measure.call_count,1)
            self.at(22,day=26)
            with self.watch_media() as files:
                self.assertEqual(self.cycle()['file_checks_pending'],1)
                self.assertIn(('stat',self.episode),files)
            self.assertEqual(measure.call_count,2)

    def test_no_variant_history_requires_no_candidate_file_or_queue_slot(self):
        mappings,_ = PodcastCoordinator(self.b).mappings()
        mapping = mappings[0]
        for hour in (12,22):
            self.at(hour)
            checks = PodcastFileChecks(self.b,True)
            with self.watch_media() as files, checks.episode(mapping):
                self.assertFalse(VariantProof(self.b).candidate(mapping))
            self.assertEqual(files,[])
            self.assertEqual(checks.pending_count,0)

    def test_queue_budget_removal_new_identity_and_overnight_retry(self):
        mapping = PodcastCoordinator(self.b).mappings()[0][0]
        self.cfg['podcast_maintenance'] = {'start_hour':22,'end_hour':3,'timezone':'Europe/Berlin'}
        rows = [dict(mapping,id=mapping['id']+str(i)) for i in range(4)]
        self.at(22)
        checks = PodcastFileChecks(self.b,True)
        for row in rows[:3]:
            with checks.episode(row):
                require_file_check(self.b,row)
        with checks.episode(rows[-1]), self.assertRaises(FileCheckScheduled):
            require_file_check(self.b,rows[-1])
        self.at(1,day=26)
        checks = PodcastFileChecks(self.b,True)
        with checks.episode(rows[0]), self.assertRaises(FileCheckScheduled):
            require_file_check(self.b,rows[0])
        with checks.episode(rows[-1]):
            require_file_check(self.b,rows[-1])
        replaced = dict(rows[0],fingerprint='new-version')
        with checks.episode(replaced):
            require_file_check(self.b,replaced)
        checks.complete(replaced['id'])
        with checks.episode(replaced), self.assertRaises(FileCheckScheduled):
            require_file_check(self.b,replaced)
        # A complete inventory removes only stale scheduling entries; historical
        # baselines and approvals belong to different namespaces.
        self.save('_pine_transition:'+rows[0]['id'],{'preserved':True})
        self.assertTrue(self.cycle()['ok'])
        self.assertEqual(PodcastFileChecks(self.b,True).pending_count,0)
        self.assertEqual(self.load('_pine_transition:'+rows[0]['id']),{'preserved':True})

    def test_short_windows_cannot_starve_later_episodes_in_a_long_queue(self):
        mapping = PodcastCoordinator(self.b).mappings()[0][0]
        rows = [dict(mapping, id=f'episode-{i}') for i in range(6)]
        checks = PodcastFileChecks(self.b,True)
        for row in rows:
            with checks.episode(row), self.assertRaises(FileCheckScheduled):
                require_file_check(self.b,row)
        for day in (25, 26):
            self.at(22, day=day)
            checks = PodcastFileChecks(self.b,True)
            claimed = []
            for row in checks.maintenance_order(rows):
                with checks.episode(row):
                    try:
                        require_file_check(self.b,row)
                    except FileCheckScheduled:
                        continue
                    claimed.append(row['id'])
            expected = rows[:3] if day == 25 else rows[3:]
            self.assertEqual(claimed, [row['id'] for row in expected])

    def test_two_workers_cannot_claim_the_same_episode_in_one_window(self):
        mapping = PodcastCoordinator(self.b).mappings()[0][0]
        self.at(22)
        first = PodcastFileChecks(self.b,True)
        second = PodcastFileChecks(self.b,True)
        with first.episode(mapping):
            require_file_check(self.b,mapping)
        with second.episode(mapping), self.assertRaises(FileCheckScheduled):
            require_file_check(self.b,mapping)
        self.assertEqual(first.entries,second.entries)


class KnownVariantTests(unittest.TestCase):
    def test_cached_variant_stays_available_and_changed_identity_waits_for_the_window(self):
        from test_podcast_positive_idle_io import PositiveIdleIOTests
        case = PositiveIdleIOTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        # The unit fixture bypasses bind(); supply a real JF binding to the
        # scheduling wrapper while retaining the historical variant fixture.
        queued = dict(case.mapping, jf=dict(case.mapping['jf'],fingerprint='test-shared-file'))
        case.warm()
        case.b.time = datetime(2026,11,25,14,tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
        queue = PodcastFileChecks(case.b,True)
        with queue.episode(queued), patch.object(VariantProof,'_inspect_source') as physical:
            self.assertTrue(case.inspect(authorize=True)['ready'])
            case.generation += '-new'
            self.assertEqual(case.inspect()['reason'],'media_check_scheduled')
            self.assertFalse(case.inspect(authorize=True)['ready'])
            physical.assert_not_called()
        self.assertEqual(queue.pending_count,1)
        self.assertEqual(case.b.pinepods.posts,[])
        case.b.time = datetime(2026,11,25,22,tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
        with PodcastFileChecks(case.b,True).episode(queued):
            self.assertTrue(case.inspect()['ready'])
            self.assertTrue(case.inspect(authorize=True)['ready'])
        case.probe.assert_not_called()


if __name__=='__main__':
    unittest.main()
