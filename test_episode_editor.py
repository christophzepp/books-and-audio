import unittest
import copy
from unittest.mock import Mock, patch
from episode_editor import candidate, ReviewError

class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.p=Mock()
        self.p._ensure_native.return_value=3
        self.m=dict(feed_url='http://local/feed',enclosure_url='http://local/ep',show='Sendung',title='Titel')
        self.pods=[dict(feedurl='http://local/feed',podcastid=2,podcastname='Sendung')]
    def search(self,rows,old=None):
        self.p._request.side_effect=[{'pods':self.pods},{'total':len(rows),'episodes':rows}]
        return candidate(self.p,self.m,old)
    def test_ambiguous_title_in_same_feed_never_selects_first(self):
        self.assertIsNone(self.search([dict(Episodeid=i,Episodeurl='http://different/'+str(i),Episodetitle='Titel') for i in (1,2)]))
    def test_changed_feed_requires_independent_episode_binding(self):
        self.pods[0]['feedurl']='http://new/feed'
        self.assertIsNone(self.search([dict(Episodeid=5,Episodeurl='http://new/ep',Episodetitle='Titel')]))
        self.assertEqual(self.search([dict(Episodeid=5,Episodeurl='http://local/ep',Episodetitle='Titel')])['native_episode_id'],5)
    def test_earlier_id_cannot_be_replaced_by_matching_title(self):
        self.assertIsNone(self.search([dict(Episodeid=5,Episodeurl='http://local/ep',Episodetitle='Titel')],9))
    def test_incomplete_inventory_never_selects(self):
        self.p._request.side_effect=[{'pods':self.pods},{'total':9,'episodes':[]}]
        with self.assertRaises(ReviewError):candidate(self.p,self.m)

class AssignedStatusTests(unittest.TestCase):
    from test_podcast_status_sync import StatusTests as fixture
    setUp=fixture.setUp
    write_gate=fixture.write_gate
    load=fixture.load
    save=fixture.save
    write_record=fixture.write_record
    setup_variant=fixture.setup_variant
    cycle=fixture.cycle
    okay=fixture.okay
    advance=fixture.advance
    settle_all=fixture.settle_all

    def test_mark_heard_and_reset_across_all_four_services(self):
        from podcast_status_sync import state_key
        from podcast_sync import fingerprint
        self.setup_variant()
        self.okay(self.cycle())
        key=state_key('_podcast:show:episode')
        for target in ('finished','not_started'):
            self.advance()
            state=self.load(key)
            state['reviewed_source']=dict(target=target,snapshot=fingerprint(state['raw']))
            self.save(key,state)
            self.settle_all()
            self.assertEqual({row['status'] for row in self.load(key)['baseline'].values()},{target})
            self.assertEqual(self.pine.completed,target=='finished')
            if target=='not_started':self.assertEqual(self.pine.position,0)

