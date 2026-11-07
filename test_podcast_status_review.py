import copy
import unittest
from unittest.mock import patch

from podcast_sync import PodcastCoordinator, fingerprint, TICKS
from podcast_status_sync import (StatusCoordinator, reviewed_choice_matches,
                                 state_key, version_receipt)
import test_podcast_status_sync as fixtures


KEY='_podcast:show:episode'


class ReviewedStatusTests(unittest.TestCase):
    setUp=fixtures.StatusTests.setUp
    write_gate=fixtures.StatusTests.write_gate
    load=fixtures.StatusTests.load
    save=fixtures.StatusTests.save
    write_record=fixtures.StatusTests.write_record
    setup_variant=fixtures.StatusTests.setup_variant
    cycle=fixtures.StatusTests.cycle
    okay=fixtures.StatusTests.okay
    advance=fixtures.StatusTests.advance
    settle_all=fixtures.StatusTests.settle_all

    def prepare(self, *, peer=None, target='in_progress'):
        self.setup_variant()
        self.proof['own_duration']=4214
        self.pine.position=1414  # Native playback has no GPodder receipt yet.
        c=PodcastCoordinator(self.b)
        mapping=c.mappings()[0][0]
        mapping['_pine_selected']=False
        mapping['_variant_proof']=copy.deepcopy(self.proof)
        sc=StatusCoordinator(c)
        states,raw=sc.read(mapping,self.proof)
        binding=sc.binding(mapping,self.proof)
        decision=dict(snapshot=fingerprint(raw),**({'peer':peer} if peer else {'target':target}))
        stored=dict(schema=2,binding=binding,version=version_receipt(self.proof),
                    baseline=states,raw=raw,enrolled=True,reviewed_source=decision)
        self.save(state_key(KEY),stored)
        self.save(KEY,dict(mapping=fingerprint([mapping['fingerprint'],mapping['jf']['fingerprint']]),
                           baseline={key:states[key] for key in ('abs','jf')},pine_raw=None))
        self.save('_podcast_accounts',c.accounts)
        self.coordinator,self.mapping,self.sc=c,mapping,sc
        self.binding,self.stored=binding,copy.deepcopy(stored)
        return states,raw

    def read(self):
        return self.sc.read(self.mapping,self.proof)

    def matches(self, stored=None, states=None, raw=None, proof=None, binding=None):
        if states is None or raw is None:states,raw=self.read()
        return reviewed_choice_matches(stored or self.stored,states,raw,
            proof or self.proof,binding or self.binding)

    def test_reviewed_in_progress_survives_native_seconds_1414_to_1924_and_completes(self):
        self.prepare()
        self.pine.position=1924
        self.advance()
        self.settle_all()
        final=self.load(state_key(KEY))
        self.assertFalse(final.get('pending'))
        self.assertNotIn('reviewed_source',final)
        self.assertEqual({value['status'] for value in final['baseline'].values()},{'in_progress'})
        self.assertEqual(self.pine.position,1924)
        self.assertEqual(self.abs.raw['currentTime'],1)
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'],TICKS)

    def test_peer_pine_status_choice_accepts_positive_forward_and_backward_seek(self):
        self.prepare(peer='pinepods')
        for position in (1924,1000):
            self.pine.position=position
            self.assertTrue(self.matches())

    def test_derived_native_metadata_revisions_with_same_progress_are_safe(self):
        self.prepare()
        self.pine.observed_meta=1
        self.assertTrue(self.matches())

    def test_exact_choices_preserve_finished_reset_and_other_peer_compatibility(self):
        self.prepare()
        for choice in ({'target':'finished'},{'target':'not_started'},{'peer':'abs'},{'peer':'gpodder'}):
            stored=copy.deepcopy(self.stored)
            stored['reviewed_source']=dict(snapshot=fingerprint(stored['raw']),**choice)
            self.assertTrue(self.matches(stored))

    def test_fallback_never_applies_a_finished_reset_or_other_peer_choice(self):
        self.prepare()
        self.pine.position=1924
        for choice in ({'target':'finished'},{'target':'not_started'},{'peer':'abs'},{'peer':'jf'},{'peer':'gpodder'}):
            stored=copy.deepcopy(self.stored)
            stored['reviewed_source']=dict(snapshot=fingerprint(stored['raw']),**choice)
            self.assertFalse(self.matches(stored))

    def test_reset_completion_and_missing_native_history_still_refuse(self):
        self.prepare()
        for position,completed in ((0,False),(None,False),(1924,True)):
            self.pine.position,self.pine.completed=position,completed
            self.assertFalse(self.matches())

    def test_gpodder_new_reset_or_play_receipt_is_not_hidden_by_same_statuses(self):
        self.prepare()
        for field,value in (('gpodder_revision','changed'),('updated_at',int(self.clock)),
                            ('action','new'),('device','another-player'),
                            ('gpodder_raw_position',0),('gpodder_raw_total',4214)):
            states,raw=self.read()
            raw['pinepods'][field]=value
            self.assertFalse(self.matches(states=states,raw=raw),field)

    def test_any_peer_status_or_missing_change_still_refuses(self):
        states,raw=self.prepare()
        self.pine.position=1924
        states,raw=self.read()
        for peer in states:
            changed=copy.deepcopy(states);changed[peer]['status']='finished'
            self.assertFalse(self.matches(states=changed,raw=raw),peer)
            changed=copy.deepcopy(states);changed[peer]['missing']=not changed[peer].get('missing',False)
            self.assertFalse(self.matches(states=changed,raw=raw),peer)

    def test_other_peer_raw_metadata_cannot_sneak_through_native_position_exception(self):
        self.prepare();self.pine.position=1924
        states,raw=self.read()
        changed=copy.deepcopy(raw);changed['jf']['LastPlayedDate']='2033-05-18T03:33:21Z'
        self.assertFalse(self.matches(states=states,raw=changed))
        changed=copy.deepcopy(raw);changed['abs']={'currentTime':0}
        self.assertFalse(self.matches(states=states,raw=changed))

    def test_catalog_duration_and_unlisted_native_metadata_changes_still_refuse(self):
        self.prepare();self.pine.position=1924
        states,raw=self.read()
        for field,value in (('native_duration',460),('own_duration',4215),('extra_metadata','new')):
            changed=copy.deepcopy(raw);changed['pinepods'][field]=value
            self.assertFalse(self.matches(states=states,raw=changed),field)

    def test_binding_version_hash_and_native_identity_remain_required(self):
        self.prepare();self.pine.position=1924
        bad=copy.deepcopy(self.stored);bad['reviewed_source']['snapshot']='0'*64
        self.assertFalse(self.matches(bad))
        bad=copy.deepcopy(self.stored);bad['raw']['pinepods']['position']=1400
        self.assertFalse(self.matches(bad))
        self.assertFalse(self.matches(binding='different-binding'))
        for update in ({'own_duration':4215},{'proof_fingerprint':'another-version'}):
            proof=dict(self.proof,**update)
            self.assertFalse(self.matches(proof=proof))
        states,raw=self.read()
        for field in ('native_episode_id','native_user_id'):
            changed=copy.deepcopy(raw);changed['pinepods'][field]+=1
            self.assertFalse(self.matches(states=states,raw=changed))

    def test_missing_or_invalid_legacy_evidence_never_gets_semantic_reapproval(self):
        self.prepare()
        legacy=copy.deepcopy(self.stored);legacy.pop('baseline')
        self.assertTrue(self.matches(legacy))  # Old exact-snapshot behavior only.
        self.pine.position=1924
        self.assertFalse(self.matches(legacy))
        for field in ('raw','binding','version'):
            bad=copy.deepcopy(self.stored);bad.pop(field)
            self.assertFalse(self.matches(bad),field)
        for decision in ({'peer':'invalid'},{'target':'invalid'},
                         {'peer':'pinepods','target':'in_progress'}):
            bad=copy.deepcopy(self.stored)
            bad['reviewed_source']=dict(snapshot=fingerprint(bad['raw']),**decision)
            self.assertFalse(self.matches(bad))
        for field in ('native_revision','native_observed_revision','revision'):
            bad=copy.deepcopy(self.stored)
            bad['raw']['pinepods'].pop(field)
            bad['reviewed_source']['snapshot']=fingerprint(bad['raw'])
            self.assertFalse(self.matches(bad),field)

    def test_out_of_range_or_inconsistent_native_seconds_remain_invalid(self):
        self.prepare()
        states,raw=self.read()
        for value in (None,0,-1,True,float('nan'),4215):
            changed=copy.deepcopy(raw)
            changed['pinepods'].update(native_position=value,position=value)
            self.assertFalse(self.matches(states=states,raw=changed),repr(value))
        changed=copy.deepcopy(raw);changed['pinepods']['position']=1924
        self.assertFalse(self.matches(states=states,raw=changed))

    def test_strict_pending_preimage_still_refuses_new_native_seconds(self):
        self.prepare();self.abs.fail=True
        self.assertTrue(self.cycle()['blocked'])
        pending=copy.deepcopy(self.load(state_key(KEY))['pending'])
        count=len(self.abs.writes)
        self.abs.fail=False;self.pine.position=1924;self.advance()
        self.assertTrue(self.cycle()['blocked'])
        self.assertEqual(len(self.abs.writes),count)
        self.assertEqual(self.load(state_key(KEY))['pending'],pending)
        self.assertFalse(self.jf.writes or self.pine.writes)

    def test_strict_read_renewed_still_refuses_a_change_after_preplan_validation(self):
        self.prepare();self.pine.position=1924
        original=self.sc.read
        calls=[]
        def concurrent(mapping,proof):
            calls.append(True)
            if len(calls)==2:self.pine.position=2000
            return original(mapping,proof)
        with patch.object(self.sc,'read',side_effect=concurrent):
            with self.assertRaisesRegex(ValueError,'Several participants changed'):
                self.sc.handle(self.mapping,True,0)
        self.assertFalse(self.abs.writes or self.jf.writes or self.pine.writes)

    def test_reviewed_choice_cannot_adopt_a_new_version_during_preplan_refresh(self):
        self.prepare();self.pine.position=1924
        self.proof['fresh']=False
        self.next_proof=dict(own_duration=4264,proof_fingerprint='renewed-audio-version')
        result=self.cycle()
        self.assertTrue(result['blocked'])
        self.assertEqual(result['episodes'][0]['pine_status_reason_code'],'variant_identity_changed')
        self.assertFalse(self.abs.writes or self.jf.writes or self.pine.writes)
        stored=self.load(state_key(KEY))
        self.assertFalse(stored.get('pending'))
        self.assertEqual(stored['reviewed_source'],self.stored['reviewed_source'])
        self.assertEqual(stored['version'],self.stored['version'])


if __name__=='__main__':unittest.main()
