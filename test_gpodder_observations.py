"""Exercise the worker's observation batch, actual API parameters and on-disk cursor."""
import copy
from datetime import datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from observation_snapshot import ObservationBatch
from pinepods_client import PinepodsClient, RequestError, ActionBatchTooLarge, action_epoch
from podcast_sync import PodcastCoordinator


A = 'https://feed.example/a'
B = 'https://feed.example/b'
C = 'https://feed.example/c'
M = 'https://media.example/episode'


def action(feed, stamp, position=17):
    return dict(podcast=feed, episode=M, timestamp=stamp, action='play', device='phone',
                started=0, position=position, total=600)


class GpodderObservationTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'state.sqlite'
        self.clock = datetime.fromisoformat('2026-11-24T12:00:00+01:00').timestamp()
        self.bridge = SimpleNamespace(db=None, clock=lambda: self.clock, config={})
        self.open_db()
        self.client = PinepodsClient('https://pine.example', 'reader', 'private',
                                     api_key='private', server_version='0.9.0')
        self.addCleanup(self.client.close)
        self.client._request = Mock(side_effect=self.respond)
        self.rows = [action(A, 100), action(B, 101)]
        self.cursor = 101
        self.calls = []
        self.break_response = False

    def open_db(self):
        if self.bridge.db is not None:
            self.bridge.db.close()
        self.bridge.db = sqlite3.connect(self.path)
        self.addCleanup(self.bridge.db.close)

    def respond(self, method, path, **kw):
        self.assertEqual(method, 'GET')
        if path == '/api/data/get_user':
            return {'status': 'success', 'retrieved_id': 2}
        if path == '/api/data/user_details_id/2':
            return {'UserID': 2, 'Username': self.client.username}
        if path.startswith('/api/2/devices/'):
            return [{'id': self.client.device_id}]
        if path == '/api/data/gpodder/status':
            return dict(sync_type='gpodder', gpodder_enabled=True, external_enabled=False, external_url=None)
        self.assertEqual(path, '/api/2/episodes/' + self.client._user_path)
        params = kw['params']
        self.calls.append(params.copy())
        rows = [row for row in self.rows if (not params['since'] or action_epoch(row) > params['since'])
                and (not params.get('podcast') or params['podcast'] == row['podcast'])]
        if self.break_response:
            rows.append({'broken': True})
        page = rows[:params.get('limit', 25_000)]
        cursor = max(action_epoch(row) for row in page) if len(page) == 25_000 else self.cursor
        return {'timestamp': cursor, 'actions': copy.deepcopy(page)}

    def batch(self, apply=True):
        return ObservationBatch(self.bridge, persist_actions=apply).client(self.client)

    def test_bootstrap_then_one_delta_across_feeds_after_restart_and_no_historical_replay(self):
        b = self.batch()
        self.assertEqual(b._episode_actions(A, M), [self.rows[0]])
        self.assertEqual(b.actions(B)['actions'], [self.rows[1]])
        self.assertEqual([p['podcast'] for p in self.calls], [A,B])
        self.assertTrue(all(p['since'] == 0 for p in self.calls))
        self.open_db()
        self.calls.clear()
        self.rows.extend([action(A, 101, 28), action(A, 102, 44), action(C, 102)])
        self.cursor = 102
        b = self.batch()
        self.assertEqual(b._read_gpodder(A, M, 600)['position'], 44)
        self.assertEqual(b._read_gpodder(B, M, 600)['position'], 17)
        self.assertEqual(self.calls, [{'since': 100, 'aggregated': 'false'}])
        self.assertEqual(b.actions(C)['actions'], [self.rows[-1]])
        self.assertEqual(self.calls[-1]['podcast'], C)
        self.assertEqual(self.calls[-1]['since'], 0)

    def test_repeat_overlap_preserves_simultaneous_conflicts_and_genuine_duplicate_multiplicity(self):
        self.rows = [action(A, 100), action(A, 100)]
        self.cursor = 100
        self.assertEqual(len(self.batch().actions(A)['actions']), 2)
        self.rows.append(action(A, 100, 33))
        self.open_db()
        for _ in range(3):
            b = self.batch()
            self.assertEqual(len(b.actions(A)['actions']), 3)
            with self.assertRaisesRegex(RequestError, 'simultaneous'):
                b._read_gpodder(A, M, 600)
        self.assertTrue(all(p['since'] == 99 for p in self.calls[1:]))

    def test_failed_delta_and_interrupted_transaction_replay_from_old_cursor(self):
        self.batch().actions(A)
        self.cursor = 104
        self.rows.append(action(A, 104, 64))
        self.break_response = True
        b = self.batch()
        with self.assertRaises(RequestError):
            b.actions(A)
        with self.assertRaises(RequestError):
            b.actions(B)
        self.assertEqual(len(self.calls), 2)
        self.break_response = False
        self.bridge.db.execute("""CREATE TRIGGER interrupt_cursor BEFORE INSERT ON gpodder_observation_cursor
            BEGIN SELECT RAISE(ABORT, 'simulated interruption'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.batch().actions(A)
        self.assertEqual(self.bridge.db.execute('SELECT cursor FROM gpodder_observation_cursor').fetchone(), (101,))
        self.assertNotIn('104', self.bridge.db.execute('SELECT actions FROM gpodder_observation_feed').fetchone()[0])
        self.bridge.db.execute('DROP TRIGGER interrupt_cursor')
        self.open_db()
        self.assertEqual(self.batch().actions(A)['actions'], [self.rows[0], self.rows[2]])
        self.assertEqual([p['since'] for p in self.calls], [0,100,100,100])

    def test_committed_delta_survives_restart_before_changed_episode_was_consumed(self):
        b = self.batch()
        b.actions(A)
        b.actions(B)
        self.rows.append(action(A, 105, 77))
        self.cursor = 105
        self.batch().actions(B)  # A has not passed through the coordinator yet
        self.open_db()
        self.assertEqual(self.batch()._read_gpodder(A, M, 600)['position'], 77)
        self.assertEqual(self.calls[-1]['since'], 104)

    def test_fast_client_clock_cannot_hide_another_episodes_new_activity(self):
        present = int(self.clock)
        self.rows = [action(A, present + 3600, 250), action(B, present - 20)]
        self.cursor = present + 3600
        b = self.batch()
        b.actions(A)
        b.actions(B)
        self.clock += 60
        self.rows.append(action(B, present + 30, 150))
        self.assertEqual(self.batch()._read_gpodder(B, M, 600)['position'], 150)
        self.assertEqual(self.calls[-1]['since'], present - 1)

    def test_rollback_and_limit_require_new_verified_feed_before_use(self):
        self.batch().actions(A)
        self.rows = [action(A, 21, 31)]
        self.cursor = 21
        self.assertEqual(self.batch()._read_gpodder(A, M, 600)['position'], 31)
        self.assertEqual(self.calls[-1]['podcast'], A)
        self.client.MAX_ACTIONS = 2
        self.cursor = 24
        self.rows.extend([action(B, 22), action(B, 23), action(C, 24)])
        self.assertEqual(self.batch()._read_gpodder(A, M, 600)['position'], 31)
        self.assertEqual([p.get('limit') for p in self.calls[-3:]], [None,1,None])
        self.assertEqual(self.calls[-1]['podcast'], A)

    def test_25000_equal_timestamp_rows_never_become_a_partial_feed_or_skip_a_sibling(self):
        self.rows = [action(B, 101)]
        self.batch().actions(B)
        self.cursor = 150
        self.rows.extend([action(A, 150)] * 25_000)
        self.calls.clear()
        b = self.batch()
        with self.assertRaises(ActionBatchTooLarge):
            b.actions(A)
        self.assertEqual(b.actions(B)['actions'], [action(B, 101)])
        self.assertFalse(self.bridge.db.execute('SELECT 1 FROM gpodder_observation_feed WHERE feed=?',
                                                (A,)).fetchone())
        self.assertEqual([p.get('limit') for p in self.calls], [None,1,None,None])

    def test_backdated_offline_actions_found_in_night_window_one_complete_feed_per_cycle(self):
        b = self.batch()
        b.actions(A)
        b.actions(B)
        self.rows.append(action(A, 98, 2))
        self.calls.clear()
        self.assertEqual(len(self.batch().actions(A)['actions']), 1)
        self.clock += 10*3600
        b = self.batch()
        self.assertEqual(len(b.actions(A)['actions']), 2)
        b.actions(B)
        self.assertEqual([p.get('podcast') for p in self.calls], [None,None,A])
        self.calls.clear()
        self.batch().actions(A)
        self.assertEqual([p.get('podcast') for p in self.calls], [None,B])

    def test_dirty_target_rechecks_feed_and_alias_history_other_feeds_stay_cached(self):
        b = self.batch()
        b._episode_actions(A, M)
        b._episode_actions(B, M)
        self.calls.clear()
        self.rows[0]['position'] = 200
        b.invalidate_progress(feed_url=A, enclosure_url=M)
        self.assertEqual(b._read_gpodder(A, M, 600)['position'], 200)
        self.assertEqual(b.actions(A)['actions'][0]['position'], 200)
        self.assertEqual(b._read_gpodder(B, M, 600)['position'], 17)
        self.assertEqual([p.get('podcast') for p in self.calls], [A])

    def test_live_receipt_change_reseeds_only_affected_feed_even_after_restart(self):
        b = self.batch()
        b.actions(A)
        b.actions(B)
        self.rows.append(action(A,95,50))
        self.rows = [row for row in self.rows if not (row['podcast'] == A and row['timestamp'] == 100)]
        c = PodcastCoordinator.__new__(PodcastCoordinator)
        c.b, c.pine = self.bridge, self.client
        c.observation = ObservationBatch(self.bridge, persist_actions=True)
        mapping = dict(feed_url=A, enclosure_url=M)
        observed = c.observation.client(c.pine)._read_gpodder(A, M, 600)
        fresh = self.client._read_gpodder(A, M, 600)
        self.assertEqual((observed['position'],fresh['position']), (17,50))
        c.refresh_changed_gpodder(mapping, {'pinepods':{'gpodder_revision':observed['revision']}},
                                         {'pinepods':{'gpodder_revision':fresh['revision']}})
        self.open_db()
        self.calls.clear()
        b = self.batch()
        self.assertEqual(b._read_gpodder(A, M, 600)['position'], 50)
        self.assertEqual(b._read_gpodder(B, M, 600)['position'], 17)
        self.assertEqual([p.get('podcast') for p in self.calls], [None,A])

    def test_distinct_accounts_isolate_histories_and_dry_run_does_not_seed(self):
        self.batch(apply=False).actions(A)
        self.assertFalse(self.bridge.db.execute("""SELECT name FROM sqlite_master
            WHERE name LIKE 'gpodder_observation_%'""").fetchall())
        self.batch().actions(A)
        self.rows = [action(A, 89, 54)]
        self.cursor = 89
        self.client.username = 'different'
        self.client._user_path = 'different.json'
        self.calls.clear()
        self.assertEqual(self.batch()._read_gpodder(A, M, 600)['position'], 54)
        self.assertEqual(self.calls, [{'since':0, 'aggregated':'false', 'podcast':A}])
        self.assertEqual(self.bridge.db.execute('SELECT COUNT(*) FROM gpodder_observation_cursor').fetchone(), (2,))

    def test_thousand_unchanged_episodes_use_one_shared_delta_after_bootstrap(self):
        self.rows = [dict(action(A if i % 2 else B, 100 + i), episode=M + str(i)) for i in range(1000)]
        self.cursor = 1099
        for _ in range(2):
            b = self.batch()
            self.calls.clear()
            for row in self.rows:
                self.assertEqual(b._episode_actions(row['podcast'], row['episode']), [row])
        self.assertEqual(self.calls, [{'since':1098, 'aggregated':'false'}])

    def test_recurring_worker_probe_checks_endpoint_without_loading_account_history(self):
        self.rows.clear()
        result = self.client.probe()
        self.assertTrue(result['native_import_disabled'])
        self.assertEqual(result['sampled_actions'],0)
        self.assertEqual(self.calls[0]['limit'], 1)
        self.assertGreater(self.calls[0]['since'], 0)


if __name__ == '__main__':
    unittest.main()
