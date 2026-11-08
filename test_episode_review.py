import os
import unittest
from unittest.mock import patch

import podcast_review as review
from podcast_sync import PodcastCoordinator, fingerprint, run
from podcast_status_sync import state_key
from test_podcast_status_sync import VariantPine
import test_podcast_sync as base

KEY = '_podcast:show:episode'


class EpisodeReviewTests(unittest.TestCase):
    setUp = base.Tests.setUp
    write_gate = base.Tests.write_gate
    load = base.Tests.load
    save = base.Tests.save
    write_record = base.Tests.write_record

    def configure(self):
        cfg=self.cfg['podcasts']
        media=(self.root/'media').resolve()
        file=media/'Show/episode.mp3'
        file.parent.mkdir(parents=True)
        file.write_bytes(b'x'*1000)
        os.utime(file,(base.STAMP/1000,base.STAMP/1000))
        cfg.update(worker_root=str(media),pinepods_enabled=True,pinepods_optional_enrollment=True,pinepods_variant_status_only=True)
        # Production workers provide the current kernel/mount scope. An
        # explicit review can persist reusable proof only within that scope.
        def media_scope(kind):
            self.assertEqual(kind,'podcasts')
            return 'verified-podcast-mount-generation'
        self.b.media_observation_generation=media_scope
        self.b.media_observation_guard=media_scope
        self.gate['podcast_mounts']['worker']['destination']=str(media)
        self.write_gate()
        self.b.pinepods=self.pine=VariantPine(lambda:self.clock)
        self.pine.server_version="0.9.0"
        self.reported=600
        def native(feed,url,duration):
            return dict(duration=self.reported,account=self.pine.identity,user_id=7,episode_id=9,podcast_id=5,downloaded=False,
                        metadata=dict(is_downloaded=False,is_youtube=False,episodeid=9,podcastid=5,feedurl=feed,episodeurl=url))
        self.pine.inspect_episode_metadata=native
        self.abs.set(200);self.jf.set(200);self.pine.set(125,'in_progress')
        c=PodcastCoordinator(self.b)
        m=c.mappings()[0][0]
        local,_=c.read(dict(m,_pine_selected=False))
        pine_fp=fingerprint([m['feed_url'],m['enclosure_url'],m['duration'],c.accounts['pinepods']])
        previous=dict(mapping=fingerprint([m['fingerprint'],m['jf']['fingerprint']]),pine_enrolled=True,
            baseline=dict(local,pinepods=dict(status='in_progress',position=120,missing=False,fingerprint=pine_fp),
                          gpodder=dict(status='in_progress',position=120,missing=False,fingerprint=fingerprint([pine_fp,'gpodder']))),
            pine_raw=dict(native_episode_id=9,native_user_id=7))
        self.save(KEY,previous)
        self.write_record('old',KEY,previous,'complete')
        return c,m,previous

    def cycle(self):
        self.clock+=3;self.gate['checked_at']=self.clock;self.write_gate()
        return run(self.b,apply=True,settle=0)

    def test_manual_and_restart_with_global_rule_off(self):
        _,_,previous=self.configure()
        self.cfg['podcasts']['pinepods_variant_status_only']=False
        row=review.review(self.b,KEY)
        self.assertEqual(row['mode'],'reported')
        self.assertFalse(self.load(review.PREFIX+KEY))
        review.review(self.b,KEY,record=row,source='abs')
        self.assertEqual(self.load(review.PREFIX+KEY)['previous_core'],previous)
        self.assertFalse(self.pine.writes or self.jf.writes or self.abs.writes)
        self.assertTrue(self.cycle()['ok'])
        self.abs.set(235)
        result=self.cycle()
        self.assertTrue(result['ok'],result)
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],235*base.TICKS)
        self.assertEqual(self.pine.position,125)
        self.assertFalse(self.pine.writes)
        self.reported=680
        self.assertFalse(self.cycle()['ok'])
        self.assertFalse(self.pine.writes)

    def test_user_selects_source_and_syncs_only_status(self):
        self.configure()
        self.cfg['podcasts']['pinepods_variant_status_only']=False
        self.pine.set(600,'finished')
        row=review.review(self.b,KEY)
        review.review(self.b,KEY,record=row,source='pinepods')
        self.assertEqual(self.abs.raw['currentTime'],200)
        result=self.cycle()
        self.assertTrue(result['ok'],result)
        self.assertTrue(self.abs.raw['isFinished'])
        self.assertEqual(self.abs.raw['currentTime'],200)
        self.assertTrue(self.jf.row['UserData']['Played'])
        self.assertNotIn('reviewed_source',self.load(state_key(KEY)))

    def test_recheck_at_approval_and_after_approval(self):
        self.configure()
        row=review.review(self.b,KEY)
        self.pine.set(600,'finished')
        with self.assertRaises(review.ReviewError):review.review(self.b,KEY,record=row,source='abs')
        fresh=review.review(self.b,KEY)
        review.review(self.b,KEY,record=fresh,source='pinepods')
        self.pine.set(200,'in_progress')
        result=self.cycle()
        self.assertFalse(result['ok'])
        self.assertFalse(self.pine.writes or self.jf.writes or self.abs.writes)

    def test_pending_and_wrong_native_id_refused(self):
        _,_,previous=self.configure()
        self.write_record('in-flight',KEY,{},'pending')
        with self.assertRaises(review.ReviewError):review.review(self.b,KEY)
        self.write_record('in-flight',KEY,{},'complete')
        previous['pine_raw']['native_episode_id']=15;self.save(KEY,previous)
        with self.assertRaises(review.ReviewError):review.review(self.b,KEY)
        self.assertFalse(self.load(review.PREFIX+KEY))

    def test_confirmed_agreeing_variant_automatically_migrates_with_history(self):
        c,m,previous=self.configure()
        proof=review.reported_proof(c,m)
        with patch.object(PodcastCoordinator,'variant_candidate',return_value=True), \
             patch.object(PodcastCoordinator,'repair_metadata',return_value={'outcome':'waiting'}), \
             patch.object(PodcastCoordinator,'variant_proof',return_value=proof):
            result=self.cycle()
            self.assertTrue(result['ok'],result)
            self.assertEqual(result['episodes'][0]['pine_mode'],'status_only')
            self.assertEqual(self.load(review.PREFIX+KEY)['previous_core'],previous)
            self.assertTrue(self.cycle()['ok'])
            self.assertFalse(self.pine.writes or self.abs.writes or self.jf.writes)

    def test_conflicting_variant_does_not_automatically_migrate(self):
        c,m,_=self.configure()
        self.pine.set(600,'finished')
        with patch.object(PodcastCoordinator,'variant_candidate',return_value=True), \
             patch.object(PodcastCoordinator,'repair_metadata',return_value={'outcome':'waiting'}), \
             patch.object(PodcastCoordinator,'variant_proof',return_value=review.reported_proof(c,m)):
            self.assertFalse(self.cycle()['ok'])
        self.assertFalse(self.load(review.PREFIX+KEY))


