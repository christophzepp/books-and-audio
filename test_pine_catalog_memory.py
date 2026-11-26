"""Historical PinePods catalogs may dwarf the downloaded library."""
from collections import Counter
import tracemalloc
import unittest

from observation_snapshot import PineObservation
from pinepods_client import EpisodeUnavailable, PinepodsClient, RequestError
from test_podcast_bulk_io import FEED, URL, catalog_row, metadata, pod


class CatalogMemoryTests(unittest.TestCase):
    def setUp(self):
        self.count = 1600
        self.calls = Counter()
        self.client = PinepodsClient('https://pinepods.example', 'reader', 'private',
                                   api_key='private', server_version='0.9.0')
        self.addCleanup(self.client.close)
        self.client._request = self.request
        self.rows = {}
        self.snapshot = PineObservation(self.client)
        self.snapshot.plan_catalog([{'enclosure_url': URL + '1'}, {'enclosure_url': URL + '2'}])

    def row(self, i):
        # Allocate a separate description per row, as the real JSON decoder does.
        return catalog_row(i, Episodedescription=f'{i}:' + 'a' * 8192) | self.rows.get(i, {})

    def request(self, method, path, **kwargs):
        self.calls[path] += 1
        if path == '/api/data/get_user':
            return {'status': 'success', 'retrieved_id': 2}
        if path == '/api/data/user_details_id/2':
            return {'UserID': 2, 'Username': 'reader'}
        if path == '/api/data/return_pods/2':
            return {'pods': [pod()]}
        if path == '/api/data/podcast_episodes':
            offset = kwargs['params']['offset']
            return {'total': self.count, 'episodes': [
                self.row(i) for i in range(offset + 1, min(self.count, offset + 200) + 1)]}
        if path == '/api/data/get_episode_metadata':
            return {'episode': metadata(self.row(kwargs['payload']['episode_id']))}
        raise AssertionError(path)

    def inspect(self, i):
        return self.snapshot.inspect_episode_metadata(FEED, URL + str(i), 600)

    def test_pages_are_released_while_all_historical_rows_are_validated(self):
        self.rows[73] = {'Episodeurl': ''}
        tracemalloc.start()
        try:
            self.assertEqual(self.inspect(1)['metadata'], metadata(self.row(1)))
            self.assertEqual(self.inspect(2)['metadata'], metadata(self.row(2)))
            current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(current, 1024 ** 2)
        self.assertLess(peak, 5 * 1024 ** 2)
        self.assertEqual(self.calls['/api/data/podcast_episodes'], 8)
        self.assertEqual(self.calls['/api/data/get_episode_metadata'], 1)
        _, rows = self.snapshot._catalog_data[FEED, 2]
        self.assertEqual(set(rows), {1, 2, 73})
        self.assertNotIn('Episodedescription', rows[73])
        self.assertEqual(rows[73]['Episodetitle'], 'Episode 73')

    def test_unplanned_alias_is_validated_once_and_late_duplicates_block_it(self):
        self.inspect(1)
        self.assertEqual(self.inspect(1600)['episode_id'], 1600)
        self.assertEqual(self.inspect(1600)['metadata'], metadata(self.row(1600)))
        self.assertEqual(self.calls['/api/data/podcast_episodes'], 16)
        self.rows[1599] = {'Episodeurl': URL + '99'}
        with self.assertRaises(EpisodeUnavailable) as error:
            self.inspect(99)
        self.assertEqual(error.exception.reason, 'episode_ambiguous')
        self.assertEqual(self.calls['/api/data/get_episode_metadata'], 1)

    def test_invalid_historical_row_blocks_observation_and_live_reads(self):
        self.rows[self.count] = {'Episodeid': 599}
        for client in (self.snapshot, self.client):
            with self.subTest(client=type(client).__name__), self.assertRaises(RequestError):
                client.inspect_episode_metadata(FEED, URL + '1', 600)
        self.assertFalse(self.snapshot._catalog_data)
        self.assertFalse(self.snapshot._catalogs)
        self.assertEqual(self.calls['/api/data/get_episode_metadata'], 0)

    def test_second_feed_and_failed_projection_use_only_its_validated_metadata(self):
        second = 'https://feed.example/moved'
        moved = URL + 'reviewed'
        def moved_api(method, path, **kwargs):
            if path == '/api/data/return_pods/2':
                return {'pods': [pod(), dict(pod(), podcastid=12, feedurl=second)]}
            if path == '/api/data/podcast_episodes' and kwargs['params']['podcast_id'] == 12:
                return {'total': 2, 'episodes': [self.row(1), dict(self.row(73), Episodeurl=moved)]}
            if path == '/api/data/get_episode_metadata':
                ident = kwargs['payload']['episode_id']
                self.calls[path] += 1
                values = metadata(self.row(ident))
                values.update(podcastid=12, feedurl=second, extra='individual metadata')
                if ident == 73:
                    values['episodeurl'] = moved
                return {'episode': values}
            return self.request(method, path, **kwargs)
        self.snapshot._observation_request = moved_api
        proof = self.snapshot.inspect_episode_metadata(second, moved, 600)
        self.assertEqual((proof['episode_id'], proof['podcast_id'], proof['metadata']['extra']),
                         (73, 12, 'individual metadata'))
        proof = self.snapshot.inspect_episode_metadata(second, URL+'1', 600)
        self.assertEqual(proof['metadata']['extra'], 'individual metadata')
        self.assertEqual(self.calls['/api/data/get_episode_metadata'], 2)
        _, rows = self.snapshot._catalog_data[second, 2]
        self.assertNotIn('Episodedescription', rows[1])
        self.assertEqual(rows[73]['Episodeurl'], moved)
        self.assertFalse(any('get_episode_metadata' in key for key in self.snapshot._observation_cache))
        with self.assertRaises(EpisodeUnavailable) as failure:
            self.snapshot.inspect_episode_metadata(FEED, moved, 600)
        self.assertEqual(failure.exception.reason, 'episode_missing')


if __name__ == '__main__':
    unittest.main()
