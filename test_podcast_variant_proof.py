import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from test_pine_metadata_repair import Bridge,URL
from podcast_sync import build_mapping
from pine_metadata_repair import PineMetadataRepair
from podcast_variant_proof import VariantProof,NAMESPACE,digest,variant_probe_order


def proof(duration=650.25,at=1000,full=False):
    header={'known':True,'duration_seconds':duration,'source':'full_mp3_packets' if full else 'Xing_frame_count'}
    fetch={'bytes_read':65536,'fetched_at':dt.datetime.fromtimestamp(at,dt.timezone.utc).isoformat()}
    result={'known':True,'duration_seconds':duration,'header':header,'fetch':fetch,'full_stream_verified':False,
        'source_url_sha256':hashlib.sha256(URL.encode()).hexdigest(),'prefix_sha256':'f'*64}
    if full:
        header.update(packet_count=25000,full_demux_completed=True,duration_extrapolated=False,full_decode_checked=False)
        fetch.update(actual_bytes=65536,full_download=True,complete_body_read=True)
        result['full_audio_sha256']='e'*64
    result['evidence_sha256']=digest(result);return result


class VariantProofTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.b=Bridge(Path(self.temp.name).resolve());self.addCleanup(self.b.db.close)
        self.mapping=build_mapping(self.b.item,self.b.item['media']['episodes'][0],self.b.config['podcasts'])
        self.mapping['jf']={'id':'jf','size':1000,'duration':600.25}
        self.account={'base_url':'http://pinepods:8040','username':'reader','device_id':'book-progress-sync',
            'protocol':'gpodder-v2','server_version':'0.9.0','native_user_id':2,'completion_margin_seconds':60}
        self.b.pinepods.identity=copy.deepcopy(self.account);self.b.pinepods.value['account']=copy.deepcopy(self.account)
        self.b.save('_podcast_accounts',{'pinepods':self.account})
        self.source=PineMetadataRepair(self.b,probe=Mock())._source(self.mapping)
        seed=proof(at=990)
        self.seed={'schema':1,'stage':'deferred','source':self.source,'evidence':seed,'evidence_sha256':digest(seed)}
        self.b.save('_pine_metadata:'+self.mapping['id'],self.seed)
        self.probe=Mock(return_value=proof());self.helper=VariantProof(self.b,self.probe)
        self.key=NAMESPACE+self.mapping['id']

    def inspect(self,**kw):return self.helper.inspect(self.mapping,**kw)
    def states(self):return self.b.db.execute('SELECT id,value FROM state ORDER BY id').fetchall()

    def test_saved_measurement_requires_refresh_and_default_read_performs_no_probe_or_writes(self):
        before=self.states();result=self.inspect()
        self.assertFalse(result['ready']);self.assertEqual(result['reason'],'fresh_variant_proof_required')
        self.assertEqual(result['own_duration'],650.25);self.probe.assert_not_called();self.assertEqual(self.states(),before)

    def test_refresh_accepts_actual_variant_despite_wrong_rss_duration_and_writes_only_own_namespace(self):
        before=dict(self.states());result=self.inspect(refresh=True,apply=True)
        self.assertTrue(result['ready']);self.assertTrue(result['fresh']);self.assertEqual(result['own_duration'],650.25)
        self.assertEqual(result['native_episode_id'],7)
        after=dict(self.states());self.assertEqual({k:v for k,v in after.items() if k!=self.key},before)
        self.assertFalse(self.b.pinepods.posts)
        self.assertEqual(self.b.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],0)
        self.assertEqual(self.b.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)

    def test_preview_refresh_never_persists_and_does_not_modify_progress(self):
        before=self.states();result=self.inspect(refresh=True,apply=False)
        self.assertTrue(result['ready']);self.assertTrue(result['fresh']);self.assertEqual(self.states(),before)

    def test_cached_read_is_not_fresh_and_refresh_fingerprints_ignore_receipt_timestamp(self):
        first=self.inspect(refresh=True,apply=True);self.assertTrue(first['ready'])
        self.probe.reset_mock();cached=self.inspect();self.assertTrue(cached['ready']);self.assertFalse(cached['fresh'])
        self.probe.assert_not_called();self.b.time+=20;self.probe.return_value=proof(at=1020,duration=650.3)
        refreshed=self.inspect(refresh=True,apply=True);self.assertTrue(refreshed['fresh'])
        self.assertEqual(refreshed['proof_fingerprint'],first['proof_fingerprint'])
        self.assertEqual(refreshed['source_fingerprint'],first['source_fingerprint'])
        self.assertNotEqual(refreshed['evidence_sha256'],first['evidence_sha256'])

    def test_unknown_altered_equal_or_future_seed_never_calls_probe(self):
        variants=[]
        for key,value in [('known',False),('duration_seconds',600.25)]:
            seed=copy.deepcopy(self.seed);seed['evidence'][key]=value;seed['evidence_sha256']=digest(seed['evidence']);variants.append(seed)
        seed=copy.deepcopy(self.seed);seed['evidence']=proof(at=1001);seed['evidence_sha256']=digest(seed['evidence']);variants.append(seed)
        seed=copy.deepcopy(self.seed);seed['source']['file']['inode']+=1;variants.append(seed)
        for seed in variants:
            with self.subTest(seed=variants.index(seed)):
                self.b.save('_pine_metadata:'+self.mapping['id'],seed)
                self.assertFalse(self.inspect(refresh=True,apply=True)['ready'])
        self.probe.assert_not_called();self.assertFalse(self.b.load(self.key))

    def test_downloaded_changed_identity_account_or_alias_refuse_admission(self):
        original=copy.deepcopy(self.b.pinepods.value)
        for update in ({'downloaded':True},{'episode_id':8},{'user_id':3},{'account':dict(self.account,username='other')}):
            self.b.pinepods.value=dict(copy.deepcopy(original),**update)
            self.assertFalse(self.inspect(refresh=True,apply=True)['ready'])
        self.b.pinepods.value=original;self.b.save('_pine_alias:'+self.mapping['id'],{'enabled':True})
        self.assertEqual(self.inspect(refresh=True)['reason'],'variant_alias_requires_review')
        self.probe.assert_not_called()

    def test_metadata_intent_blocks_while_other_progress_is_never_reset(self):
        self.b.save(self.mapping['id'],{'pending':{'preserve':'all'},'baseline':{'abs':{'position':123}}})
        state=self.b.load(self.mapping['id']);record=copy.deepcopy(self.seed);record['stage']='intent'
        self.b.save('_pine_metadata:'+self.mapping['id'],record)
        self.assertEqual(self.inspect(refresh=True,apply=True)['reason'],'variant_metadata_intent_pending')
        self.assertEqual(self.b.load(self.mapping['id']),state);self.probe.assert_not_called()

    def test_source_or_native_changes_during_probe_never_create_verified_proof(self):
        def source_changes(*a,**kw):
            self.b.item['media']['episodes'][0]['audioFile']['metadata']['mtimeMs']+=10
            return proof()
        self.probe.side_effect=source_changes
        self.assertFalse(self.inspect(refresh=True,apply=True)['ready']);self.assertEqual(self.b.load(self.key)['stage'],'deferred')

    def test_native_identity_changes_during_probe_never_create_verified_proof(self):
        def native_changes(*a,**kw):
            self.b.pinepods.value['episode_id']=8
            self.b.pinepods.value['metadata']['episodeid']=8
            return proof()
        self.probe.side_effect=native_changes
        result=self.inspect(refresh=True,apply=True)
        self.assertEqual(result['reason'],'variant_native_changed_during_probe')
        self.assertEqual(self.b.load(self.key)['stage'],'deferred');self.assertFalse(self.b.pinepods.posts)

    def test_late_native_check_does_not_renew_media_receipt_or_authorize_stale_probe(self):
        real=self.b.pinepods.inspect_episode_metadata
        calls=0
        def slow_recheck(*a,**kw):
            nonlocal calls
            calls+=1
            if calls==2:self.b.time+=151
            return real(*a,**kw)
        self.b.pinepods.inspect_episode_metadata=slow_recheck
        result=self.inspect(refresh=True,apply=True)
        self.assertEqual(result['reason'],'variant_probe_receipt_stale')
        self.assertEqual(self.b.load(self.key)['stage'],'deferred')

    def test_validated_at_is_the_media_receipt_even_when_final_checks_take_time(self):
        real=self.b.pinepods.inspect_episode_metadata
        calls=0
        def slow_recheck(*a,**kw):
            nonlocal calls
            calls+=1
            if calls==2:self.b.time+=20
            return real(*a,**kw)
        self.b.pinepods.inspect_episode_metadata=slow_recheck
        result=self.inspect(refresh=True,apply=True)
        self.assertTrue(result['fresh']);self.assertEqual(result['validated_at'],1000)
        self.assertEqual(self.b.load(self.key)['validated_at'],1000)

    def test_unknown_refresh_backoff_keeps_prior_verified_identity_but_blocks_new_status_write(self):
        self.assertTrue(self.inspect(refresh=True,apply=True)['ready'])
        self.b.time+=5;self.probe.return_value={'known':False,'reason':'probe_timeout'}
        self.assertFalse(self.inspect(refresh=True,apply=True)['ready'])
        helper=VariantProof(self.b,self.probe);self.probe.reset_mock()
        self.assertEqual(helper.inspect(self.mapping,refresh=True,apply=True)['reason'],'variant_refresh_backoff')
        self.probe.assert_not_called();self.assertTrue(helper.inspect(self.mapping)['ready'])
        self.assertFalse(helper.inspect(self.mapping)['fresh'])

    def test_stale_receipt_never_authorizes(self):
        for value in (proof(at=800),proof(at=1001)):
            self.helper=VariantProof(self.b,self.probe);self.probe.return_value=value
            result=self.inspect(refresh=True,apply=False);self.assertFalse(result['ready'])
        self.assertFalse(self.b.load(self.key))

    def test_budget_is_shared_and_full_fallback_receives_original_full_proof(self):
        seed=copy.deepcopy(self.seed);seed['evidence']=proof(at=990,full=True);seed['evidence_sha256']=digest(seed['evidence'])
        self.b.save('_pine_metadata:'+self.mapping['id'],seed)
        self.probe.side_effect=[{'known':False,'reason':'mpeg_audio_without_frame_count_tag'},proof(full=True),proof(),proof()]
        first=self.inspect(refresh=True);self.assertTrue(first['ready'])
        calls=self.probe.call_args_list
        self.assertEqual(calls[0].kwargs['previous_evidence'],seed['evidence']);self.assertTrue(calls[1].kwargs['full_probe'])
        self.assertTrue(self.inspect(refresh=True)['ready'])
        self.assertEqual(self.inspect(refresh=True)['reason'],'variant_cycle_budget')
        self.assertEqual(self.probe.call_count,3)

    def test_cached_proof_or_source_tamper_requires_review_and_no_probe(self):
        self.assertTrue(self.inspect(refresh=True,apply=True)['ready']);record=self.b.load(self.key)
        record['own_duration']=999;self.b.save(self.key,record);self.probe.reset_mock()
        self.assertFalse(self.inspect(refresh=True,apply=True)['ready']);self.probe.assert_not_called()

    def test_abs_guid_changes_require_review_even_with_same_file_and_urls(self):
        self.assertTrue(self.inspect(refresh=True,apply=True)['ready'])
        self.assertIn('abs_guid',self.b.load(self.key)['binding']['native'])
        self.b.item['media']['episodes'][0]['guid']='replacement-guid'
        self.probe.reset_mock()
        self.assertFalse(self.inspect(refresh=True,apply=True)['ready']);self.probe.assert_not_called()
        changed=dict(self.mapping,abs_guid='replacement-guid')
        self.assertEqual(self.helper.inspect(changed,refresh=True,apply=True)['reason'],'variant_native_identity_changed')

    def test_dynamic_duration_with_pending_is_throttled_without_overwriting_existing_proof(self):
        self.assertTrue(self.inspect(refresh=True,apply=True)['ready']);original=self.b.load(self.key)
        self.b.save('_podcast_status:'+self.mapping['id'],{'pending':{'keep':'old plan'}})
        self.probe.return_value=proof(duration=700)
        failed=self.inspect(refresh=True,apply=True)
        self.assertEqual(failed['reason'],'variant_pending_proof_pin')
        stored=self.b.load(self.key)
        for key in ('stage','binding','own_duration','proof_fingerprint','evidence'):
            self.assertEqual(stored[key],original[key])
        self.assertEqual(stored['next_attempt_at'],1900)
        self.helper=VariantProof(self.b,self.probe);self.probe.reset_mock()
        self.assertEqual(self.inspect(refresh=True,apply=True)['reason'],'variant_refresh_backoff')
        self.assertEqual(self.helper.remaining,2);self.probe.assert_not_called()

    def test_fair_order_prioritizes_pending_then_untried_then_oldest_and_preserves_other_order(self):
        mappings=[dict(self.mapping,id='_podcast:show:'+str(i)) for i in range(8)]
        for i in (0,1,3,4,6):
            self.b.save('_pine_metadata:'+mappings[i]['id'],self.seed)
        for i,when in ((0,100),(1,200),(6,100)):
            self.b.save(NAMESPACE+mappings[i]['id'],dict(stage='deferred',last_attempt_at=when))
        self.b.save('_podcast_status:'+mappings[5]['id'],{'pending':{'id':'existing'}})
        before=self.states()
        result=variant_probe_order(self.b,mappings)
        self.assertEqual([m['id'] for m in result],[mappings[i]['id'] for i in (5,3,4,0,6,1,2,7)])
        self.assertEqual(self.states(),before)
        self.assertEqual(mappings[0]['id'],'_podcast:show:0')

    def test_two_dynamic_failures_move_behind_remaining_ninety_five_candidates(self):
        mappings=[dict(self.mapping,id='_podcast:show:'+str(i)) for i in range(97)]
        for mapping in mappings:self.b.save('_pine_metadata:'+mapping['id'],self.seed)
        for mapping in mappings[:2]:
            self.helper._failed_attempt(NAMESPACE+mapping['id'],None,'exact-pinned-source',
                                        'variant_remote_duration_changed',True)
        result=variant_probe_order(self.b,mappings)
        self.assertEqual(result[:95],mappings[2:])
        self.assertEqual(result[95:],mappings[:2])
        self.assertEqual(self.b.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],0)

    def priority_scenario(self):
        # Scheduling alone is under test: exact source/native validation has
        # separate tests above; retain real proof, budget and persistence code.
        mappings=[dict(self.mapping,id='_podcast:show:new'+str(i)) for i in range(2)]+[self.mapping]
        sources={}
        for mapping in mappings:
            source=copy.deepcopy(self.source);source['mapping']['id']=mapping['id'];sources[mapping['id']]=source
            self.b.save('_pine_metadata:'+mapping['id'],dict(self.seed,source=source))
        self.probe.side_effect=lambda *args,**kwargs:proof(at=self.b.time)
        def factory():
            helper=VariantProof(self.b,self.probe)
            helper._source=lambda mapping:sources[mapping['id']]
            return helper
        return mappings,factory

    def test_existing_changed_status_gets_slot_before_two_new_admissions(self):
        mappings,factory=self.priority_scenario()
        self.assertTrue(factory().inspect(self.mapping,refresh=True,apply=True)['ready'])
        self.b.save('_podcast_status:'+self.mapping['id'],{'enrolled':True,'baseline':{'kept':'untouched'}})
        before=self.states();ordered=variant_probe_order(self.b,mappings)
        self.assertEqual(self.states(),before);self.assertEqual(ordered[0],self.mapping)
        helper=factory();self.probe.reset_mock();results={}
        for mapping in ordered:results[mapping['id']]=helper.inspect(mapping,refresh=True,apply=True)
        self.assertTrue(results[self.mapping['id']]['ready'])
        self.assertTrue(results[mappings[0]['id']]['ready'])
        self.assertEqual(results[mappings[1]['id']]['reason'],'variant_cycle_budget')
        self.assertEqual(self.probe.call_count,2);self.assertFalse(self.b.pinepods.posts)
        self.assertEqual(self.b.load('_podcast_status:'+self.mapping['id'])['baseline'],{'kept':'untouched'})
        self.assertEqual(self.b.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],0)

    def test_unchanged_enrolled_cached_read_leaves_both_slots_for_new_admissions(self):
        mappings,factory=self.priority_scenario()
        self.assertTrue(factory().inspect(self.mapping,refresh=True,apply=True)['ready'])
        self.b.save('_podcast_status:'+self.mapping['id'],{'enrolled':True})
        helper=factory();self.probe.reset_mock()
        for mapping in variant_probe_order(self.b,mappings):
            result=helper.inspect(mapping,refresh=mapping['id']!=self.mapping['id'],apply=True)
            self.assertTrue(result['ready'],result)
        self.assertEqual(self.probe.call_count,2);self.assertFalse(self.b.pinepods.posts)

    def test_three_enrolled_changes_rotate_by_oldest_attempt_with_two_slots(self):
        mappings,factory=self.priority_scenario();self.b.time=1000
        for mapping in mappings:
            self.assertTrue(factory().inspect(mapping,refresh=True,apply=True)['ready'])
            self.b.save('_podcast_status:'+mapping['id'],{'enrolled':True})
        attempts=[]
        for cycle in range(2):
            self.b.time=1100+cycle*60;helper=factory();initial=self.probe.call_count
            for mapping in variant_probe_order(self.b,mappings):
                calls=self.probe.call_count;helper.inspect(mapping,refresh=True,apply=True)
                if self.probe.call_count>calls:attempts.append(mapping['id'])
            self.assertEqual(self.probe.call_count-initial,2)
        self.assertEqual(set(attempts[:3]),{mapping['id'] for mapping in mappings})
        self.assertFalse(self.b.pinepods.posts)

    def test_pending_precedes_enrolled_and_enrollment_is_only_an_order_hint(self):
        mappings,factory=self.priority_scenario()
        self.b.save('_podcast_status:'+mappings[0]['id'],{'enrolled':True})
        self.b.save('_podcast_status:'+mappings[1]['id'],{'pending':{'id':'pinned'}})
        self.b.save('_pine_metadata:'+mappings[0]['id'],{})
        ordered=variant_probe_order(self.b,mappings)
        self.assertEqual(ordered[:2],[mappings[1],mappings[0]])
        self.probe.reset_mock();result=factory().inspect(mappings[0],refresh=True,apply=True)
        self.assertFalse(result['ready']);self.probe.assert_not_called()

    def test_ninety_seven_persistent_failures_all_receive_slots_across_forty_nine_cycles(self):
        mappings=[dict(self.mapping,id='_podcast:show:'+str(i)) for i in range(97)]
        sources={}
        for mapping in mappings:
            source=copy.deepcopy(self.source);source['mapping']['id']=mapping['id'];sources[mapping['id']]=source
            self.b.save('_pine_metadata:'+mapping['id'],dict(self.seed,source=source))
        self.probe.return_value={'known':False,'reason':'probe_timeout'}
        attempted=[]
        for cycle in range(49):
            self.b.time=1000+cycle*60
            helper=VariantProof(self.b,self.probe)
            # This regression exercises real attempt/backoff/budget/cache
            # logic, independently of the already-tested local file guard.
            helper._source=lambda mapping:sources[mapping['id']]
            initial_calls=self.probe.call_count
            for mapping in variant_probe_order(self.b,mappings):
                before=self.probe.call_count
                result=helper.inspect(mapping,refresh=True,apply=True)
                self.assertFalse(result['ready'])
                if self.probe.call_count>before:attempted.append(mapping['id'])
            self.assertLessEqual(self.probe.call_count-initial_calls,2)
        self.assertEqual(set(attempted),{mapping['id'] for mapping in mappings})
        self.assertEqual(len(set(attempted[:97])),97)
        self.assertEqual(self.b.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],0)
        self.assertFalse(self.b.pinepods.posts)

    def test_expired_cache_requires_fresh_measurement_and_can_renew_only_unchanged_identity(self):
        original=self.inspect(refresh=True,apply=True);self.assertTrue(original['ready'])
        self.b.time+=31*86400;self.helper=VariantProof(self.b,self.probe);self.probe.reset_mock()
        self.assertEqual(self.inspect()['reason'],'variant_cached_evidence_expired');self.probe.assert_not_called()
        self.probe.return_value=proof(at=self.b.time)
        renewed=self.inspect(refresh=True,apply=True)
        self.assertTrue(renewed['fresh']);self.assertEqual(renewed['validated_at'],self.b.time)
        self.assertEqual(renewed['proof_fingerprint'],original['proof_fingerprint'])
        self.assertEqual(renewed['source_fingerprint'],original['source_fingerprint'])

    def test_expired_cache_cannot_repin_changed_duration_while_old_plan_is_pending(self):
        original=self.inspect(refresh=True,apply=True);self.assertTrue(original['ready'])
        self.b.time+=31*86400;self.helper=VariantProof(self.b,self.probe)
        self.b.save('_podcast_status:'+self.mapping['id'],{'pending':{'keep':'old plan'}})
        self.probe.return_value=proof(at=self.b.time,duration=720)
        self.assertEqual(self.inspect(refresh=True,apply=True)['reason'],'variant_pending_proof_pin')
        self.assertEqual(self.b.load(self.key)['validated_at'],original['validated_at'])
        self.assertEqual(self.b.load(self.key)['own_duration'],original['own_duration'])
        self.assertFalse(self.inspect()['ready'])

    def test_first_admission_uses_latest_real_measurement_not_candidate_seed_duration(self):
        seed=copy.deepcopy(self.seed);seed['evidence']=proof(at=990,duration=2870.520458)
        seed['evidence_sha256']=digest(seed['evidence']);self.b.save('_pine_metadata:'+self.mapping['id'],seed)
        self.probe.return_value=proof(duration=3171.321114)
        result=self.inspect(refresh=True,apply=True)
        self.assertTrue(result['fresh']);self.assertEqual(result['own_duration'],3171.321114)
        self.assertEqual(self.b.load(self.key)['evidence']['duration_seconds'],3171.321114)
        self.assertEqual(self.b.load('_pine_metadata:'+self.mapping['id']),seed)

    def test_duration_renewal_without_pending_preserves_identity_and_audits_complete_old_proof(self):
        first=self.inspect(refresh=True,apply=True);old=self.b.load(self.key);before=dict(self.states())
        self.b.time+=30;self.probe.return_value=proof(at=1030,duration=730)
        updated=self.inspect(refresh=True,apply=True)
        self.assertTrue(updated['fresh']);self.assertEqual(updated['own_duration'],730)
        self.assertEqual(updated['source_fingerprint'],first['source_fingerprint'])
        self.assertNotEqual(updated['proof_fingerprint'],first['proof_fingerprint'])
        self.assertEqual({k:v for k,v in dict(self.states()).items() if k!=self.key},
                         {k:v for k,v in before.items() if k!=self.key})
        rows=self.b.db.execute('SELECT book,kind,detail FROM events').fetchall();self.assertEqual(len(rows),1)
        key,kind,raw=rows[0];audit=json.loads(raw)
        self.assertEqual(key,self.key);self.assertEqual(kind,'pine_variant_measurement_renewed')
        self.assertEqual(audit['previous_measurement'],{key:old[key] for key in
            ('source_fingerprint','proof_fingerprint','own_duration','validated_at','evidence')})
        self.assertNotIn(URL,raw);self.assertNotIn('private=not-for-logs',raw)
        self.assertEqual(audit['next_own_duration'],730)
        self.assertEqual(audit['next_proof_fingerprint'],updated['proof_fingerprint'])
        self.assertEqual(self.b.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],0)
        self.assertFalse(self.b.pinepods.posts)

    def test_every_existing_pending_namespace_or_operation_pins_duration(self):
        self.assertTrue(self.inspect(refresh=True,apply=True)['ready']);old=self.b.load(self.key)
        keys=(self.mapping['id'],'_podcast_status:'+self.mapping['id'],'_pine_metadata:'+self.mapping['id'])
        for storage in ('state','operation'):
            for key in keys:
                with self.subTest(storage=storage,key=key):
                    self.b.save(self.key,old);saved=self.b.load(key)
                    if storage=='state':self.b.save(key,dict(saved,pending={'keep':'old plan'}))
                    else:
                        self.b.db.execute('INSERT INTO operations VALUES(?,?,?,?)',('old',key,'{}','pending'));self.b.db.commit()
                    self.helper=VariantProof(self.b,self.probe);self.probe.return_value=proof(duration=730)
                    self.assertFalse(self.inspect(refresh=True,apply=True)['ready'])
                    self.assertEqual(self.b.load(self.key)['own_duration'],old['own_duration'])
                    if storage=='state':self.b.save(key,saved)
                    else:self.b.db.execute("DELETE FROM operations WHERE id='old'");self.b.db.commit()
        self.assertEqual(self.b.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)

    def test_pending_same_duration_refresh_keeps_effect_fingerprint_unchanged(self):
        first=self.inspect(refresh=True,apply=True)
        self.b.save('_podcast_status:'+self.mapping['id'],{'pending':{'keep':'old plan'}})
        self.b.time+=10;self.probe.return_value=proof(at=1010,duration=650.3)
        result=self.inspect(refresh=True,apply=True)
        self.assertTrue(result['fresh']);self.assertEqual(result['own_duration'],first['own_duration'])
        self.assertEqual(result['proof_fingerprint'],first['proof_fingerprint'])
        self.assertEqual(self.b.load('_podcast_status:'+self.mapping['id']),{'pending':{'keep':'old plan'}})

    def test_pending_appearing_during_measurement_blocks_duration_renewal(self):
        self.assertTrue(self.inspect(refresh=True,apply=True)['ready']);old=self.b.load(self.key)
        def add_pending(*a,**kw):
            self.b.save(self.mapping['id'],{'pending':{'keep':'concurrent plan'}})
            return proof(duration=730)
        self.probe.side_effect=add_pending
        self.assertEqual(self.inspect(refresh=True,apply=True)['reason'],'variant_pending_proof_pin')
        self.assertEqual(self.b.load(self.key)['own_duration'],old['own_duration'])
        self.assertEqual(self.b.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)

    def test_failed_renewal_audit_rolls_back_new_cache_version(self):
        self.assertTrue(self.inspect(refresh=True,apply=True)['ready']);old=self.b.load(self.key)
        self.b.db.execute("CREATE TRIGGER refuse_audit BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'test refusal'); END")
        self.probe.return_value=proof(duration=730)
        self.assertFalse(self.inspect(refresh=True,apply=True)['ready'])
        current=self.b.load(self.key)
        for key in ('evidence','proof_fingerprint','own_duration'):self.assertEqual(current[key],old[key])
        self.assertEqual(self.b.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)

    def test_candidate_is_read_only_and_historical_variant_does_not_grant_fresh_readiness(self):
        self.b.time+=31*86400;before=self.states()
        self.assertTrue(self.helper.candidate(self.mapping));self.probe.assert_not_called()
        self.assertFalse(self.inspect()['ready']);self.assertEqual(self.states(),before)
        self.probe.return_value=proof(at=self.b.time,duration=600.25)
        result=self.inspect(refresh=True,apply=True)
        self.assertTrue(result['fresh']);self.assertEqual(result['own_duration'],600.25)
        self.assertEqual(self.b.load(self.key)['origin']['evidence'],self.seed['evidence'])
        self.assertEqual(self.b.load(self.key)['origin']['validated_at'],990)
        self.assertTrue(self.helper.candidate(self.mapping));self.assertTrue(self.inspect()['ready'])

    def test_equal_current_length_renews_known_variant_without_erasing_its_original_evidence(self):
        first=self.inspect(refresh=True,apply=True);origin=copy.deepcopy(self.b.load(self.key)['origin'])
        self.b.time+=10;self.probe.return_value=proof(at=1010,duration=600.25)
        result=self.inspect(refresh=True,apply=True)
        self.assertTrue(result['ready']);self.assertEqual(result['own_duration'],600.25)
        self.assertEqual(result['source_fingerprint'],first['source_fingerprint'])
        self.assertEqual(self.b.load(self.key)['origin'],origin)
        self.assertTrue(self.helper.candidate(self.mapping));self.assertTrue(self.inspect()['ready'])

    def test_candidate_rejects_unknown_equal_only_changed_source_and_tampered_origin(self):
        seed=copy.deepcopy(self.seed);seed['evidence']=proof(duration=600.25);seed['evidence_sha256']=digest(seed['evidence'])
        self.b.save('_pine_metadata:'+self.mapping['id'],seed)
        self.assertFalse(self.helper.candidate(self.mapping));self.probe.assert_not_called()
        self.b.save('_pine_metadata:'+self.mapping['id'],self.seed)
        self.assertTrue(self.inspect(refresh=True,apply=True)['ready']);record=self.b.load(self.key)
        record['origin']['source_fingerprint']='f'*64;self.b.save(self.key,record)
        self.probe.reset_mock();self.assertFalse(self.helper.candidate(self.mapping));self.assertFalse(self.inspect()['ready'])
        self.probe.assert_not_called()

    def test_candidate_rejects_unvalidated_records_and_source_drift_without_mutation_or_media_probe(self):
        variants=[{},dict(self.seed,evidence={'known':False}),dict(self.seed,evidence_sha256='0'*64)]
        for seed in variants:
            self.b.save('_pine_metadata:'+self.mapping['id'],seed);before=self.states()
            self.assertFalse(self.helper.candidate(self.mapping));self.assertEqual(self.states(),before)
        self.b.save('_pine_metadata:'+self.mapping['id'],self.seed)
        self.b.item['media']['episodes'][0]['audioFile']['metadata']['mtimeMs']+=10
        before=self.states();self.assertFalse(self.helper.candidate(self.mapping));self.assertEqual(self.states(),before)
        self.probe.assert_not_called();self.assertEqual(self.b.pinepods.reads,0)

    def test_native_service_outage_propagates_without_changing_pending_history_or_cache(self):
        from pinepods_client import RequestError
        self.assertTrue(self.inspect(refresh=True,apply=True)['ready'])
        self.b.save('_podcast_status:'+self.mapping['id'],{'pending':{'keep':'uncertain operation'}})
        before=self.states();self.probe.reset_mock()
        for error in (RequestError('PinePods: transport failure'),RequestError('PinePods: API request failed',401),
                      RequestError('PinePods: API request failed',403),RequestError('PinePods: API request failed',429),
                      RequestError('PinePods: API request failed',503)):
            self.b.pinepods.inspect_episode_metadata=Mock(side_effect=error)
            with self.subTest(status=error.status),self.assertRaises(RequestError):
                self.inspect(refresh=True,apply=True)
            self.assertEqual(self.states(),before)
        self.probe.assert_not_called()
        self.b.pinepods.inspect_episode_metadata=Mock(side_effect=RequestError('PinePods: invalid JSON response',200))
        self.assertEqual(self.inspect(refresh=True,apply=True)['reason'],'variant_proof_requires_review')


if __name__=='__main__':unittest.main()
