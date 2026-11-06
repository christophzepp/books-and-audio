import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from pine_metadata_repair import PineMetadataRepair, NAMESPACE, same_except_duration
from podcast_sync import build_mapping
import pine_media_probe


FEED = 'https://feed.example/podcast.xml?private=not-for-logs'
URL = 'https://media.example/episode.mp3?private=not-for-logs'


def evidence(duration=600.25):
    value = dict(known=True, duration_seconds=duration, full_stream_verified=False,
                 header=dict(known=True, duration_seconds=duration, source='Xing_frame_count'),
                 fetch=dict(bytes_read=65536), source_url_sha256=hashlib.sha256(URL.encode()).hexdigest(),
                 prefix_sha256='f' * 64)
    value['evidence_sha256'] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return value


def full_evidence(duration=600.25):
    value = dict(known=True, duration_seconds=duration, full_stream_verified=False,
                 header=dict(known=True, duration_seconds=duration, source='full_mp3_packets',
                             packet_count=25000, full_demux_completed=True,
                             duration_extrapolated=False, full_decode_checked=False),
                 fetch=dict(bytes_read=8000000, actual_bytes=8000000, response_total_bytes=8000000,
                            full_download=True, complete_body_read=True,
                            fetched_at='1970-01-01T00:16:40+00:00'),
                 source_url_sha256=hashlib.sha256(URL.encode()).hexdigest(), full_audio_sha256='f' * 64)
    value['evidence_sha256'] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return value


class Pine:
    def __init__(self):
        self.value = dict(episode_id=7, user_id=2, duration=480, position=None, completed=False,
                          downloaded=False, podcast_id=11, expected_duration=600.25,
                          metadata=dict(episodeid=7, podcastid=11, feedurl=FEED, episodeurl=URL,
                                        is_youtube=False, is_downloaded=False, completed=False,
                                        episodeduration=480, listenduration=None))
        self.posts = []
        self.reads = 0
        self.after_write = None

    def inspect_episode_metadata(self, *args):
        self.reads += 1
        return copy.deepcopy(self.value)

    def write_episode_duration(self, before, duration):
        assert before == self.value
        self.posts.append(duration)
        self.value['duration'] = duration
        self.value['metadata']['episodeduration'] = duration
        if self.after_write:
            self.after_write(self.value)
        return {'accepted': True}


