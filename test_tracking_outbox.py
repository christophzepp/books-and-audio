import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gpodder_relay import Relay, handler
from test_native_tracking_relay import BASE, ExactResolver, NativeSource, Upstream, native
from tracking_outbox import BATCH_LIMIT, candidates


class NativeOutboxTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.config = dict(upstream='https://pine.example.test', username='reader', baseline=BASE,
                           native_source={'user_id': 2})
        self.now = BASE + 1
        self.source = NativeSource([])
        self.upstream = Upstream()
        self.resolver = ExactResolver()
        self.restart()

    def restart(self):
        self.relay = Relay(self.config, Path(self.root.name), self.upstream, source=self.source,
                           resolver=self.resolver, clock=lambda: self.now)

    def poll(self, since=None):
        self.now += 1
        return self.relay.actions(since, '', 'Basic test')

    @staticmethod
    def append(db, ident, episode=None, sequence=1, completed=True):
        eid = episode or ident
        payload = {'_tracking_event_id': ident, '_tracking_episode_id': eid,
                   '_tracking_generation': 0, '_tracking_completed': completed}
        db.execute('INSERT INTO native_tracking_events VALUES(?,?,?,?,?,?,?,?,?)',
                   (ident, 'play', eid, 0, sequence, 'progress', BASE, '{}', json.dumps(payload)))

    @staticmethod
    def old_schema(db):
        for trigger in ('track_new_native_play', 'track_native_confirmation', 'track_native_supersession'):
            db.execute('DROP TRIGGER '+trigger)
        for table in ('native_outbox', 'native_floppy_polls', 'native_outbox_stats'):
            db.execute('DROP TABLE '+table)

    def test_beta17_migration_preserves_pending_history_superseded_and_old_confirmations(self):
        self.source.rows = [native('2:3:4')]
        baseline = self.poll()
        self.source.rows[0]['completed'] = True
        packet = self.poll(baseline['timestamp'])
        with self.relay.db() as db:
            self.old_schema(db)
            for key in ('old-ack', 'unresolved', 'old-intermediate'):
                self.append(db, key)
            db.execute("INSERT INTO seen VALUES('native:old-ack')")
            db.execute("INSERT INTO native_tracking_superseded VALUES('old-intermediate','old-ack')")
            self.relay.set(db, 'native_blocked', ['unresolved'])
            pending = self.relay.get(db, 'pending')
            ack = self.relay.get(db, 'ack')
            n = db.execute('SELECT count(*) FROM native_tracking_events').fetchone()[0]
        for _ in range(2):
            self.restart()
            self.assertEqual(self.poll(), packet)
            with self.relay.db() as db:
                self.assertEqual(self.relay.get(db, 'pending'), pending)
                self.assertEqual(self.relay.get(db, 'ack'), ack)
                self.assertEqual(db.execute('SELECT count(*) FROM native_tracking_events').fetchone()[0], n)
                self.assertEqual(set(db.execute('SELECT event_id,reason,first_attempt_cursor FROM native_outbox')),
                                 {('unresolved','legacy_blocked',None),
                                  (pending['fingerprints'][0][7:],None,packet['timestamp'])})
                self.assertEqual(db.execute('SELECT COUNT(*) FROM native_floppy_polls').fetchone()[0], 0)

    def test_raw_old_binary_writes_and_rollbacks_maintain_outbox(self):
        with self.relay.db() as db:
            self.append(db, 'old-unfinished', 'same', 1, False)
            self.append(db, 'old-finished', 'same', 2)
            self.assertEqual([(e['_tracking_event_id'], latest) for e, latest in candidates(db)],
                             [('old-unfinished','old-finished'), ('old-finished','old-finished')])
            db.execute("INSERT INTO native_tracking_superseded VALUES('old-unfinished','old-finished')")
            db.execute('SAVEPOINT old_ack')
            db.execute("INSERT INTO seen VALUES('native:old-finished')")
            self.assertEqual(list(candidates(db)), [])
            db.execute('ROLLBACK TO old_ack')
            self.assertEqual(len(list(candidates(db))), 1)
            db.execute('RELEASE old_ack')
        self.restart()
        self.assertEqual(self.relay.status()['projected_events'], 2)
        self.assertEqual(self.relay.status()['outstanding_events'], 1)

    def test_historical_payloads_never_visited_and_work_is_bounded(self):
        with self.relay.db() as db:
            for i in range(10000):
                key = f'old-{i}'
                self.append(db, key)
                db.execute('INSERT INTO seen VALUES(?)', ('native:'+key,))
            self.append(db, 'current')
            steps = 0
            def count():
                nonlocal steps
                steps += 1
                return int(steps > 600)
            db.set_progress_handler(count, 1)
            self.assertEqual([e['_tracking_event_id'] for e, successor in candidates(db)], ['current'])
            db.set_progress_handler(None, 0)
            self.assertLess(steps, 600)
        self.assertEqual(self.relay.status()['outstanding_events'], 1)
        self.assertEqual(self.relay.status()['projected_events'], 10001)

    def test_new_and_retry_queues_share_bounded_poll(self):
        with self.relay.db() as db:
            for i in range(BATCH_LIMIT*3):
                key = str(i)
                self.append(db, key)
                if i < BATCH_LIMIT:
                    db.execute('UPDATE native_outbox SET last_attempt_cursor=10 WHERE event_id=?', (key,))
            selected = [int(e['_tracking_event_id']) for e,last in candidates(db)]
            self.assertEqual(len(selected), BATCH_LIMIT)
            self.assertEqual(sum(i < BATCH_LIMIT for i in selected), BATCH_LIMIT//2)
            db.executemany('UPDATE native_outbox SET last_attempt_cursor=20 WHERE event_id=?',
                           ((str(i),) for i in selected))
            next_batch = [int(e['_tracking_event_id']) for e,last in candidates(db)]
            self.assertTrue(set(selected).isdisjoint(next_batch))

    def test_blockers_are_scoped_redacted_and_last_attempt_is_durable(self):
        self.source.rows = [native('2:3:4')]
        baseline = self.poll()
        self.source.rows[0]['completed'] = True
        with patch.object(self.resolver, 'resolve', side_effect=ValueError('tracking_rss_identity_unresolved')):
            first = self.poll(baseline['timestamp'])
        self.restart()
        with self.relay.db() as db:
            self.assertEqual(db.execute('SELECT reason,first_attempt_cursor,last_attempt_at FROM native_outbox').fetchone(),
                             ('tracking_rss_identity_unresolved',first['timestamp'], first['timestamp']))
        with patch.object(self.resolver, 'resolve', side_effect=ValueError('a private token or URL')):
            second = self.poll(first['timestamp'])
        with self.relay.db() as db:
            self.assertEqual(db.execute('SELECT reason,first_attempt_cursor,last_attempt_at FROM native_outbox').fetchone(),
                             ('resolver_unavailable',first['timestamp'], second['timestamp']))
        issued = self.poll(second['timestamp'])
        self.assertEqual(len(issued['actions']), 1)
        with self.relay.db() as db:
            self.assertIsNone(db.execute('SELECT reason FROM native_outbox').fetchone()[0])
        self.poll(issued['timestamp'])
        self.assertEqual(self.relay.status()['outstanding_events'], 0)

    def test_only_completed_stock_http_response_records_a_distinct_floppy_poll(self):
        ua = 'Floppy/1.0 (https://github.com/dannyvfilms/Floppy)'
        auth = base64.b64encode(b'reader:fixture').decode()
        episode_path = '/api/2/episodes/reader.json'
        def get(path, user_agent=ua, authorization=auth, broken=False):
            request = (f'GET {path} HTTP/1.1\r\nHost: relay\r\nAuthorization: Basic {authorization}\r\n'
                       f'User-Agent: {user_agent}\r\nConnection: close\r\n\r\n').encode()
            stream = io.BytesIO()
            class Socket:
                def makefile(self, mode, size): return io.BytesIO(request)
                def sendall(self, data):
                    if broken:
                        raise BrokenPipeError()
                    stream.write(data)
            handler(self.relay)(Socket(), ('127.0.0.1', 1234), None)
            if broken:
                self.assertFalse(stream.getvalue())
                return
            head, body = stream.getvalue().split(b'\r\n\r\n',1)
            return head.splitlines()[0], json.loads(body)
        for path in ('/health', '/status'):
            self.assertIn(b'200', get(path)[0])
        self.assertIn(b'401', get(episode_path,authorization='bad')[0])
        get(episode_path, user_agent='requests/9.3')
        get(episode_path, broken=True)
        with self.relay.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM native_floppy_polls').fetchone()[0], 0)
        first = get(episode_path)[1]['timestamp']
        get(episode_path); get(episode_path)
        self.now += 15
        second = get(episode_path+'?since='+str(first))[1]['timestamp']
        self.restart()
        self.assertIn(b'503', get(episode_path+'?since='+str(second+100))[0])
        with self.relay.db() as db:
            self.assertEqual(list(db.execute('SELECT cursor FROM native_floppy_polls ORDER BY id')),[(first,), (second,)])


if __name__ == '__main__':
    unittest.main()
