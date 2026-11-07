"""Real SQLite regression tests for manifest-only three-source baseline refresh."""
import copy
import json
import unittest
from unittest.mock import patch

import sync
from jellyfin_audio import AudioCoordinator, TICKS
import test_revision_sync as revision_harness
from test_jellyfin_transactions import FakeJellyfin


class JellyfinRevisionTests(unittest.TestCase):
    def setUp(self):
        self.env = revision_harness.RevisionIntegrationTests()
        self.env.setUp()
        self.addCleanup(self.env.doCleanups)
        self.b = self.env.bridge
        self.jf = FakeJellyfin()
        self.b.jellyfin = self.jf
        self.b.config['jellyfin'] = {'audio_library_id': 'lib'}
        self.env.baseline(25)
        self.jf.data.update(PlaybackPositionTicks=25*TICKS)
        self.assertFalse(self.env.cycle()['blocked'])
        self.key = '_jellyfin_audio:' + self.env.seed['abs_id']
        self.original = copy.deepcopy(self.b.load(self.key))
        self.original_core = copy.deepcopy(self.b.load(self.env.seed['abs_id']))
        self.assert_no_writes()

    def assert_no_writes(self):
        self.assertFalse(self.env.abs.writes or self.env.bo.writes or self.jf.writes)

    def new_revision(self, letter='b'):
        self.env.change_revision(letter)
        return self.env.prepare()

    def make_stale(self):
        self.new_revision()
        self.b.save(self.key, self.original)

    def test_revision_refresh_updates_both_baselines_and_keeps_jellyfin_identity(self):
        previous = copy.deepcopy(self.original)
        previous.update(candidate={'since': 900, 'signature':'old'}, independent_note={'keep':True})
        self.b.save(self.key, previous)
        mapping, _, _, _, revised = self.new_revision()
        expected = copy.deepcopy(previous); expected.pop('candidate')
        for side in ('abs','bo'):
            expected['baseline'][side]['fingerprint'] = mapping['fingerprint']
        self.assertTrue(revised)
        self.assertEqual(self.b.load(self.key), expected)
        self.assertEqual(self.b.load(self.key)['baseline']['jf'], previous['baseline']['jf'])
        self.assertEqual(self.b.load(self.key)['raw'], previous['raw'])
        self.assertNotIn('_jellyfin_revision_preview', self.b.list_mappings()[0])
        self.assert_no_writes()

    def test_three_way_cycle_accepts_scan_without_replaying_playback(self):
        self.env.change_revision()
        result = self.env.cycle()
        self.assertFalse(result['blocked'], result)
        self.assertEqual(result['revisions_updated'], 1)
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(self.env.abs.raw['currentTime'], 25)
        self.assertEqual(self.env.bo.raw['positionMs'], 25000)
        self.assertEqual(self.jf.data['PlaybackPositionTicks'], 25*TICKS)
        self.assert_no_writes()

    def test_dry_run_uses_only_call_local_baseline_and_changes_no_tables(self):
        self.env.change_revision(); before = self.env.snapshot()
        result = self.env.cycle(apply=False)
        self.assertFalse(result['blocked'], result)
        self.assertEqual(result['revisions_proposed'], 1)
        self.assertEqual(before, self.env.snapshot())
        self.assert_no_writes()

    def test_proven_already_stale_baseline_repairs_without_new_manifest_revision(self):
        self.make_stale(); before_core=self.b.load(self.env.seed['abs_id'])
        result = self.env.cycle()
        self.assertFalse(result['blocked'], result)
        self.assertEqual(result['revisions_updated'], 0)
        mapping = self.b.list_mappings()[0]
        for side in ('abs','bo'):
            self.assertEqual(self.b.load(self.key)['baseline'][side]['fingerprint'],mapping['fingerprint'])
        self.assertEqual(self.b.load(self.key)['baseline']['jf'], self.original['baseline']['jf'])
        self.assertEqual(self.b.load(self.env.seed['abs_id']), before_core)
        self.assert_no_writes()

    def test_stale_baseline_dry_run_is_repeatable_and_read_only(self):
        self.make_stale(); before = self.env.snapshot()
        for _ in range(2):
            result = self.env.cycle(apply=False)
            self.assertFalse(result['blocked'], result)
            self.assertEqual(before, self.env.snapshot())
        self.assert_no_writes()

    def test_restart_and_several_verified_revisions_preserve_repair_proof(self):
        for letter in ('b','c','d'):
            self.new_revision(letter)
        self.b.save(self.key,self.original)
        self.b.db.close();self.env.bridge=self.env.open_bridge();self.b=self.env.bridge
        self.b.jellyfin=self.jf
        result=self.env.cycle()
        self.assertFalse(result['blocked'],result)
        self.assertEqual(self.b.load(self.key)['baseline']['jf'],self.original['baseline']['jf'])
        self.assert_no_writes()

    def test_missing_or_changed_historical_identity_evidence_refuses_stale_repair(self):
        self.make_stale()
        self.env.bo.raw['manifestRevision']='b'*64
        for replacement in (None,'0'*64):
            with self.subTest(replacement=replacement):
                if replacement is None:
                    self.b.db.execute('DELETE FROM mapping_revision_evidence WHERE revision=?',('a'*64,))
                else:
                    self.b.db.execute('INSERT OR REPLACE INTO mapping_revision_evidence VALUES (?,?,?)',
                                      (self.env.seed['abs_id'],'a'*64,replacement))
                self.b.db.commit(); before=self.env.snapshot()
                with self.assertRaisesRegex(RuntimeError,'Dateievidenz'):
                    self.env.prepare()
                self.assertEqual(before,self.env.snapshot())
        self.assert_no_writes()

    def test_legacy_fingerprint_alone_or_foreign_book_history_cannot_authorize_repair(self):
        self.make_stale()
        self.env.bo.raw['manifestRevision']='b'*64
        self.b.db.execute('DELETE FROM mapping_revision_history WHERE id=? AND revision=?',
                          (self.env.seed['abs_id'],'a'*64))
        self.b.db.execute('INSERT INTO mapping_revision_history VALUES (?,?,?)',
                          ('foreign-book','a'*64,self.env.seed['fingerprint']))
        self.b.db.commit(); before=self.env.snapshot()
        with self.assertRaisesRegex(RuntimeError,'Dateievidenz'):self.env.prepare()
        self.assertEqual(before,self.env.snapshot());self.assert_no_writes()

    def test_inconsistent_or_unknown_baseline_fingerprint_is_not_rewritten(self):
        self.make_stale()
        for update in ({'abs':'0'*64,'bo':'0'*64},{'abs':'0'*64}):
            value=copy.deepcopy(self.original)
            for side,fp in update.items():value['baseline'][side]['fingerprint']=fp
            self.b.save(self.key,value);before=self.env.snapshot()
            with self.assertRaises(RuntimeError):self.env.prepare()
            self.assertEqual(before,self.env.snapshot())
        self.assert_no_writes()

    def test_real_jellyfin_track_change_is_still_rejected_after_manifest_refresh(self):
        original_items=self.jf.items
        def changed(**kwargs):
            result=original_items(**kwargs);result[0]['Id']='b'*32;return result
        self.jf.items=changed
        self.env.change_revision();result=self.env.cycle()
        self.assertTrue(result['blocked'])
        self.assertIn('Source identity changed',result['blocked'][0]['reason'])
        self.assertEqual(self.b.load(self.key)['baseline']['jf'],self.original['baseline']['jf'])
        self.assert_no_writes()

    def test_changed_jellyfin_size_or_duration_cannot_be_accepted_as_a_revision(self):
        original_items=self.jf.items
        for field in ('size','duration'):
            with self.subTest(field=field):
                def changed(**kwargs):
                    result=original_items(**kwargs)
                    if field=='size':result[0]['MediaSources'][0]['Size']+=1
                    else:result[0]['RunTimeTicks']+=3*TICKS
                    return result
                self.jf.items=changed
                self.env.change_revision();result=self.env.cycle()
                self.assertTrue(result['blocked'])
                self.assertIn('Jellyfin audiobook file differs',result['blocked'][0]['reason'])
                self.assertEqual(self.b.load(self.key)['baseline']['jf'],self.original['baseline']['jf'])
                self.assert_no_writes()

    def test_pending_jellyfin_state_or_journal_blocks_entire_revision_transaction(self):
        for pending in ('state','journal'):
            with self.subTest(pending=pending):
                value=copy.deepcopy(self.original)
                if pending=='state':value['pending']={'stage':'sending_jellyfin_0','goal':value['baseline']['abs']}
                self.b.save(self.key,value)
                if pending=='journal':self.b.write_record('jf-pending',self.key,{'stage':'sending'})
                self.env.change_revision();before=self.env.snapshot()
                with self.assertRaisesRegex(RuntimeError,'Offene'):self.env.prepare()
                self.assertEqual(before,self.env.snapshot())
                self.b.db.execute("DELETE FROM operations WHERE id='jf-pending'");self.b.db.commit()
        self.assert_no_writes()

    def test_same_revision_pending_goal_can_still_be_reconciled_normally(self):
        value=copy.deepcopy(self.original)
        value['pending']={'goal':copy.deepcopy(value['baseline']['abs']),'stage':'sending_jellyfin_0'}
        self.b.save(self.key,value)
        result=self.env.cycle()
        self.assertFalse(result['blocked'],result)
        self.assertNotIn('pending',self.b.load(self.key))
        self.assert_no_writes()

    def test_pending_stale_jellyfin_baseline_is_not_automatically_migrated(self):
        self.make_stale();value=copy.deepcopy(self.original);value['pending']={'stage':'sending'}
        self.b.save(self.key,value);before=self.env.snapshot()
        with self.assertRaisesRegex(RuntimeError,'Offene'):self.env.prepare()
        self.assertEqual(before,self.env.snapshot());self.assert_no_writes()

    def test_concurrent_jellyfin_baseline_update_is_detected_before_new_revision_commit(self):
        self.env.change_revision(); core_before=self.b.load(self.env.seed['abs_id'])
        changed=copy.deepcopy(self.original);changed['raw'][0]['PlayCount']=7
        original_guard=self.b.guard
        def guard():
            original_guard();self.b.save(self.key,changed)
        with patch.object(self.b,'guard',side_effect=guard):
            with self.assertRaisesRegex(RuntimeError,'Jellyfin-Baseline'):self.env.prepare()
        self.assertEqual(self.b.load(self.key),changed)
        self.assertEqual(self.b.load(self.env.seed['abs_id']),core_before)
        self.assertEqual(self.env.overrides(),[]);self.assertEqual(self.env.history(),[])
        self.assert_no_writes()

    def test_concurrent_stale_baseline_update_is_detected_before_repair(self):
        self.make_stale();changed=copy.deepcopy(self.original);changed['raw'][0]['PlayCount']=7
        original_guard=self.b.guard
        def guard():original_guard();self.b.save(self.key,changed)
        with patch.object(self.b,'guard',side_effect=guard):
            with self.assertRaisesRegex(RuntimeError,'Jellyfin-Baseline'):self.env.prepare()
        self.assertEqual(self.b.load(self.key),changed);self.assert_no_writes()

    def test_journal_failure_rolls_back_both_baselines_override_and_history(self):
        self.env.change_revision()
        self.b.db.execute("""CREATE TRIGGER fail_jf_revision BEFORE INSERT ON events
          WHEN NEW.kind='jellyfin_revision_baseline_updated' BEGIN SELECT RAISE(ABORT,'interrupted'); END""")
        self.b.db.commit();before=self.env.snapshot()
        with self.assertRaises(Exception):self.env.prepare()
        self.assertEqual(before,self.env.snapshot());self.assert_no_writes()

    def test_stale_repair_journal_failure_rolls_back_baseline(self):
        self.make_stale()
        self.b.db.execute("""CREATE TRIGGER fail_jf_repair BEFORE INSERT ON events
          WHEN NEW.kind='jellyfin_revision_baseline_updated' BEGIN SELECT RAISE(ABORT,'interrupted'); END""")
        self.b.db.commit();before=self.env.snapshot()
        with self.assertRaises(Exception):self.env.prepare()
        self.assertEqual(before,self.env.snapshot());self.assert_no_writes()

    def test_new_playback_change_keeps_the_normal_settle_window(self):
        value=copy.deepcopy(self.original);value['candidate']={'signature':'old','since':1}
        self.b.save(self.key,value);self.jf.data['PlaybackPositionTicks']=45*TICKS
        self.env.change_revision();result=self.env.cycle(settle=30)
        self.assertFalse(result['blocked'],result);self.assertEqual(result['waiting'],1)
        self.assertEqual(self.b.load(self.key)['baseline']['abs']['position'],25)
        self.assert_no_writes()
        self.env.clock+=30;self.env.gate();result=self.env.cycle(settle=30)
        self.assertFalse(result['blocked'],result);self.assertEqual(result['synced'],1)
        self.assertEqual(self.env.abs.raw['currentTime'],45)
        self.assertEqual(self.env.bo.raw['positionMs'],45000)

    def test_apply_never_trusts_injected_preview_baseline(self):
        self.make_stale()
        self.env.bo.raw['manifestRevision']='b'*64
        self.b.db.execute('DELETE FROM mapping_revision_evidence');self.b.db.commit()
        mapping=self.b.list_mappings()[0]
        mapping['_jellyfin_revision_preview']=copy.deepcopy(self.original)
        for side in ('abs','bo'):
            mapping['_jellyfin_revision_preview']['baseline'][side]['fingerprint']=mapping['fingerprint']
        pair,raw=self.b.read_pair(mapping)
        with self.assertRaisesRegex(ValueError,'Source identity changed'):
            AudioCoordinator(self.b,self.jf).handle(mapping,pair,raw,True,0)
        self.assert_no_writes()
