import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tracking_projection import NativeProjection


NOW = 1_800_000_000


def snapshot(identifier='account:podcast:episode', **changes):
    row = dict(id=identifier, podcast='https://example.test/feed',
               episode='https://example.test/episode.mp3', guid='stable-guid',
               completed=False, position=0, total=600)
    row.update(changes)
    return row


class NativeProjectionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)

    def update(self, rows, now=NOW, recovery_ids=()):
        return NativeProjection.update(self.db, rows, now, recovery_ids)

    def events(self):
        return NativeProjection.events(self.db)

    def state(self):
        return list(self.db.execute('SELECT * FROM native_tracking_episodes ORDER BY id'))

    def test_sixteen_thousand_initial_completed_rows_create_no_plays(self):
        rows = [snapshot(str(i), completed=True, position=600) for i in range(16000)]
        self.assertEqual(self.update(rows), [])
        self.assertEqual(len(self.state()), 16000)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.update(rows, NOW + 1), [])

    def test_native_completion_without_any_gpodder_receipt_emits_once(self):
        self.update([snapshot()])
        events = self.update([snapshot(completed=True, position=598)], NOW + 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['action'], 'play')
        self.assertEqual(events[0]['position'], 600)
        self.assertEqual(events[0]['total'], 600)
        self.assertEqual(events[0]['guid'], 'stable-guid')
        self.assertTrue(events[0]['_tracking_completed'])
        self.assertEqual(self.update([snapshot(completed=True, position=600)], NOW + 2), [])

    def test_initial_started_rows_are_baseline_without_explicit_recovery(self):
        self.assertEqual(self.update([snapshot(position=100)]), [])
        self.assertEqual(self.events(), [])

    def test_recovery_is_exact_audited_and_repeated_requests_do_not_replay(self):
        active, inactive, other = snapshot('active', completed=True), snapshot('zero'), snapshot('other', position=10)
        events = self.update([active, inactive, other], recovery_ids=['active', 'zero'])
        self.assertEqual([e['_tracking_episode_id'] for e in events], ['active'])
        audit = self.db.execute("SELECT snapshot FROM native_tracking_events WHERE kind='baseline'").fetchone()[0]
        self.assertEqual(json.loads(audit)['recovery_ids'], ['active', 'zero'])
        self.assertEqual(self.update([active, inactive, other], NOW + 1, ['active', 'zero']), [])
        self.assertEqual(self.events(), events)

    def test_unseen_newly_published_completed_and_started_rows_after_baseline_emit(self):
        self.update([snapshot('old', completed=True)])
        rows = [snapshot('old', completed=True),
                snapshot('new', completed=True, published_at=NOW + 1),
                snapshot('started', position=123, updated_at=NOW + 1)]
        events = self.update(rows, NOW + 1)
        self.assertEqual([e['_tracking_episode_id'] for e in events], ['new', 'started'])
        self.assertEqual(self.update(rows, NOW + 2), [])

    def test_consumed_recovery_plan_survives_later_unsubscription(self):
        row = snapshot('recovered', completed=True)
        first = self.update([row], recovery_ids=['recovered'])
        self.assertEqual(len(first), 1)
        self.assertEqual(self.update([], NOW + 1, ['recovered']), [])
        self.assertEqual(self.update([row], NOW + 2, ['recovered']), [])
        self.assertEqual(self.events(), first)

    def test_initial_empty_inventory_is_a_real_baseline(self):
        self.assertEqual(self.update([]), [])
        self.assertEqual(len(self.update([snapshot(completed=True, published_at=NOW + 1)], NOW + 1)), 1)

    def test_new_subscription_does_not_import_sixteen_thousand_old_completions(self):
        self.update([snapshot('existing')])
        rows = [snapshot('existing')] + [
            snapshot(str(i), podcast='https://new-subscription.test/feed', completed=True,
                     position=600, published_at=NOW - 10000, updated_at=NOW - 500)
            for i in range(16000)]
        self.assertEqual(self.update(rows, NOW + 1), [])
        self.assertEqual(self.events(), [])

    def test_unseen_old_episode_heard_today_uses_actual_recent_activity(self):
        self.update([snapshot('existing')])
        events = self.update([snapshot('old-episode', completed=True,
            published_at=NOW - 10000, updated_at=NOW + 1)], NOW + 2)
        self.assertEqual(len(events), 1)

    def test_unseen_old_episode_without_activity_evidence_is_only_baselined(self):
        self.update([snapshot('existing')])
        self.assertEqual(self.update([snapshot('old-episode', completed=True)], NOW + 1), [])
        self.assertEqual(self.events(), [])

    def test_empty_poll_advances_watermark_for_a_later_new_subscription(self):
        self.update([], NOW)
        self.update([], NOW + 10)
        self.assertEqual(self.update([snapshot(completed=True,
            updated_at=NOW + 5, published_at=NOW - 100)], NOW + 20), [])

    def test_own_receipt_echo_and_completed_metadata_changes_do_not_add_plays(self):
        self.update([snapshot()])
        event = self.update([snapshot(completed=True, position=600)], NOW + 1)
        for index, changed in enumerate([
            snapshot(completed=True, position=600, updated_at=NOW + 2),
            snapshot(completed=True, position=0, total=650),
            snapshot(completed=True, position=700, total=550),
            snapshot(completed=True, position=700, total=550,
                     episode='https://example.test/new.mp3', podcast='https://example.test/new-feed', guid='same-verified-episode'),
        ], 2):
            self.assertEqual(self.update([changed], NOW + index), [])
        self.assertEqual(self.events(), event)

    def test_progress_is_preserved_and_duration_only_change_is_not_activity(self):
        self.update([snapshot()])
        event = self.update([snapshot(position=120.5)], NOW + 1)[0]
        self.assertEqual(event['position'], 120.5)
        self.assertFalse(event['_tracking_completed'])
        self.assertEqual(self.update([snapshot(position=120.5, total=700)], NOW + 2), [])
        self.assertEqual(len(self.update([snapshot(position=130, total=700)], NOW + 3)), 1)

    def test_explicit_reset_records_generation_but_never_fabricates_a_play(self):
        self.update([snapshot()])
        first = self.update([snapshot(completed=True)], NOW + 1)[0]
        self.assertEqual(self.update([snapshot()], NOW + 2), [])
        self.assertEqual(self.update([snapshot()], NOW + 3), [])
        second = self.update([snapshot(completed=True)], NOW + 4)[0]
        self.assertNotEqual(first['_tracking_event_id'], second['_tracking_event_id'])
        self.assertEqual(second['_tracking_generation'], first['_tracking_generation'] + 1)
        self.assertEqual(len(self.events()), 2)

    def test_resume_after_completed_without_intermediate_zero_starts_generation(self):
        self.update([snapshot(completed=True)])
        event = self.update([snapshot(position=100)], NOW + 1)[0]
        self.assertEqual(event['_tracking_generation'], 1)
        self.assertEqual(event['position'], 100)
        finished = self.update([snapshot(completed=True)], NOW + 2)[0]
        self.assertEqual(finished['_tracking_generation'], 1)

    def test_missing_row_is_not_a_reset_or_a_new_listen_when_it_reappears(self):
        self.update([snapshot(completed=True)])
        self.assertEqual(self.update([], NOW + 1), [])
        self.assertEqual(self.update([snapshot(completed=True)], NOW + 2), [])

    def test_timestamps_use_valid_native_time_and_fallback_once(self):
        self.update([snapshot()])
        native = self.update([snapshot(completed=True, updated_at=NOW + 0.5)], NOW + 1)[0]
        self.assertEqual(datetime.fromisoformat(native['timestamp'].replace('Z', '+00:00')).timestamp(), NOW + 0.5)
        saved = copy.deepcopy(self.events())
        self.assertEqual(self.update([snapshot(completed=True, updated_at=NOW + 2)], NOW + 2), [])
        self.assertEqual(self.events(), saved)
        self.update([snapshot()], NOW + 3)
        fallback = self.update([snapshot(position=10)], NOW + 4)[0]
        self.assertEqual(datetime.fromisoformat(fallback['timestamp'].replace('Z', '+00:00')).timestamp(), NOW + 4)

    def test_native_completion_with_stale_history_time_uses_new_edge_observation_once(self):
        stale_time = NOW - 10000
        self.update([snapshot(position=100, updated_at=stale_time)])
        event = self.update([snapshot(completed=True, position=100,
                                      updated_at=stale_time)], NOW + 1)[0]
        self.assertEqual(datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp(), NOW + 1)
        stored = json.loads(self.state()[0][1])
        self.assertEqual(stored['updated_at'], stale_time)
        self.assertEqual(self.update([snapshot(completed=True, position=100,
            updated_at=stale_time)], NOW + 100), [])
        self.assertEqual(self.events(), [event])

    def test_explicit_first_recovery_preserves_actual_historical_activity_time(self):
        old_time = NOW - 10000
        event = self.update([snapshot(completed=True, updated_at=old_time)],
                            recovery_ids=[snapshot()['id']])[0]
        self.assertEqual(datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp(), old_time)
        self.assertEqual(self.update([snapshot(completed=True, updated_at=old_time)],
                                     NOW + 10, [snapshot()['id']]), [])
        self.assertEqual(self.events(), [event])

    def test_invalid_optional_native_timestamp_uses_observation(self):
        for index, value in enumerate([None, 'invalid', False, float('nan'), float('inf'), -1], 1):
            row = snapshot(str(index), position=10, updated_at=value)
            events = self.update([row], NOW + index, [str(index)]) if index == 1 else self.update(
                [{**row, 'published_at': NOW + index}], NOW + index)
            self.assertEqual(len(events), 1)
            self.assertEqual(datetime.fromisoformat(events[0]['timestamp'].replace('Z', '+00:00')).timestamp(), NOW + index)

    def test_restart_replays_immutable_events_without_new_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'relay.sqlite'
            first = sqlite3.connect(file)
            NativeProjection.update(first, [snapshot()], NOW)
            expected = NativeProjection.update(first, [snapshot(completed=True)], NOW + 1)
            first.close()
            second = sqlite3.connect(file)
            try:
                self.assertEqual(NativeProjection.events(second), expected)
                self.assertEqual(NativeProjection.update(second, [snapshot(completed=True)], NOW + 100), [])
                self.assertEqual(NativeProjection.events(second), expected)
            finally:
                second.close()

    def test_event_results_are_detached_from_persisted_payloads(self):
        self.update([snapshot()])
        event = self.update([snapshot(completed=True)], NOW + 1)[0]
        expected = copy.deepcopy(event)
        event['position'] = 1
        returned = self.events()
        self.assertEqual(returned, [expected])
        returned[0]['position'] = 2
        self.assertEqual(self.events(), [expected])

    def test_invalid_inventory_is_all_or_nothing_before_any_schema_change(self):
        bad_rows = [
            snapshot(completed=1), snapshot(position=-1), snapshot(total=0),
            snapshot(position=float('nan')), snapshot(total=float('inf')),
            snapshot(position=True), snapshot(position=601), snapshot(episode='bad'),
            snapshot(podcast='https://user:password@example.test/feed'),
            snapshot(guid=''), snapshot(id=''), {**snapshot(), 'surprise': 1},
            snapshot(updated_at=NOW + 100),
        ]
        for bad in bad_rows:
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    self.update([snapshot('valid', completed=True), bad], recovery_ids=['valid'])
                self.assertEqual(self.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'native_tracking_%'").fetchall(), [])

    def test_duplicate_ids_are_rejected_without_changing_existing_state(self):
        self.update([snapshot()])
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'duplicate_native_tracking_id'):
            self.update([snapshot(completed=True), snapshot(position=100)], NOW + 1)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.events(), [])

    def test_unknown_recovery_id_is_rejected_before_changes(self):
        with self.assertRaisesRegex(ValueError, 'unknown_native_tracking_recovery_id'):
            self.update([snapshot()], recovery_ids=['another-account'])
        self.assertEqual(self.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'native_tracking_%'").fetchall(), [])

    def test_regressing_observation_keeps_all_state_unchanged(self):
        self.update([snapshot('a'), snapshot('b')])
        self.update([snapshot('b')], NOW + 10)
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'observation_regressed'):
            self.update([snapshot('a', completed=True), snapshot('b')], NOW + 5)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.events(), [])

    def test_late_database_failure_rolls_back_earlier_rows_and_events(self):
        self.update([snapshot('a'), snapshot('b')])
        self.db.execute('''CREATE TRIGGER reject_b BEFORE UPDATE ON native_tracking_episodes
            WHEN NEW.id='b' BEGIN SELECT RAISE(ABORT,'test failure'); END''')
        before = self.state()
        with self.assertRaises(sqlite3.IntegrityError):
            self.update([snapshot('a', completed=True), snapshot('b', completed=True)], NOW + 1)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.events(), [])

    def test_failed_update_preserves_an_enclosing_owners_transaction(self):
        self.update([snapshot()])
        self.db.execute('''CREATE TRIGGER reject_update BEFORE UPDATE ON native_tracking_episodes
            BEGIN SELECT RAISE(ABORT,'test failure'); END''')
        self.db.execute('CREATE TABLE owner_state (value INTEGER)')
        self.db.execute('INSERT INTO owner_state VALUES(1)')
        with self.assertRaises(sqlite3.IntegrityError):
            self.update([snapshot(completed=True)], NOW + 1)
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.db.execute('SELECT * FROM owner_state').fetchall(), [(1,)])
        self.assertEqual(self.events(), [])

    def test_projection_does_not_commit_owners_transaction(self):
        self.db.execute('CREATE TABLE owner_state (value INTEGER)')
        self.db.execute('INSERT INTO owner_state VALUES(1)')
        self.assertTrue(self.db.in_transaction)
        self.update([snapshot()])
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT * FROM owner_state').fetchall(), [])
        self.assertEqual(self.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'native_tracking_%'").fetchall(), [])


if __name__ == '__main__':
    unittest.main()