class QueueTests(unittest.TestCase):
    def test_preview_can_pass_episode_but_not_connection_change(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from cryptography.fernet import Fernet
        from admin_store import AdminStore
        with TemporaryDirectory() as td:
            key=Path(td)/'k';key.write_bytes(Fernet.generate_key());s=AdminStore(td,key)
            a=s.submit('episode.status_apply',{})['id'];b=s.submit('review.preview',{})['id']
            self.assertEqual(s.claim(preview_only=True)['id'],b)
            self.assertIsNone(s.claim(preview_only=True))
            self.assertEqual(s.claim()['id'],a)
            s.submit('connection.activate',{});s.submit('review.preview',{})
            self.assertIsNone(s.claim(preview_only=True))


class PersonalRouteReReviewTests(unittest.TestCase):
    from test_episode_review import EpisodeReviewTests as fixture
    setUp=fixture.setUp
    write_gate=fixture.write_gate
    load=fixture.load
    save=fixture.save
    write_record=fixture.write_record
    configure=fixture.configure

    def prepare(self,legacy=True):
        import episode_editor as editor
        from podcast_status_sync import StatusCoordinator,state_key,version_receipt
        from podcast_sync import fingerprint
        from podcast_variant_proof import VariantProof
        self.editor=editor;self.key='_podcast:show:episode'
        self.abs.data['media']['episodes'][0]['guid']='stable-local-guid'
        c,m,_=self.configure()
        self.native={}
        inspect=self.pine.inspect_episode_metadata
        def native(*args):
            value=inspect(*args);value['metadata']['episodetitle']=m['title']
            value.update(copy.deepcopy(self.native))
            return value
        self.pine.inspect_episode_metadata=native
        self.inode_offset=0
        actual_source=VariantProof._source
        def source(checker,mapping):
            value=actual_source(checker,mapping)
            value['file']['inode']+=self.inode_offset
            return value
        self.addCleanup(patch.stopall)
        patch.object(VariantProof,'_source',source).start()
        self.route=dict(feed_url=m['feed_url'],enclosure_url=m['enclosure_url'],
            native_episode_id=9,native_user_id=7,native_podcast_id=5)
        proof=editor.route_proof(c,m,self.route)
        m['_pine_selected']=False;sc=StatusCoordinator(c);states,raw=sc.read(m,proof)
        self.note=dict(mode='personal_route',approved_by='user',route=copy.deepcopy(self.route),
                       binding=sc.binding(m,proof),version=version_receipt(proof))
        if not legacy:
            self.note.update(source_snapshot=copy.deepcopy(proof['source_snapshot']),
                             native_identity=copy.deepcopy(proof['native_identity']))
        self.status=dict(schema=2,binding=self.note['binding'],version=self.note['version'],baseline=states,raw=raw,enrolled=True)
        self.core=dict(mapping=fingerprint([m['fingerprint'],m['jf']['fingerprint']]),
                       baseline={k:states[k] for k in ('abs','jf')},pine_raw=None)
        self.save(self.key,self.core);self.save(state_key(self.key),self.status)
        self.save(editor.PREFIX+self.key,self.note);self.save('_podcast_accounts',c.accounts)
        self.inode_offset=100
        return c,m

    def assert_no_media_writes(self):
        self.assertFalse(self.abs.writes or self.jf.writes or self.pine.writes)

    def test_legacy_recheck_is_explicit_readonly_and_preserves_positions(self):
        from podcast_status_sync import state_key,StatusConflict
        c,m=self.prepare()
        before=list(self.db.execute('SELECT * FROM state ORDER BY id'))
        with self.assertRaises(StatusConflict):c.variant_proof(m,refresh=True,apply=False)
        row=self.editor.review(self.b,self.key)
        self.assertTrue(row['needs_identity_review']);self.assertIn('nicht vollständig belegbar',row['explanation'])
        self.assertEqual(before,list(self.db.execute('SELECT * FROM state ORDER BY id')))
        self.editor.review(self.b,self.key,record=row,source='pinepods')
        note=self.load(self.editor.PREFIX+self.key)
        self.assertNotEqual(note['binding'],self.note['binding'])
        self.assertEqual(note['binding'],self.load(state_key(self.key))['binding'])
        self.assertEqual(self.load(note['previous_approval_audit'])['previous_approval'],self.note)
        self.assertEqual(self.load(state_key(self.key))['raw'],self.status['raw'])
        self.assertNotIn('reviewed_source',self.load(state_key(self.key)))
        self.assertTrue(note['source_snapshot']);self.assertEqual(note['native_identity']['abs_guid'],'stable-local-guid')
        self.assertTrue(c.variant_proof(m,refresh=True,apply=False)['ready'])
        self.assert_no_media_writes()

    def test_changed_native_identity_or_missing_core_is_refused(self):
        self.prepare();self.native['episode_id']=10
        with self.assertRaises((ReviewError,ValueError)):self.editor.review(self.b,self.key)
        self.native.clear();self.save(self.key,{})
        with self.assertRaises(ReviewError):self.editor.review(self.b,self.key)
        self.assert_no_media_writes()

    def test_missing_historical_accounts_are_not_accepted_as_unchanged(self):
        self.prepare();self.save('_podcast_accounts',{})
        with self.assertRaises(ReviewError):self.editor.review(self.b,self.key)
        self.assert_no_media_writes()

    def test_saved_guid_may_not_be_replaced(self):
        self.prepare(legacy=False)
        self.abs.data['media']['episodes'][0]['guid']='different-guid'
        with self.assertRaises(ReviewError):self.editor.review(self.b,self.key)
        self.assert_no_media_writes()

    def test_changed_retired_actions_are_refused(self):
        from pine_identity_alias import source_history_evidence
        _,m=self.prepare()
        self.note['route']['old_actions']=source_history_evidence(m,[])
        self.save(self.editor.PREFIX+self.key,self.note)
        self.pine.actions=Mock(return_value={'actions':[]})
        self.editor.review(self.b,self.key)
        self.pine.actions.return_value={'actions':[dict(podcast=m['feed_url'],episode=m['enclosure_url'],
            timestamp='2033-05-18T03:33:20',action='new',device='phone')]}
        with self.assertRaises((ReviewError,ValueError)):self.editor.review(self.b,self.key)
        self.assert_no_media_writes()

    def test_new_source_progress_or_expired_preview_cannot_approve(self):
        self.prepare();row=self.editor.review(self.b,self.key)
        self.inode_offset+=1
        with self.assertRaises(ReviewError):self.editor.review(self.b,self.key,record=row,source='pinepods')
        self.inode_offset-=1;self.pine.set(600,'finished')
        with self.assertRaises(ReviewError):self.editor.review(self.b,self.key,record=row,source='pinepods')
        row=self.editor.review(self.b,self.key);self.clock+=151;self.gate['checked_at']=self.clock;self.write_gate()
        with self.assertRaises(ReviewError):self.editor.review(self.b,self.key,record=row,source='pinepods')
        self.assertEqual(self.load(self.editor.PREFIX+self.key),self.note);self.assert_no_media_writes()

    def test_active_playback_pending_and_commit_race_leave_approval_untouched(self):
        self.prepare();self.jf.active={'a'*32}
        with self.assertRaises(ReviewError):self.editor.review(self.b,self.key)
        self.jf.active.clear();row=self.editor.review(self.b,self.key)
        self.write_record('transfer',self.key,{},'pending')
        with self.assertRaises(ReviewError):self.editor.review(self.b,self.key,record=row,source='pinepods')
        self.write_record('transfer',self.key,{},'complete')
        calls=0
        def race():
            nonlocal calls
            calls+=1
            if calls==2:self.save(self.key,dict(self.core,changed_after_read=True))
            return set()
        with patch.object(self.jf,'active_ids',race):
            with self.assertRaises(ReviewError):self.editor.review(self.b,self.key,record=row,source='pinepods')
        self.assertEqual(self.load(self.editor.PREFIX+self.key),self.note)
        self.assertFalse(self.db.execute('SELECT 1 FROM state WHERE id LIKE ?', (self.editor.AUDIT_PREFIX+'%',)).fetchone())
        self.assert_no_media_writes()

    def prepare_measured(self,enrolled=True):
        import hashlib
        from podcast_variant_proof import VariantProof,digest
        from podcast_status_sync import StatusCoordinator,state_key,version_receipt
        from test_podcast_variant_proof import proof
        c,m=self.prepare(legacy=False)
        evidence=proof(duration=600,at=self.clock)
        evidence.pop('evidence_sha256')
        evidence['source_url_sha256']=hashlib.sha256(m['enclosure_url'].encode()).hexdigest()
        evidence['evidence_sha256']=digest(evidence)
        binding=dict(source=copy.deepcopy(self.note['source_snapshot']),native=copy.deepcopy(self.note['native_identity']))
        self.cache=dict(schema=1,stage='verified',binding=binding,own_duration=600,evidence=evidence,
            validated_at=self.clock,source_fingerprint=digest(binding),proof_fingerprint=digest(dict(binding=binding,own_duration=600)))
        self.cache_key='_pine_variant_proof:'+self.key
        self.save(self.cache_key,self.cache)
        checker=VariantProof(self.b)
        self.assertIsNotNone(checker._cache(self.cache,binding['source'],self.clock))
        old=checker._response(self.cache)
        self.status.update(binding=StatusCoordinator(c).binding(m,old),version=version_receipt(old))
        self.save(state_key(self.key),self.status if enrolled else {})
        self.db.execute('DELETE FROM state WHERE id=?',(self.editor.PREFIX+self.key,));self.db.commit()
        return c,m

    def test_measured_variant_recheck_requires_explicit_catalog_approval_without_probe(self):
        self.check_measured_recheck(True)

    def test_unenrolled_measured_variant_has_same_explicit_review_path(self):
        self.check_measured_recheck(False)

    def check_measured_recheck(self,enrolled):
        from podcast_variant_proof import VariantProof
        from podcast_status_sync import state_key
        c,m=self.prepare_measured(enrolled)
        before=list(self.db.execute('SELECT * FROM state ORDER BY id'))
        with patch.object(VariantProof,'_measure',side_effect=AssertionError('no media probe')):
            row=self.editor.review(self.b,self.key)
            self.assertTrue(row['needs_identity_review']);self.assertIn('keine neue Messung',row['explanation'])
            self.assertEqual(before,list(self.db.execute('SELECT * FROM state ORDER BY id')))
            self.editor.review(self.b,self.key,record=row,source='pinepods')
            note=self.load(self.editor.PREFIX+self.key)
            self.assertEqual(note['mode'],'reported')
            self.assertEqual(self.load(note['previous_approval_audit'])['previous_variant_proof'],self.cache)
            self.assertEqual(self.load(self.cache_key),self.cache)
            self.assertEqual(self.load(state_key(self.key))['raw'],self.status['raw'])
            self.assertTrue(c.variant_proof(m,refresh=True,apply=False)['ready'])
            self.assertFalse(self.editor.review(self.b,self.key)['needs_identity_review'])
            self.inode_offset+=1
            fresh=self.editor.review(self.b,self.key)
            self.assertTrue(fresh['needs_identity_review'])
            self.editor.review(self.b,self.key,record=fresh,source='pinepods')
            self.assertEqual(self.load(self.editor.PREFIX+self.key)['mode'],'reported')
            self.assertTrue(c.variant_proof(m,refresh=True,apply=False)['ready'])
        self.assert_no_media_writes()

    def test_measured_native_guid_and_old_proof_pins_are_not_bypassed(self):
        from podcast_status_sync import state_key
        self.prepare_measured()
        self.abs.data['media']['episodes'][0]['guid']='replacement-guid'
        with self.assertRaises(ReviewError):self.editor.review(self.b,self.key)
        self.abs.data['media']['episodes'][0]['guid']='stable-local-guid'
        changed=copy.deepcopy(self.cache);changed['own_duration']=601
        self.save(self.cache_key,changed)
        with self.assertRaises(ReviewError):self.editor.review(self.b,self.key)
        self.save(self.cache_key,self.cache)
        self.save(state_key(self.key),dict(self.status,binding='changed'))
        with self.assertRaises(ReviewError):self.editor.review(self.b,self.key)
        self.assert_no_media_writes()

    def test_status_edit_keeps_healthy_automatically_enrolled_variant_measured(self):
        from podcast_variant_proof import VariantProof
        c,m=self.prepare_measured();self.inode_offset=0
        with patch.object(VariantProof,'_measure',return_value=copy.deepcopy(self.cache['evidence'])):
            row=self.editor.review(self.b,self.key)
            self.assertFalse(row['needs_identity_review'])
            self.editor.review(self.b,self.key,record=row,source='pinepods')
            note=self.load(self.editor.PREFIX+self.key)
            self.assertEqual(note['mode'],'measured')
            self.assertEqual(note['source_snapshot'],self.cache['binding']['source'])
            self.assertTrue(c.variant_proof(m,refresh=True,apply=False)['ready'])
        self.assert_no_media_writes()
