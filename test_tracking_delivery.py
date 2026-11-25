import json
from pathlib import Path
import tempfile
import unittest

from gpodder_relay import Relay
from test_native_tracking_relay import BASE, ExactResolver, NativeSource, Upstream, native
from tracking_delivery import lookup_key, read_episode_delivery, search_episode_delivery


class EpisodeDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'relay.sqlite'
        self.clock = BASE + 100
        self.ident = '2:13:32'
        self.item = native(self.ident, title='Straßen & Bücher', show='Podcast', updated_at=BASE - 10)
        self.source = NativeSource([self.item])
        self.config = dict(upstream='https://pine.example.test', username='reader', baseline=BASE,
                           native_source=dict(user_id=2))
        self.restart()

    def restart(self):
        self.relay = Relay(self.config, self.path.parent, Upstream(), source=self.source,
                           resolver=ExactResolver(), clock=lambda: self.clock)

    def poll(self, since=None):
        return self.relay.actions(since, '', 'Basic test')['timestamp']

    def detail(self, **scope):
        return read_episode_delivery(self.path, episode_id=self.ident, **scope)

    def test_baseline_and_retries_ack_during_outage_restarts_and_reset(self):
        baseline = self.poll()
        self.assertEqual(self.detail()['event']['stage'], 'baseline')
        self.assertEqual(self.detail()['source']['updated_at'], BASE - 10)
        self.clock += 3
        self.item.update(position=400, completed=True)
        cursor = self.poll(baseline)
        offered = self.detail()
        self.assertEqual(offered['event']['stage'], 'offered')
        self.assertEqual((offered['event']['ready_at'], offered['event']['offered_at']), (self.clock, self.clock))
        self.assertEqual(offered['event']['cursor'], cursor)
        self.assertEqual(offered['source']['position'], 400)
        self.assertEqual(offered['target'], {'state': 'unverified'})
        self.clock += 7
        self.restart()
        self.assertEqual(self.poll(), cursor)
        self.assertEqual(self.detail(), offered)
        with self.assertRaises(ValueError):
            self.poll(cursor - 1)
        with self.assertRaises(ValueError):
            self.relay.actions(cursor, 'wrong-device', 'Basic test')
        self.assertEqual(self.detail(), offered)
        self.source.offline = True
        with self.assertRaises(ValueError):
            self.poll(cursor)
        self.restart()
        self.assertEqual(self.detail()['event']['stage'], 'acknowledged')
        self.assertEqual(self.detail()['event']['acknowledged_at'], self.clock)
        self.assertEqual(self.detail()['target'], {'state': 'unverified'})
        self.source.offline = False
        self.item.update(position=0, completed=False)
        self.poll(cursor)
        self.assertEqual(self.detail()['event']['stage'], 'no_event')
        self.assertEqual(self.detail()['event']['cursor'], None)

    def test_new_event_blocked_and_old_ack_does_not_confirm_it(self):
        old = self.poll()
        self.clock += 1
        self.item.update(position=25)
        issued = self.poll(old)
        self.clock += 1
        self.item.update(completed=True)
        def unavailable(_):
            raise ValueError('changed')
        self.relay.resolver.resolve = unavailable
        self.poll(issued)
        self.assertEqual(self.detail()['event']['stage'], 'blocked')
        self.assertIsNone(self.detail()['event']['cursor'])

    def test_offered_identity_uses_actual_alias_and_catches_cross_episode_collisions(self):
        other = native('2:13:99', position=0)
        self.source.rows.append(other)
        initial = self.poll()
        self.clock += 1
        self.item['position'] = 597
        other['position'] = 10
        alias = 'https://streams.test/actual.mp3'
        self.relay.resolver.resolve = lambda action: dict(action, episode=alias)
        self.poll(initial)
        match = self.detail()['delivery_match']
        self.assertNotEqual(match['lookup_hash'], match['wire_hash'])
        self.assertTrue(match['unique'])
        self.assertFalse(match['wire_unique'])
        self.assertEqual(match['wire_hash'], lookup_key(self.item['podcast'], alias))
        self.assertEqual(match['wire_position'], self.item['total'] - 6)

    def test_failed_poll_keeps_source_event_ready_and_next_poll_can_offer_it(self):
        initial = self.poll()
        self.clock += 1
        self.item['completed'] = True
        def broken(_):
            raise RuntimeError('unexpected adapter failure')
        self.relay.resolver.resolve = broken
        with self.assertRaises(RuntimeError):
            self.poll(initial)
        self.assertEqual(self.detail()['event']['stage'], 'ready')
        self.assertTrue(self.detail()['source']['present_in_last_scan'])
        self.restart()
        self.poll(initial)
        self.assertEqual(self.detail()['event']['stage'], 'offered')

    def test_upgrade_without_timestamps_backfills_hash_and_only_known_facts(self):
        initial = self.poll()
        self.clock += 1
        self.item.update(completed=True)
        issued = self.poll(initial)
        # Reproduce a beta16 database mid-batch. Source snapshots, events,
        # seen fingerprints and the original pending payload are retained.
        with self.relay.db() as db:
            db.execute('DROP TABLE native_delivery_receipts')
            db.execute('DROP INDEX native_lookup_hash')
            db.execute('ALTER TABLE native_tracking_episodes DROP COLUMN lookup_hash')
            db.execute('ALTER TABLE native_tracking_episodes DROP COLUMN scan_number')
            db.execute("DELETE FROM state WHERE id='delivery_mode'")
        self.restart()
        key = lookup_key(self.item['podcast'], self.item['episode'])
        self.assertEqual(read_episode_delivery(self.path, lookup_hash=key)['event']['cursor'], issued)
        self.assertEqual(self.poll(), issued)
        self.clock += 1
        self.poll(issued)
        with self.relay.db() as db:
            db.execute('DELETE FROM native_delivery_receipts')
        self.assertEqual(self.detail()['event']['stage'], 'acknowledged')
        self.assertIsNone(self.detail()['event']['acknowledged_at'])
        self.assertIsNone(self.detail()['event']['cursor'])

    def test_native_search_and_scope_without_playback_changes_or_private_urls(self):
        self.item.update(episode='https://streams.test/private?token=unused')
        self.source.rows.append(native('2:13:33', title='Straße zwei', updated_at=BASE))
        self.poll()
        for scope in ({'username': 'other'}, {'user_id': 3}, {'upstream': 'https://other.test'}):
            self.assertEqual(self.detail(**scope), {'state': 'scope_mismatch'})
            self.assertEqual(search_episode_delivery(self.path, **scope), {'state': 'scope_mismatch'})
        first = search_episode_delivery(self.path, query='STRASSE', limit=1)
        second = search_episode_delivery(self.path, query='strasse', limit=1, offset=1)
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])
        self.assertNotEqual(first['rows'][0]['episode_id'], second['rows'][0]['episode_id'])
        self.assertFalse(search_episode_delivery(self.path, query='%')['rows'])
        self.assertNotIn('https:', json.dumps([first, self.detail()]))
        self.assertEqual(self.source.calls, ['Basic test'])
        self.assertEqual(self.detail(username='reader', user_id=2)['source']['completed'], False)

    def test_missing_legacy_and_ambiguous_identity(self):
        missing = self.path.with_name('uninstalled')
        self.assertEqual(search_episode_delivery(missing), {'state': 'unavailable'})
        self.assertFalse(missing.exists())
        self.assertEqual(self.detail(), {'state': 'not_found'})
        self.source.rows.append(native('2:13:34', episode=self.item['episode']))
        self.poll()
        self.assertEqual(read_episode_delivery(self.path, feed_url=self.item['podcast'],
            enclosure_url=self.item['episode']), {'state': 'ambiguous'})
        legacy = dict(self.config)
        del legacy['native_source']
        Relay(legacy, self.path.parent, Upstream())
        self.assertEqual(self.detail(), {'state': 'legacy'})

    def test_reupgrade_after_old_relay_ack_and_enclosure_change(self):
        initial = self.poll()
        self.item['completed'] = True
        self.clock += 1
        self.poll(initial)
        old_hash = lookup_key(self.item['podcast'], self.item['episode'])
        with self.relay.db() as db:
            fingerprint = self.relay.get(db, 'pending')['fingerprints'][0]
            db.execute('INSERT INTO seen(hash) VALUES(?)', (fingerprint,))
            self.relay.set(db, 'pending', None)
            db.execute("UPDATE native_tracking_episodes SET snapshot=json_set(snapshot,'$.episode',?)",
                       ('https://stream.test/new',))
        self.restart()
        self.assertEqual(read_episode_delivery(self.path, lookup_hash=old_hash), {'state': 'not_found'})
        event = self.detail()['event']
        self.assertEqual(event['stage'], 'acknowledged')
        self.assertIsNone(event['acknowledged_at'])

    def test_same_second_removed_episode_is_explicitly_stale(self):
        first = self.poll()
        self.source.rows.clear()
        self.poll(first)
        self.assertIs(self.detail()['source']['present_in_last_scan'], False)
