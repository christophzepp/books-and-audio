"""A finished worker cycle must release its bulk observations before the next cycle."""
import gc
import sqlite3
import types
import unittest
import weakref

from gpodder_observations import ActionHistory
from pinepods_client import ActionBatchTooLarge, PinepodsClient, RequestError
from podcast_sync import PodcastCoordinator
from sync import Bridge


class Payload:
    pass


class ObservationLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.pine = PinepodsClient('https://pine.example', 'reader', 'test', api_key='test', server_version='0.9.0')
        self.addCleanup(self.pine.close)
        self.pine._ensure_native = lambda: 2
        self.pine.actions = lambda **filters: {'timestamp': 100, 'actions': []}
        self.bridge = Bridge.__new__(Bridge)
        self.bridge.config = {}
        self.bridge.clock = lambda: 100
        self.bridge.db = sqlite3.connect(':memory:')
        self.addCleanup(self.bridge.db.close)
        self.feed = 'https://example.org/feed'
        if gc.isenabled():
            gc.disable()
            self.addCleanup(gc.enable)

    def test_end_of_book_and_podcast_cycles_releases_indexes_even_on_failure(self):
        coordinator = PodcastCoordinator.__new__(PodcastCoordinator)
        coordinator.b = self.bridge
        for worker, attribute in ((self.bridge, '_book_observations'), (coordinator, 'observation')):
            for fail in (False, True):
                with self.subTest(worker=type(worker).__name__, fail=fail):
                    refs = []
                    def collect_inventory(instance, apply, settle):
                        batch = getattr(instance, attribute)
                        snapshot = batch.client(self.pine)
                        payload = Payload()
                        snapshot._observation_cache['large reply'] = payload
                        refs.extend((weakref.ref(snapshot), weakref.ref(payload)))
                        if batch.persist_actions:
                            snapshot._feed_history(self.feed)
                            refs.append(weakref.ref(snapshot._action_store))
                        if fail:
                            raise RuntimeError('cycle interrupted')
                        return 123
                    worker._cycle = types.MethodType(collect_inventory, worker)
                    for _ in range(3):
                        if fail:
                            with self.assertRaisesRegex(RuntimeError, 'cycle interrupted'):
                                worker.cycle(apply=True)
                        else:
                            self.assertEqual(worker.cycle(apply=True), 123)
                        self.assertTrue(all(ref() is None for ref in refs))
                        self.assertIsNone(getattr(worker, attribute))

    def test_repeated_delta_and_feed_errors_release_failed_responses_and_keep_http_status(self):
        for delta in (False, True):
            with self.subTest(delta=delta):
                refs = []
                def fail(**filters):
                    result = Payload()
                    refs.append(weakref.ref(result))
                    raise RequestError('transport error', status=503) if delta else ActionBatchTooLarge('action limit')
                history = ActionHistory(self.bridge, self.pine, fail)
                if delta:
                    with history.db:
                        history._set_state(99, '')
                    history.state = history._state()
                want = RequestError if delta else ActionBatchTooLarge
                for _ in range(20):
                    try:
                        history.read(self.feed)
                    except want as exc:
                        self.assertEqual(exc.status, 503 if delta else None)
                    else:
                        self.fail('failed history was accepted')
                    self.assertEqual(len(refs), 1)
                    self.assertIsNone(refs[0]())