class EpisodeCommandTests(unittest.TestCase):
    def test_approval_requires_its_own_successful_preview(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from cryptography.fernet import Fernet
        from admin_store import AdminStore
        from admin_runtime import Administration
        with TemporaryDirectory() as temp:
            root=Path(temp)
            key=root/'secret.key';key.write_bytes(Fernet.generate_key())
            store=AdminStore(root,key)
            admin=SimpleNamespace(store=store)
            def run_command(kind,payload):
                ident=store.submit(kind,payload)['id']
                self.assertTrue(Administration.process_one(admin,object()))
                return store.command(ident)
            record=dict(episode_id=KEY,digest='d'*64)
            with patch.object(review,'review',return_value=record) as call:
                command=run_command('episode.preview',{'episode_id':KEY})
                self.assertEqual(command['status'],'success')
                confirmation=dict(review_id=command['id'],digest=record['digest'],source='abs',confirm=True)
                self.assertEqual(run_command('episode.approve',confirmation)['status'],'success')
                self.assertEqual(call.call_args.kwargs,dict(record=record,source='abs'))
                self.assertEqual(run_command('episode.approve',dict(confirmation,digest='0'*64))['status'],'failed')
                self.assertEqual(run_command('episode.approve',dict(confirmation,confirm=False))['status'],'failed')
                with self.assertRaises(ValueError):store.review(command['id'],'review.preview')
