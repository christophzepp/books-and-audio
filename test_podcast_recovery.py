import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from pine_feed_route import ReviewedFeeds
from pine_identity_alias import AliasError, fingerprint, binding_evidence_fingerprint
from pine_metadata_repair import PineMetadataRepair
from podcast_variant_proof import VariantProof
import test_pine_metadata_repair as metadata
import test_pine_alias_coordinator as alias_harness
from test_pine_alias_coordinator import TARGET_FEED

OLD = 'https://old.example/feed?edition=private'
NEW_EP = 'https://example.org/new.mp3?edition=private'


class FeedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.env = alias_harness.AliasCoordinatorTests(); self.env.setUp(); self.addCleanup(self.env.doCleanups)
        self.b = self.env.e.b
        record = copy.deepcopy(self.env.record)
        record['source']['feed_url'] = OLD
        record['evidence']['bindings_sha256'] = binding_evidence_fingerprint(record['source'], record['target'], record['account'], record['source_history'])
        record['evidence_sha256'] = fingerprint(record['evidence'])
        self.b.save(self.env.alias_key, record)
        self.record = record
        self.mapping = dict(self.env.mapping, id='_podcast:show:incoming', episode_id='incoming', feed_url=OLD, enclosure_url=NEW_EP, abs_guid='new-guid')
        native = dict(podcast_id=18, user_id=2, episode_id=999, metadata=dict(episodeurl=NEW_EP))
        self.pine = SimpleNamespace(identity=copy.deepcopy(record['account']), _ensure_native=lambda:2,
            _request=Mock(return_value={'pods':[dict(podcastid=18, feedurl=TARGET_FEED)]}),
            actions=Mock(return_value={'actions':[]}), inspect_episode_metadata=Mock(return_value=native))
        self.resolver = ReviewedFeeds(self.b, self.pine)

    def test_verified_feed_move_accepts_only_exact_enclosure_without_writes(self):
        before = list(self.b.db.execute('SELECT id,value FROM state ORDER BY id'))
        self.assertEqual(self.resolver.resolve(self.mapping), TARGET_FEED)
        self.pine.inspect_episode_metadata.assert_called_once_with(TARGET_FEED, NEW_EP, self.mapping['duration'])
        self.assertTrue(self.mapping['_pine_reviewed_feed'])
        self.assertEqual(before, list(self.b.db.execute('SELECT id,value FROM state ORDER BY id')))

    def test_coordinator_uses_canonical_feed_without_changing_local_mapping(self):
        from podcast_sync import PodcastCoordinator
        self.b.pinepods=self.pine
        original=copy.deepcopy(self.mapping)
        route=PodcastCoordinator(self.b).pine_route(self.mapping)
        self.assertEqual(route['feed_url'],TARGET_FEED)
        self.assertEqual(route['enclosure_url'],original['enclosure_url'])
        self.assertEqual(self.mapping['feed_url'],OLD)
        self.assertEqual(self.mapping['fingerprint'],original['fingerprint'])

    def test_old_namespace_is_rechecked_before_each_route_use(self):
        self.assertEqual(self.resolver.resolve(self.mapping),TARGET_FEED)
        self.pine.actions.return_value={'actions':[dict(podcast=OLD,episode=NEW_EP,action='new')]}
        with self.assertRaises(AliasError):self.resolver.resolve(self.mapping)

    def test_account_or_source_podcast_cannot_borrow_another_review(self):
        for update in (dict(podcast_id='other'), dict(feed_url='https://another.example/feed')):
            self.assertEqual(self.resolver.resolve(dict(self.mapping, **update)), dict(self.mapping, **update)['feed_url'])
        self.pine.identity = dict(self.pine.identity, username='other')
        self.assertEqual(self.resolver.resolve(self.mapping), OLD)
        self.pine._request.assert_not_called()

    def test_coexisting_public_and_private_feeds_are_not_combined(self):
        self.pine._request.return_value['pods'].append(dict(podcastid=19, feedurl=OLD))
        self.assertEqual(self.resolver.resolve(self.mapping), OLD)
        self.pine.actions.assert_not_called()

    def test_conflicting_reviewed_feed_pairs_are_rejected(self):
        record = copy.deepcopy(self.record)
        record['mapping_id'] = '_podcast:show:other'; record['source']['podcast_id']='show';record['source']['episode_id']='other'
        self.mapping['podcast_id']='show'
        # Keep the first source and the new anchor in the same subscription.
        original=copy.deepcopy(self.record);original['source']['podcast_id']='show';original['mapping_id']='_podcast:show:original';original['source']['episode_id']='original'
        for row in (record,original):
            if row is record:row['target'].update(podcast_id=19,feed_url='https://other.example/feed')
            row['evidence']['bindings_sha256']=binding_evidence_fingerprint(row['source'],row['target'],row['account'],row['source_history'])
            row['evidence_sha256']=fingerprint(row['evidence'])
            self.b.save('_pine_alias:'+row['mapping_id'],row)
        with self.assertRaises(AliasError):self.resolver.resolve(self.mapping)

    def test_old_play_reset_or_deletion_is_not_silently_discarded(self):
        for action in ('play','new','delete'):
            self.resolver=ReviewedFeeds(self.b,self.pine)
            self.pine.actions.return_value={'actions':[dict(podcast=OLD,episode=NEW_EP,action=action,timestamp='2026-11-06T10:00:00',position=0,total=100)]}
            with self.assertRaises(AliasError):self.resolver.resolve(self.mapping)
        self.pine.inspect_episode_metadata.assert_not_called()

    def test_same_title_other_url_or_native_account_is_not_a_match(self):
        for patch in (dict(metadata=dict(episodeurl='https://wrong.example/episode.mp3')),dict(podcast_id=19),dict(user_id=3)):
            original=copy.deepcopy(self.pine.inspect_episode_metadata.return_value)
            self.pine.inspect_episode_metadata.return_value.update(patch)
            with self.assertRaises(AliasError):ReviewedFeeds(self.b,self.pine).resolve(self.mapping)
            self.pine.inspect_episode_metadata.return_value=original

    def test_existing_episode_history_cannot_be_reassigned(self):
        self.b.save('_podcast:other:episode',dict(pine_raw=dict(native_episode_id=999,native_user_id=2)))
        with self.assertRaises(AliasError):self.resolver.resolve(self.mapping)

    def test_enrolled_native_identity_must_remain_the_same(self):
        self.b.save(self.mapping['id'],dict(pine_raw=dict(native_episode_id=998,native_user_id=2)))
        with self.assertRaises(AliasError):self.resolver.resolve(self.mapping)

    def test_incomplete_duplicate_or_removed_personal_inventory_is_rejected(self):
        for rows in (None,[dict(podcastid=18,feedurl=TARGET_FEED)]*2,[]):
            self.pine._request.return_value={'pods':rows}
            with self.assertRaises(AliasError):ReviewedFeeds(self.b,self.pine).resolve(self.mapping)


class MeasurementRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.env=metadata.RepairTests();self.env.setUp();self.addCleanup(self.env.doCleanups)

    def test_variant_measurement_survives_budget_and_transport_deferrals(self):
        e=self.env
        e.manager=PineMetadataRepair(e.b,probe=e.probe)
        e.probe.side_effect=[{'known':False,'reason':'mpeg_audio_without_frame_count_tag'}, metadata.full_evidence(650.25)]
        self.assertEqual(e.run_repair()['reason'],'measured_audio_duration_differs')
        source=e.manager._source(e.mapping);measurement=e.b.load(e.key)['candidate_evidence']
        for failure in ('budget','transport'):
            e.b.time+=1000 if failure=='transport' else 60;e.manager=PineMetadataRepair(e.b,probe=e.probe)
            e.probe.side_effect=[{'known':False,'reason':'mpeg_audio_without_frame_count_tag' if failure=='budget' else 'probe_timeout'}]
            e.run_repair()
            stored=e.b.load(e.key)
            self.assertEqual(stored['candidate_evidence'],measurement)
            self.assertEqual(VariantProof(e.b)._seed(e.mapping,source,e.b.time),measurement)
        self.assertFalse(e.b.pinepods.posts)
        self.assertFalse(e.b.db.execute("SELECT 1 FROM operations WHERE status='pending'").fetchone())

    def test_old_daily_limit_recovers_after_bounded_fifteen_minute_cooldown(self):
        e=self.env;e.seed_full_repair()
        value=e.b.load(e.key);value.update(stage='deferred',next_probe_at=1000+86400)
        e.b.save(e.key,value);e.b.time=2000
        e.probe.side_effect=[{'known':False,'reason':'mpeg_audio_without_frame_count_tag'},metadata.full_evidence()]
        self.assertEqual(e.run_repair()['outcome'],'corrected')
        self.assertEqual(e.b.pinepods.posts,[600,600])
        self.assertEqual(sum(c.kwargs.get('full_probe',False) for c in e.probe.call_args_list),1)

    def test_changed_local_source_does_not_inherit_candidate_evidence(self):
        e=self.env;e.probe.return_value=metadata.full_evidence(650.25);e.run_repair()
        value=e.b.load(e.key);source=e.manager._source(e.mapping)
        altered=copy.deepcopy(source);altered['file']['inode']+=1
        self.assertEqual(e.manager._cache_fields(value,altered),{})
        self.assertIsNone(VariantProof(e.b)._seed(e.mapping,altered,e.b.time))

    def test_tampered_measurement_cannot_authorize_variant_admission(self):
        e=self.env;e.probe.return_value=metadata.full_evidence(650.25);e.run_repair()
        value=e.b.load(e.key);value['candidate_evidence']['duration_seconds']=700;e.b.save(e.key,value)
        self.assertIsNone(VariantProof(e.b)._seed(e.mapping,e.manager._source(e.mapping),e.b.time))
