"""Native state -> durable relay -> stock GPodder-consumer contract tests."""
import copy
import html
import tempfile
import unittest
from datetime import datetime, timezone

import requests

from gpodder_relay import Relay
from tracking_rss import TrackingResolver


BASE = 1_700_000_000
FEED = 'https://podcasts.example.test/feed'


def native(identifier='u2:p3:e4', **changes):
    result = dict(id=identifier, podcast=FEED,
        episode='https://media.example.test/' + identifier.replace(':', '-') + '.mp3',
        guid='urn:episode:' + identifier, completed=False, position=0, total=600,
        published_at=BASE - 1000)
    result.update(changes)
    return result


class NativeSource:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []
        self.offline = False
        self.last_status = {'ok': True}

    def capture(self, authorization):
        self.calls.append(authorization)
        if self.offline:
            raise ValueError('native_source_unavailable')
        return copy.deepcopy(self.rows)


class Upstream:
    def __init__(self):
        self.rows = []
        self.calls = []
        self.cursor = BASE + 30

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        data = dict(actions=self.rows, timestamp=self.cursor)
        return type('Response', (), dict(status_code=200, content=b'{}',
            json=lambda self: copy.deepcopy(data)))()


class ExactResolver:
    def __init__(self):
        self.calls = []

    def resolve(self, event):
        self.calls.append(copy.deepcopy(event))
        return dict(event, url=event['guid'])


class FeedServer:
    """Exercise the production RSS parser/resolver with changing feed contents."""
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []
        self.offline = False

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url))
        if self.offline:
            raise requests.Timeout('unavailable')
        items = []
        for row in self.rows:
            items.append('<item><title>Same title is not identity evidence</title>'
                '<guid>' + html.escape(row['guid']) + '</guid><enclosure url="' +
                html.escape(row['episode'], quote=True) + '"/></item>')
        body = ('<rss><channel>' + ''.join(items) + '</channel></rss>').encode()
        return type('Response', (), dict(status_code=200, headers={},
            iter_content=lambda self, size: iter([body]), close=lambda self: None))()


class NativeRelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = BASE + 100
        self.upstream = Upstream()
        self.source = NativeSource([native()])
        self.resolver = ExactResolver()
        self.config = dict(upstream='https://pine.example.test', username='user',
            baseline=BASE, exclude_devices=['book-progress-sync', 'book-progress-sync-test'],
            native_source=dict(api_key_file='/unused-injected-provider', user_id=2,
                               server_version='0.9.0'))
        self.relay = self.make_relay()

    def make_relay(self, config=None, resolver=None):
        return Relay(config or self.config, self.tmp.name, self.upstream,
            source=self.source, resolver=resolver or self.resolver, clock=lambda: self.now)

    def poll(self, since=None):
        return self.relay.actions(since, '', 'Basic injected-existing-credentials')

    def advance(self, seconds=1):
        self.now += seconds

    def complete(self, **changes):
        self.source.rows = [native(completed=True, **changes)]

    def test_initial_sixteen_thousand_completed_rows_export_nothing(self):
        self.source.rows = [native(str(i), completed=True, position=600) for i in range(16000)]
        baseline = self.poll()
        self.assertEqual(baseline['actions'], [])
        self.assertEqual(self.resolver.calls, [])
        self.advance()
        self.assertEqual(self.poll(baseline['timestamp'])['actions'], [])
        status = self.relay.status()
        self.assertEqual(status['baseline_episodes'], 16000)
        self.assertEqual(status['projected_events'], 0)

    def test_verified_recovery_is_once_even_after_full_resync_and_restart(self):
        self.config['native_source']['recovery_ids'] = [native()['id']]
        self.relay = self.make_relay()
        self.complete(position=300)
        first = self.poll()
        self.assertEqual(len(first['actions']), 1)
        self.assertEqual(first['actions'][0]['position'], 600)
        self.assertEqual(self.poll(None), first)
        self.advance()
        second = self.poll(first['timestamp'])
        self.assertEqual(second['actions'], [])
        self.relay = self.make_relay()
        self.assertEqual(self.poll(None), second)
        self.advance()
        self.assertEqual(self.poll(second['timestamp'])['actions'], [])
        self.assertEqual(self.relay.status()['projected_events'], 1)

    def test_native_completed_without_gpodder_action_exports_actual_end_once(self):
        baseline = self.poll()
        self.complete(position=450)
        self.advance()
        completed = self.poll(baseline['timestamp'])
        self.assertEqual(len(completed['actions']), 1)
        action = completed['actions'][0]
        self.assertEqual(action['position'], 600)
        self.assertEqual(action['total'], 600)
        self.assertEqual(action['url'], native()['guid'])
        self.assertEqual(action['episode'], native()['episode'])
        self.assertFalse(any(key.startswith('_tracking_') for key in action))
        self.assertEqual(self.upstream.calls, [])
        self.advance()
        self.assertEqual(self.poll(completed['timestamp'])['actions'], [])

    def test_sync_receipt_log_noise_cannot_create_or_duplicate_a_listen(self):
        baseline = self.poll()
        for device in ('book-progress-sync', 'book-progress-sync-test', 'phone'):
            self.upstream.rows.append(dict(podcast=FEED, episode=native()['episode'],
                timestamp=datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                device=device, action='play', position=600, total=600))
        self.advance()
        unchanged = self.poll(baseline['timestamp'])
        self.assertEqual(unchanged['actions'], [])
        self.complete()
        self.advance()
        completed = self.poll(unchanged['timestamp'])
        self.assertEqual(len(completed['actions']), 1)
        self.upstream.rows[0]['timestamp'] = datetime.fromtimestamp(self.now + 100, timezone.utc).isoformat()
        self.advance()
        self.assertEqual(self.poll(completed['timestamp'])['actions'], [])
        self.assertEqual(self.relay.status()['projected_events'], 1)
        self.assertEqual(self.upstream.calls, [])

    def test_pending_payload_is_immutable_across_retries_full_resync_and_restart(self):
        baseline = self.poll()
        self.complete(updated_at=self.now)
        self.advance()
        first = self.poll(baseline['timestamp'])
        calls = len(self.source.calls)
        self.advance(86400)
        self.source.rows = [native()]
        self.assertEqual(self.poll(None), first)
        self.assertEqual(self.poll(baseline['timestamp']), first)
        self.assertEqual(len(self.source.calls), calls)
        self.relay = self.make_relay()
        self.assertEqual(self.poll(None), first)
        self.assertEqual(len(self.source.calls), calls)

    def test_acknowledgement_survives_outage_and_prevents_resending_after_restart(self):
        baseline = self.poll()
        self.complete()
        self.advance()
        first = self.poll(baseline['timestamp'])
        self.source.offline = True
        self.advance()
        with self.assertRaisesRegex(ValueError, 'native_source_unavailable'):
            self.poll(first['timestamp'])
        self.assertEqual(self.relay.status()['acknowledged'], first['timestamp'])
        self.assertEqual(self.relay.status()['outstanding_events'], 0)
        self.relay = self.make_relay()
        self.source.offline = False
        self.advance()
        self.assertEqual(self.poll(first['timestamp'])['actions'], [])
        self.assertEqual(self.relay.status()['projected_events'], 1)

    def test_blocked_exact_rss_identity_does_not_discard_event_or_block_ready_event(self):
        missing, ready = native('missing'), native('ready')
        self.source.rows = [missing, ready]
        server = FeedServer([ready])
        self.relay = self.make_relay(resolver=TrackingResolver(request=server))
        baseline = self.poll()
        self.source.rows = [{**row, 'completed': True} for row in self.source.rows]
        self.advance()
        first = self.poll(baseline['timestamp'])
        self.assertEqual([action['guid'] for action in first['actions']], [ready['guid']])
        self.assertEqual(self.relay.status()['blocked'], 1)
        self.assertEqual(self.relay.status()['outstanding_events'], 2)
        with self.relay.db() as db:
            from tracking_projection import NativeProjection
            blocked_event = next(e for e in NativeProjection.events(db)
                                 if e['_tracking_episode_id'] == missing['id'])
            original_time = blocked_event['timestamp']
        self.advance(100)
        empty = self.poll(first['timestamp'])
        self.assertEqual(empty['actions'], [])
        self.assertEqual(self.relay.status()['outstanding_events'], 1)
        server.rows = [missing, ready]
        self.advance(100)
        recovered = self.poll(empty['timestamp'])
        self.assertEqual([action['guid'] for action in recovered['actions']], [missing['guid']])
        self.assertEqual(recovered['actions'][0]['timestamp'], original_time)
        self.advance()
        self.assertEqual(self.poll(recovered['timestamp'])['actions'], [])
        self.assertEqual(self.relay.status()['blocked'], 0)
        self.assertEqual(self.relay.status()['outstanding_events'], 0)

    def test_rss_cache_refreshes_for_tomorrows_new_episode_without_restart(self):
        first_row = native('today')
        self.source.rows = [first_row]
        server = FeedServer([first_row])
        self.relay = self.make_relay(resolver=TrackingResolver(request=server))
        baseline = self.poll()
        self.source.rows = [{**first_row, 'completed': True}]
        self.advance()
        first = self.poll(baseline['timestamp'])
        self.assertEqual(len(first['actions']), 1)
        tomorrow = native('tomorrow', completed=True, published_at=self.now + 86400)
        self.source.rows.append(tomorrow)
        server.rows.append(tomorrow)
        self.advance(86400)
        second = self.poll(first['timestamp'])
        self.assertEqual([a['guid'] for a in second['actions']], [tomorrow['guid']])
        self.assertEqual(len(server.calls), 2)

    def test_blocked_old_enclosure_recovers_from_fresh_same_native_identity(self):
        original = native()
        server = FeedServer([])
        self.relay = self.make_relay(resolver=TrackingResolver(request=server))
        baseline = self.poll()
        self.complete()
        self.advance()
        blocked = self.poll(baseline['timestamp'])
        self.assertEqual(blocked['actions'], [])
        self.assertEqual(self.relay.status()['blocked'], 1)
        with self.relay.db() as db:
            from tracking_projection import NativeProjection
            before = NativeProjection.events(db)[0]
        rotated = native(completed=True, total=700, position=700,
                         episode='https://media.example.test/refreshed-enclosure.mp3')
        self.source.rows = [rotated]
        server.rows = [rotated]
        self.advance(1000)
        recovered = self.poll(blocked['timestamp'])
        self.assertEqual(len(recovered['actions']), 1)
        wire = recovered['actions'][0]
        self.assertEqual(wire['episode'], rotated['episode'])
        self.assertEqual(wire['guid'], original['guid'])
        self.assertEqual(wire['url'], original['guid'])
        self.assertEqual(wire['timestamp'], before['timestamp'])
        self.assertEqual(wire['position'], before['position'])
        self.assertEqual(wire['total'], before['total'])
        with self.relay.db() as db:
            self.assertEqual(NativeProjection.events(db), [before])
        self.assertEqual(before['episode'], original['episode'])
        self.assertEqual(self.relay.status()['projected_events'], 1)
        self.advance()
        self.assertEqual(self.poll(recovered['timestamp'])['actions'], [])
        self.assertEqual(self.relay.status()['outstanding_events'], 0)

    def test_missing_or_deferred_native_identity_keeps_event_without_unknown_or_ack(self):
        original = native()
        server = FeedServer([])
        self.relay = self.make_relay(resolver=TrackingResolver(request=server))
        baseline = self.poll()
        self.complete()
        self.advance()
        blocked = self.poll(baseline['timestamp'])
        self.assertEqual(self.relay.status()['outstanding_events'], 1)
        self.source.rows = []  # Such as a source row deferred for missing duration.
        server.rows = [original]
        calls_before = len(server.calls)
        self.advance()
        absent = self.poll(blocked['timestamp'])
        self.assertEqual(absent['actions'], [])
        self.assertEqual(len(server.calls), calls_before)
        self.assertEqual(self.relay.status()['seen'], 0)
        self.assertEqual(self.relay.status()['blocked'], 1)
        self.assertEqual(self.relay.status()['outstanding_events'], 1)
        self.complete()
        self.advance(1000)
        resumed = self.poll(absent['timestamp'])
        self.assertEqual(len(resumed['actions']), 1)
        self.assertEqual(resumed['actions'][0]['episode'], original['episode'])
        self.assertEqual(self.relay.status()['projected_events'], 1)
        self.advance()
        self.assertEqual(self.poll(resumed['timestamp'])['actions'], [])
        self.assertEqual(self.relay.status()['outstanding_events'], 0)
        self.assertEqual(self.relay.status()['blocked'], 0)

    def test_feed_change_on_same_native_id_defers_without_reassigning_history(self):
        original = native()
        server = FeedServer([])
        self.relay = self.make_relay(resolver=TrackingResolver(request=server))
        baseline = self.poll()
        self.complete()
        self.advance()
        blocked = self.poll(baseline['timestamp'])
        changed = native(completed=True, podcast='https://changed-feed.example.test/feed')
        self.source.rows = [changed]
        server.rows = [changed]
        calls_before = len(server.calls)
        self.advance()
        deferred = self.poll(blocked['timestamp'])
        self.assertEqual(deferred['actions'], [])
        self.assertEqual(len(server.calls), calls_before)
        self.assertEqual(self.relay.status()['seen'], 0)
        self.assertEqual(self.relay.status()['blocked'], 1)
        self.assertEqual(self.relay.status()['outstanding_events'], 1)
        self.complete()
        server.rows = [original]
        self.advance()
        resumed = self.poll(deferred['timestamp'])
        self.assertEqual(len(resumed['actions']), 1)
        self.assertEqual(resumed['actions'][0]['podcast'], original['podcast'])
        self.assertEqual(self.relay.status()['projected_events'], 1)

    def test_completed_duration_and_enclosure_refresh_do_not_manufacture_a_second_play(self):
        baseline = self.poll()
        self.complete()
        self.advance()
        first = self.poll(baseline['timestamp'])
        self.complete(position=900, total=700, episode='https://media.example.test/rotated.mp3')
        self.advance(86400)
        after = self.poll(first['timestamp'])
        self.assertEqual(after['actions'], [])
        self.assertEqual(self.relay.status()['projected_events'], 1)

    def test_native_unfinished_near_end_remains_unfinished_in_stock_floppy(self):
        baseline = self.poll()
        self.source.rows = [native(position=599)]
        self.advance()
        result = self.poll(baseline['timestamp'])
        self.assertEqual(len(result['actions']), 1)
        wire = result['actions'][0]
        # The stock importer considers the final five seconds completed. Its
        # tracking projection must not contradict the native completion flag.
        self.assertGreater(int(wire['position']), 0)
        self.assertLess(int(wire['position']), int(wire['total']) - 5)
        with self.relay.db() as db:
            from tracking_projection import NativeProjection
            recorded = NativeProjection.events(db)[0]
        self.assertEqual(recorded['position'], 599)
        self.assertFalse(recorded['_tracking_completed'])

    def test_short_unrepresentable_progress_is_superseded_by_actual_completion(self):
        self.source.rows = [native(total=5)]
        baseline = self.poll()
        self.source.rows = [native(total=5, position=4)]
        self.advance()
        blocked = self.poll(baseline['timestamp'])
        self.assertEqual(blocked['actions'], [])
        self.assertEqual(self.relay.status()['blocked'], 1)
        self.assertEqual(self.relay.status()['outstanding_events'], 1)
        self.source.rows = [native(total=5, position=4, completed=True)]
        self.advance(5)
        completed = self.poll(blocked['timestamp'])
        self.assertEqual(len(completed['actions']), 1)
        self.assertEqual(completed['actions'][0]['position'], 5)
        self.assertEqual(completed['actions'][0]['total'], 5)
        self.assertEqual(self.relay.status()['blocked'], 0)
        self.assertEqual(self.relay.status()['superseded_events'], 1)
        self.advance()
        self.assertEqual(self.poll(completed['timestamp'])['actions'], [])
        self.assertEqual(self.relay.status()['outstanding_events'], 0)
        self.assertEqual(self.relay.status()['projected_events'], 2)
        self.assertEqual(self.relay.status()['superseded_events'], 1)

    def test_undelivered_completed_generation_is_preserved_after_reset_and_rehear(self):
        row = native()
        server = FeedServer([])
        self.relay = self.make_relay(resolver=TrackingResolver(request=server))
        baseline = self.poll()
        self.complete()
        self.advance()
        blocked = self.poll(baseline['timestamp'])
        self.assertEqual(blocked['actions'], [])
        self.assertEqual(self.relay.status()['blocked'], 1)
        self.source.rows = [row]
        self.advance(600)
        reset = self.poll(blocked['timestamp'])
        self.assertEqual(reset['actions'], [])
        self.complete()
        server.rows = [row]
        self.advance(600)
        completed = self.poll(reset['timestamp'])
        self.assertEqual(len(completed['actions']), 2)
        self.assertTrue(all(action['position'] == action['total'] for action in completed['actions']))
        self.assertNotEqual(completed['actions'][0]['timestamp'], completed['actions'][1]['timestamp'])
        self.assertEqual(self.relay.status()['superseded_events'], 0)
        self.advance()
        self.assertEqual(self.poll(completed['timestamp'])['actions'], [])
        self.assertEqual(self.relay.status()['outstanding_events'], 0)
        self.assertEqual(self.relay.status()['projected_events'], 2)

    def test_native_completion_with_stale_history_time_is_delivered_as_a_new_edge(self):
        old_time = BASE - 1000
        self.source.rows = [native(position=100, updated_at=old_time)]
        baseline = self.poll()
        self.source.rows = [native(completed=True, position=100, updated_at=old_time)]
        self.advance()
        event = self.poll(baseline['timestamp'])
        self.assertEqual(len(event['actions']), 1)
        stamp = datetime.fromisoformat(event['actions'][0]['timestamp'].replace('Z', '+00:00')).timestamp()
        self.assertEqual(stamp, self.now)
        self.advance(1000)
        self.assertEqual(self.poll(None), event)
        self.assertEqual(self.poll(event['timestamp'])['actions'], [])

    def test_new_subscription_with_old_completed_catalog_does_not_flood_history(self):
        baseline = self.poll()
        self.source.rows.extend(native(str(i), podcast='https://new-show.example.test/feed',
            completed=True, position=600, published_at=BASE - 1000, updated_at=BASE - 500)
            for i in range(16000))
        self.advance()
        self.assertEqual(self.poll(baseline['timestamp'])['actions'], [])
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self.relay.status()['projected_events'], 0)

    def test_legacy_pending_batch_is_drained_before_native_baseline(self):
        legacy_config = {key: value for key, value in self.config.items() if key != 'native_source'}
        self.upstream.rows = [dict(podcast=FEED, episode=native()['episode'],
            timestamp=datetime.fromtimestamp(BASE + 10, timezone.utc).isoformat(),
            device='phone', action='play', position=100, total=600)]
        # A legacy process does not receive an injected native provider.
        legacy = Relay(legacy_config, self.tmp.name, self.upstream)
        pending = legacy.actions(None, '', 'Basic existing')
        self.assertEqual(len(pending['actions']), 1)
        self.relay = self.make_relay()
        self.assertEqual(self.poll(None), pending)
        self.assertEqual(self.source.calls, [])
        self.advance()
        baseline = self.poll(pending['timestamp'])
        self.assertEqual(baseline['actions'], [])
        self.assertEqual(len(self.source.calls), 1)
        self.assertEqual(self.relay.status()['projected_events'], 0)

    def test_failed_native_snapshot_keeps_projection_and_retryable_transition(self):
        baseline = self.poll()
        self.complete()
        self.source.rows.append(native('broken', position=-1))
        self.advance()
        with self.assertRaises(ValueError):
            self.poll(baseline['timestamp'])
        self.assertEqual(self.relay.status()['projected_events'], 0)
        self.assertEqual(self.relay.status()['acknowledged'], baseline['timestamp'])
        self.source.rows.pop()
        self.advance()
        self.assertEqual(len(self.poll(baseline['timestamp'])['actions']), 1)


if __name__ == '__main__':
    unittest.main()