class Bridge:
    def __init__(self, root):
        self.root = root
        self.time = 1000
        self.clock = lambda: self.time
        self.db = sqlite3.connect(root / 'state.sqlite')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE state(id TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE operations(id TEXT PRIMARY KEY,book TEXT,value TEXT,status TEXT);
            CREATE TABLE events(id INTEGER PRIMARY KEY,at TEXT,book TEXT,kind TEXT,detail TEXT);''')
        self.config = {'podcasts': dict(enabled=True, abs_library_id='library', jellyfin_library_id='jf-library',
            abs_root='/podcasts', jellyfin_root='/podcasts', worker_root=str(root / 'media'),
            expected_abs_username='reader', pinepods_enabled=True)}
        media = root / 'media' / 'show' / 'episode.mp3'
        media.parent.mkdir(parents=True)
        media.write_bytes(b'a' * 1000)
        self.item = dict(id='show', libraryId='library', mediaType='podcast', path='/podcasts/show',
            media=dict(metadata=dict(feedUrl=FEED), episodes=[dict(id='episode', libraryItemId='show',
                enclosure=dict(url=URL), audioFile=dict(duration=600.25,
                    metadata=dict(path='/podcasts/show/episode.mp3', size=1000,
                                  mtimeMs=media.stat().st_mtime_ns / 1_000_000)))]))
        self.abs = Mock()
        self.abs.item.side_effect = lambda ident: copy.deepcopy(self.item)
        self.pinepods = Pine()
        self.external_write_guard = Mock()

    def load(self, key):
        row = self.db.execute('SELECT value FROM state WHERE id=?', (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)', (key, json.dumps(value)))
        self.db.commit()


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.b = Bridge(Path(self.temp.name).resolve())
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.b.db.close)
        self.mapping = build_mapping(self.b.item, self.b.item['media']['episodes'][0], self.b.config['podcasts'])
        self.mapping['jf'] = dict(id='jf-item', size=1000, duration=600.25)
        self.probe = Mock(return_value=evidence())
        self.manager = PineMetadataRepair(self.b, probe=self.probe, allow_full_probe=False)
        self.key = NAMESPACE + self.mapping['id']

    def run_repair(self, apply=True):
        return self.manager.repair_if_needed(self.mapping, apply)

    def test_preview_has_no_network_or_state_writes(self):
        self.assertEqual(self.run_repair(False)['reason'], 'preview_no_metadata_repair')
        self.b.abs.item.assert_not_called()
        self.assertEqual(self.b.pinepods.reads, 0)
        self.probe.assert_not_called()
        self.assertEqual(self.b.db.execute('SELECT COUNT(*) FROM state').fetchone()[0], 0)

    def test_real_probe_uses_private_state_disk_for_both_prefix_and_full_requests(self):
        manager=PineMetadataRepair(self.b)
        unknown={'known':False,'reason':'mpeg_audio_without_frame_count_tag'}
        with patch.object(pine_media_probe,'measure',side_effect=[unknown,full_evidence()]) as measure:
            self.assertTrue(manager._measure(URL)['known'])
        root=self.b.root/'pine-media-probes'
        self.assertEqual(root.stat().st_mode & 0o777,0o700)
        self.assertEqual(list(root.iterdir()),[])
        self.assertEqual(len(measure.call_args_list),2)
        for call in measure.call_args_list:self.assertEqual(call.kwargs['temp_root'],root)
        self.assertTrue(measure.call_args_list[1].kwargs['full_probe'])

    def test_real_probe_refuses_unsafe_storage_without_chmod_or_network(self):
        root=self.b.root/'pine-media-probes';root.mkdir(mode=0o755)
        with patch.object(pine_media_probe,'measure') as measure:
            result=PineMetadataRepair(self.b)._measure(URL)
        self.assertEqual(result['reason'],'probe_storage_unavailable')
        self.assertEqual(root.stat().st_mode & 0o777,0o755);measure.assert_not_called()

    def test_injected_probes_do_not_receive_or_create_storage_directory(self):
        self.assertTrue(self.manager._measure(URL)['known'])
        self.assertNotIn('temp_root',self.probe.call_args.kwargs)
        self.assertFalse((self.b.root/'pine-media-probes').exists())

    def test_success_has_durable_intent_before_only_metadata_post(self):
        original = self.b.pinepods.write_episode_duration
        def writer(before, duration):
            saved = self.b.load(self.key)
            self.assertEqual(saved['stage'], 'intent')
            self.assertEqual(saved['before'], before)
            self.assertEqual(self.b.db.execute('SELECT status FROM operations').fetchone()[0], 'pending')
            return original(before, duration)
        self.b.pinepods.write_episode_duration = writer
        self.assertEqual(self.run_repair()['outcome'], 'corrected')
        self.assertEqual(self.b.pinepods.posts, [600])
        self.assertEqual(self.b.load(self.key)['stage'], 'verified')
        self.assertEqual(self.b.db.execute('SELECT status FROM operations').fetchone()[0], 'complete')
        self.assertEqual(self.b.pinepods.value['position'], None)
        self.assertFalse(self.b.pinepods.value['completed'])
        events = json.dumps(self.b.db.execute('SELECT detail FROM events').fetchall())
        self.assertNotIn('not-for-logs', events)
        self.assertNotIn('episode.mp3', events)

    def test_completed_historical_overrun_is_preserved_exactly(self):
        self.b.pinepods.value.update(completed=True, position=720)
        self.b.pinepods.value['metadata'].update(completed=True, listenduration=720)
        self.assertEqual(self.run_repair()['outcome'], 'corrected')
        self.assertEqual(self.b.pinepods.value['position'], 720)
        self.assertTrue(self.b.pinepods.value['completed'])

    def test_nochange_does_not_probe_or_write(self):
        self.b.pinepods.value['duration'] = 600
        self.b.pinepods.value['metadata']['episodeduration'] = 600
        self.assertEqual(self.run_repair()['outcome'], 'nochange')
        self.probe.assert_not_called()
        self.assertFalse(self.b.pinepods.posts)

    def test_pending_progress_state_or_journal_prevents_inspection(self):
        for state_pending in (True, False):
            with self.subTest(state_pending=state_pending):
                if state_pending:
                    self.b.save(self.mapping['id'], {'pending': {'id': 'p'}})
                else:
                    self.b.save(self.mapping['id'], {})
                    self.b.db.execute('INSERT INTO operations VALUES(?,?,?,?)', ('p', self.mapping['id'], '{}', 'pending'))
                    self.b.db.commit()
                self.assertEqual(self.run_repair()['reason'], 'progress_transfer_pending')
                self.assertEqual(self.b.pinepods.reads, 0)
                self.probe.assert_not_called()

    def test_downloaded_and_active_unfinished_episode_are_not_probed(self):
        for changes in ({'downloaded': True}, {'position': 12}):
            self.b.pinepods.value.update(downloaded=False, position=None)
            self.b.pinepods.value.update(changes)
            self.b.db.execute('DELETE FROM state'); self.b.db.commit()
            self.assertEqual(self.run_repair()['outcome'], 'waiting')
        self.probe.assert_not_called()
        self.assertFalse(self.b.pinepods.posts)

    def test_unknown_header_backoff_avoids_reprobe_and_new_identity_invalidates_it(self):
        self.probe.return_value = {'known': False, 'reason': 'mpeg_audio_without_frame_count_tag'}
        self.assertEqual(self.run_repair()['reason'], 'audio_header_unverified')
        self.assertEqual(self.b.load(self.key)['next_probe_at'], self.b.time + 86400)
        self.assertEqual(self.run_repair()['reason'], 'metadata_probe_backoff')
        self.assertEqual(self.probe.call_count, 1)
        self.mapping['jf']['id'] = 'new-jf'
        self.assertEqual(self.run_repair()['reason'], 'audio_header_unverified')
        self.assertEqual(self.probe.call_count, 2)

    def test_timeout_has_short_backoff(self):
        self.probe.return_value = {'known': False, 'reason': 'probe_timeout'}
        self.run_repair()
        self.assertEqual(self.b.load(self.key)['next_probe_at'], self.b.time + 900)

    def test_wrong_or_fabricated_measurement_never_writes(self):
        for value in (evidence(720), dict(known=True, duration_seconds=600.25)):
            self.b.db.execute('DELETE FROM state'); self.b.db.commit()
            self.probe.return_value = value
            self.assertEqual(self.run_repair()['outcome'], 'waiting')
        self.assertFalse(self.b.pinepods.posts)

    def test_three_failed_probes_exhaust_cycle_budget(self):
        self.probe.return_value = {'known': False, 'reason': 'probe_failed'}
        for _ in range(3):
            self.run_repair()
            self.b.time += 901
        self.assertEqual(self.run_repair()['reason'], 'metadata_repair_cycle_budget')
        self.assertEqual(self.probe.call_count, 3)

    def test_full_fallback_is_once_per_cycle_with_shared_45_second_deadline(self):
        self.manager = PineMetadataRepair(self.b, probe=self.probe)
        self.probe.side_effect = [{'known':False,'reason':'id3_exceeds_prefix'}, evidence(),
                                 {'known':False,'reason':'mpeg_audio_without_frame_count_tag'}]
        with patch('pine_metadata_repair.time.monotonic', side_effect=[100,100,102]):
            self.assertEqual(self.run_repair()['outcome'], 'corrected')
        self.assertEqual(self.probe.call_args_list[1].kwargs, dict(timeout_seconds=43, full_probe=True))
        self.b.pinepods.value['duration'] = 480
        self.b.pinepods.value['metadata']['episodeduration'] = 480
        self.mapping['jf']['id'] = 'different-source'
        with patch('pine_metadata_repair.time.monotonic', return_value=110):
            self.assertEqual(self.run_repair()['reason'], 'metadata_repair_cycle_budget')
        self.assertEqual(self.probe.call_count, 3)
        self.assertNotIn('next_probe_at', self.b.load(self.key))

    def test_exhausted_probe_deadline_never_starts_another_child(self):
        self.manager.probe_deadline = 100
        with patch('pine_metadata_repair.time.monotonic', return_value=101):
            self.assertEqual(self.run_repair()['outcome'], 'waiting')
        self.probe.assert_not_called()

    def test_file_changed_during_probe_never_reaches_intent(self):
        def changed(*args, **kwargs):
            Path(self.mapping['worker_path']).write_bytes(b'new')
            return evidence()
        self.probe.side_effect = changed
        self.assertEqual(self.run_repair()['outcome'], 'waiting')
        self.assertFalse(self.b.pinepods.posts)
        self.assertNotEqual(self.b.load(self.key).get('stage'), 'intent')

    def test_symlink_or_stale_abs_file_is_rejected(self):
        worker = Path(self.mapping['worker_path'])
        elsewhere = worker.with_name('elsewhere.mp3')
        worker.rename(elsewhere); worker.symlink_to(elsewhere)
        self.assertEqual(self.run_repair()['outcome'], 'waiting')
        self.probe.assert_not_called()

    def test_native_change_during_probe_does_not_write(self):
        def changed(*args, **kwargs):
            self.b.pinepods.value['position'] = 5
            self.b.pinepods.value['metadata']['listenduration'] = 5
            return evidence()
        self.probe.side_effect = changed
        self.assertEqual(self.run_repair()['reason'], 'pinepods_changed_during_probe')
        self.assertFalse(self.b.pinepods.posts)

    def test_post_success_lost_ack_recovers_by_readback_without_second_post(self):
        def lost(_value):
            raise TimeoutError('secret URL')
        self.b.pinepods.after_write = lost
        self.assertEqual(self.run_repair()['outcome'], 'waiting')
        self.assertEqual(self.b.load(self.key)['stage'], 'intent')
        self.assertEqual(self.run_repair()['reason'], 'interrupted_repair_verified')
        self.assertEqual(self.b.pinepods.posts, [600])
        self.assertEqual(self.probe.call_count, 1)

    def test_post_not_applied_is_never_retried_automatically(self):
        self.b.pinepods.write_episode_duration = Mock(side_effect=TimeoutError('private'))
        self.run_repair()
        self.assertEqual(self.run_repair()['reason'], 'interrupted_repair_requires_review')
        self.assertEqual(self.b.pinepods.write_episode_duration.call_count, 1)

    def test_user_progress_changed_after_post_is_not_rolled_back(self):
        def changed(value):
            value['position'] = 22
            value['metadata']['listenduration'] = 22
        self.b.pinepods.after_write = changed
        self.assertEqual(self.run_repair()['reason'], 'metadata_repair_readback_changed')
        self.assertEqual(self.run_repair()['reason'], 'interrupted_repair_requires_review')
        self.assertEqual(self.b.pinepods.value['position'], 22)
        self.assertEqual(self.b.pinepods.posts, [600])

    def test_rss_regression_triggers_new_probe_not_permanent_override(self):
        self.assertEqual(self.run_repair()['outcome'], 'corrected')
        self.b.pinepods.value['duration'] = 480
        self.b.pinepods.value['metadata']['episodeduration'] = 480
        self.assertEqual(self.run_repair()['outcome'], 'corrected')
        self.assertEqual(self.probe.call_count, 2)
        self.assertEqual(self.b.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0], 2)

    def seed_full_repair(self):
        self.manager = PineMetadataRepair(self.b, probe=self.probe)
        self.probe.side_effect = [{'known':False,'reason':'mpeg_audio_without_frame_count_tag'}, full_evidence()]
        self.assertEqual(self.run_repair()['outcome'], 'corrected')
        self.assertEqual(self.b.load(self.key)['full_proof_at'], self.b.time)
        self.b.pinepods.value['duration'] = 480
        self.b.pinepods.value['metadata']['episodeduration'] = 480
        self.probe.reset_mock()
        self.manager = PineMetadataRepair(self.b, probe=self.probe)

    def test_verified_full_proof_survives_regression_without_validator_and_daily_wait(self):
        self.seed_full_repair()
        original = self.b.load(self.key)['full_proof']
        self.b.time += 60
        self.probe.side_effect = [{'known':False,'reason':'mpeg_audio_without_frame_count_tag'}]
        self.assertEqual(self.run_repair()['reason'], 'metadata_full_probe_cooldown')
        saved = self.b.load(self.key)
        self.assertEqual(saved['full_proof'], original)
        self.assertEqual(saved['full_proof_at'], 1000)
        self.assertEqual(saved['last_full_probe_at'], 1000)
        self.assertEqual(saved['next_probe_at'], 1000 + 900)
        self.assertEqual(self.probe.call_args.kwargs['previous_evidence'], original)
        self.assertNotIn('full_probe', self.probe.call_args.kwargs)
        self.assertEqual(self.b.pinepods.posts, [600])

    def test_verified_full_proof_survives_transport_failure(self):
        self.seed_full_repair()
        original = self.b.load(self.key)['full_proof']
        self.probe.side_effect = [{'known':False,'reason':'probe_timeout'}]
        self.assertEqual(self.run_repair()['reason'], 'audio_header_unverified')
        self.assertEqual(self.b.load(self.key)['full_proof'], original)
        self.assertEqual(self.b.load(self.key)['next_probe_at'], self.b.time + 900)

    def test_full_proof_expires_after_thirty_days_and_does_not_extend_on_reads(self):
        self.seed_full_repair()
        self.b.time += 31 * 86400
        self.probe.side_effect = [{'known':False,'reason':'mpeg_audio_without_frame_count_tag'}, full_evidence()]
        self.assertEqual(self.run_repair()['outcome'], 'corrected')
        self.assertNotIn('previous_evidence', self.probe.call_args_list[0].kwargs)
        self.assertTrue(self.probe.call_args_list[1].kwargs['full_probe'])
        self.assertEqual(self.b.load(self.key)['full_proof_at'], self.b.time)

    def test_source_identity_change_invalidates_proof_and_daily_throttle(self):
        self.seed_full_repair()
        self.mapping['jf']['id'] = 'replacement-jf-id'
        self.probe.side_effect = [{'known':False,'reason':'mpeg_audio_without_frame_count_tag'}, full_evidence()]
        self.assertEqual(self.run_repair()['outcome'], 'corrected')
        self.assertNotIn('previous_evidence', self.probe.call_args_list[0].kwargs)
        self.assertTrue(self.probe.call_args_list[1].kwargs['full_probe'])

    def test_real_validator_cache_repair_reuses_full_proof_without_renewing_age(self):
        now = dt.datetime.now(dt.timezone.utc)
        self.b.time = now.timestamp() - 3600
        self.manager = PineMetadataRepair(self.b, probe=self.probe)
        prefix = b'x' * pine_media_probe.MAX_PREFIX
        prefix_hash = hashlib.sha256(prefix).hexdigest()
        original = full_evidence()
        original['prefix_sha256'] = prefix_hash
        original['fetch'].update(etag_kind='strong', strong_etag_sha256='b' * 64,
            prefix_sha256=prefix_hash, prefix_bytes=len(prefix), content_length_header=8000000,
            source_url_sha256=hashlib.sha256(URL.encode()).hexdigest(),
            fetched_at=dt.datetime.fromtimestamp(self.b.time, dt.timezone.utc).isoformat())
        original['evidence_sha256'] = pine_media_probe.evidence_hash(original)
        self.probe.side_effect = [{'known':False,'reason':'mpeg_audio_without_frame_count_tag'}, original]
        self.assertEqual(self.run_repair()['outcome'], 'corrected')
        first_at = self.b.load(self.key)['full_proof_at']
        self.b.pinepods.value['duration'] = 480
        self.b.pinepods.value['metadata']['episodeduration'] = 480
        self.b.time = now.timestamp()
        fetched = dict(response_total_bytes=8000000, bytes_read=len(prefix), content_length_header=len(prefix),
            range_honoured=True, etag_kind='strong', strong_etag_sha256='b' * 64,
            fetched_at=now.isoformat(), source_url_sha256=hashlib.sha256(URL.encode()).hexdigest())
        def fresh_cache(url, **kwargs):
            self.assertNotIn('full_probe', kwargs)
            self.assertEqual(kwargs['previous_evidence'], original)
            with patch.object(pine_media_probe, 'fetch_prefix', return_value=(prefix, fetched)):
                return pine_media_probe.worker_result(url, previous_evidence=kwargs['previous_evidence'])
        self.probe.reset_mock(); self.probe.side_effect = fresh_cache
        self.manager = PineMetadataRepair(self.b, probe=self.probe)
        self.assertEqual(self.run_repair()['outcome'], 'corrected')
        saved = self.b.load(self.key)
        self.assertEqual(saved['evidence']['header']['source'], 'validated_full_mp3_cache')
        self.assertEqual(saved['full_proof'], original)
        self.assertEqual(saved['full_proof_at'], first_at)
        self.assertEqual(saved['last_full_probe_at'], first_at)
        self.assertEqual(self.probe.call_count, 1)
        self.assertEqual(self.b.pinepods.posts, [600, 600])

    def test_orphan_operation_and_nondurable_db_block_writes(self):
        self.b.db.execute('INSERT INTO operations VALUES(?,?,?,?)', ('orphan', self.key, '{}', 'pending'))
        self.b.db.commit()
        self.assertEqual(self.run_repair()['reason'], 'metadata_journal_requires_review')
        self.b.db.execute('DELETE FROM operations'); self.b.db.commit()
        self.b.db.execute('PRAGMA synchronous=OFF')
        self.assertEqual(self.run_repair()['outcome'], 'waiting')
        self.assertFalse(self.b.pinepods.posts)


if __name__ == '__main__':
    unittest.main()
