"""Native audiobook transfer uses current APIs and historical media evidence."""
import copy
import json
import unittest
from unittest.mock import patch
from bo_client import RequestError

import native_discovery
import sync
import test_native_discovery as fixtures
from test_sync import FakeABS, FakeBO, fixture


class NativeDurableIOTests(unittest.TestCase):
    setUp=fixtures.NativeEnrollmentTests.setUp
    request=fixtures.NativeEnrollmentTests.request
    cycle=fixtures.NativeEnrollmentTests.cycle
    accept=fixtures.NativeEnrollmentTests.accept
    count=fixtures.NativeEnrollmentTests.count

    def warm(self):
        self.accept();self.assertEqual(self.cycle()['unchanged'],1)
        self.hash_reads.clear()

    def trusted(self):
        return json.loads(self.bridge.db.execute(
            'SELECT proof FROM native_readalong_enrollments WHERE id=?',(self.mapping['abs_id'],)).fetchone()[0])

    def business_snapshot(self):
        return {name:self.bridge.db.execute('SELECT * FROM "'+name+'" ORDER BY 1').fetchall()
                for name in ('state','operations','native_readalong_enrollments','native_readalong_witnesses')}

    def approval_snapshot(self):
        return dict(baseline=copy.deepcopy(self.bridge.load(self.mapping['abs_id']).get('baseline')),
            approvals=self.bridge.db.execute('SELECT * FROM native_readalong_enrollments ORDER BY id').fetchall(),
            witnesses=self.bridge.db.execute('SELECT * FROM native_readalong_witnesses ORDER BY id').fetchall(),
            operations=self.bridge.db.execute('SELECT * FROM operations ORDER BY id').fetchall())

    def restart(self):
        self.bridge.db.close();self.bo._native_full_hash_cache={}
        with patch('sync.ABSClient',return_value=self.abs),patch('sync.BOClient',return_value=self.bo):
            self.bridge=sync.Bridge(self.root,self.config,dict(abs_token='dummy',bo_username='dummy',bo_password='dummy'),
                clock=lambda:self.clock)
        self.addCleanup(self.bridge.db.close)

    def test_restart_after_many_months_uses_nvme_history_without_download_hashes(self):
        self.warm();before=self.business_snapshot();self.restart();self.clock+=400*86400
        with patch.object(self.bo,'file_sha256',side_effect=AssertionError('unchanged source download')):
            self.assertEqual(self.cycle()['unchanged'],1)
        self.assertEqual(self.business_snapshot(),before)
        self.assertEqual(self.bridge._io_metrics.get('native_observation_durable_hits'),1)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def other_book(self):
        def base():
            ai,bd,bm=fixture();ai['id']='abs-other';ai['path']='/audiobooks/Other'
            ai['media']['tracks'][0]['metadata']['path']='/audiobooks/Other/01.mp3'
            bd['id']=24;bd['folderPath']='/audiobooks/Other';bd['files'][0]['absolutePath']='/audiobooks/Other/01.mp3'
            bm['book']['id']=24
            return ai,bd,bm
        mapping,ai,bd,bm,cap,job=fixtures.evidence(base);job['bookId']=24
        for row in bd['files'][1:]:row['absolutePath']=row['absolutePath'].replace('/Example/','/Other/')
        bo=FakeBO(bd,bm);bo.observation_root=self.root;reads=[]
        bo._request=lambda method,path,label,**kwargs:copy.deepcopy(cap if path.endswith('/read-aloud') else job)
        def sha(file_id,size):reads.append(file_id);return {19:'d'*64,20:'e'*64}[file_id]
        bo.file_sha256=sha
        trusted,_=native_discovery.collect(bo,mapping,ai,bd,bm)
        native_discovery.collect(bo,mapping,ai,bd,bm,trusted=trusted,cache_ok=True)
        reads.clear()
        return bo,mapping,ai,trusted,reads

    def test_changed_api_version_rehashes_only_affected_book_and_keeps_approval(self):
        self.warm();other,mapping,ai,trusted,reads=self.other_book();before=self.business_snapshot()
        self.bo.bd['files'][1]['contentVersion']='2026-10-31T02:00:00.000Z'
        self.assertEqual(self.cycle()['unchanged'],1);self.assertEqual(len(self.hash_reads),2)
        self.assertEqual(self.business_snapshot(),before)
        other._native_full_hash_cache={}
        native_discovery.collect(other,mapping,ai,other.detail(24),other.manifest(24),trusted=trusted,cache_ok=True)
        self.assertEqual(reads,[])

    def test_explicit_fresh_validation_never_trusts_a_warm_observation(self):
        self.warm();before=self.business_snapshot()
        self.bridge.verify_transfer_media(self.mapping,force_media=True)
        self.assertEqual({x[0] for x in self.hash_reads},{19,20});self.assertEqual(len(self.hash_reads),2)
        self.assertEqual(self.business_snapshot(),before);self.assertFalse(self.abs.writes or self.bo.writes)

    def test_hidden_file_replacement_is_deferred_until_explicit_media_check(self):
        self.warm();approvals=self.approval_snapshot()['approvals'];self.hashes[20]='c'*64
        self.abs.set(25)
        result=self.cycle()
        self.assertEqual(result['synced'],1)
        self.assertEqual(self.hash_reads,[])
        self.assertEqual(self.bo.raw['positionMs'],25000)
        self.assertEqual(self.approval_snapshot()['approvals'],approvals)
        before=self.approval_snapshot()
        with self.assertRaisesRegex(ValueError,'SHA256 changed'):
            self.bridge.verify_transfer_media(self.mapping,force_media=True)
        self.assertEqual(len(self.hash_reads),2)
        self.hash_reads.clear();self.restart()
        self.assertEqual(len(self.cycle()['blocked']),1)
        self.assertEqual(self.hash_reads,[])  # Known mismatch is not re-downloaded every idle cycle.
        self.assertEqual(self.approval_snapshot(),before)

    def test_untrusted_cached_hashes_cannot_replace_a_strong_approval(self):
        self.warm();before=self.approval_snapshot();self.bo._native_full_hash_cache={}
        from media_observation import ObservationStore
        original=ObservationStore.get
        def cached(store,namespace,*args):
            return {'source':'c'*64,'overlay':'a'*64} if namespace=='native-readalong' else original(store,namespace,*args)
        with patch.object(ObservationStore,'get',cached):
            result=self.cycle()
        self.assertEqual(len(result['blocked']),1)
        self.assertIn('SHA256 changed',result['blocked'][0]['reason'])
        self.assertEqual(self.approval_snapshot(),before)
        self.assertFalse(self.hash_reads or self.abs.writes or self.bo.writes)

    def test_successful_explicit_repair_replaces_negative_checkpoint_after_restart(self):
        self.warm();before=self.business_snapshot();self.hashes[20]='c'*64
        with self.assertRaisesRegex(ValueError,'SHA256 changed'):
            self.bridge.verify_transfer_media(self.mapping,force_media=True)
        self.assertEqual(len(self.hash_reads),2)
        self.hashes[20]='b'*64
        self.bridge.verify_transfer_media(self.mapping,force_media=True)
        self.assertEqual(len(self.hash_reads),4)
        self.assertEqual(self.business_snapshot(),before)
        self.restart();self.hash_reads.clear()
        with patch.object(self.bo,'file_sha256',side_effect=AssertionError('repaired checkpoint was not retained')):
            self.assertEqual(self.cycle()['unchanged'],1)
        self.assertEqual(self.business_snapshot(),before)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_service_binding_change_revalidates_only_its_checkpoint(self):
        self.warm();other,mapping,ai,trusted,reads=self.other_book();before=self.business_snapshot()
        for field,new_endpoint in (('bo_url','http://other-bookorbit'),('abs_url','http://other-audiobookshelf')):
            with self.subTest(field=field):
                self.hash_reads.clear();self.config[field]=new_endpoint
                self.assertEqual(self.cycle()['unchanged'],1)
                self.assertEqual(len(self.hash_reads),2)
                self.hash_reads.clear();self.assertEqual(self.cycle()['unchanged'],1)
                self.assertEqual(self.hash_reads,[])
        other._native_full_hash_cache={}
        native_discovery.collect(other,mapping,ai,other.detail(24),other.manifest(24),trusted=trusted,cache_ok=True)
        self.assertEqual(reads,[])
        self.assertEqual(self.business_snapshot(),before)

    def test_only_relevant_media_generation_requires_new_native_hashes(self):
        generations={'audiobooks':'audio-mount-1','ebooks':'ebook-mount-1'}
        self.bridge.media_observation_generation=lambda kind:generations[kind]
        self.warm();before=self.business_snapshot()
        generations['ebooks']='ebook-mount-2'
        self.assertEqual(self.cycle()['unchanged'],1);self.assertEqual(self.hash_reads,[])
        generations['audiobooks']='audio-mount-2'
        self.assertEqual(self.cycle()['unchanged'],1);self.assertEqual(len(self.hash_reads),2)
        self.hash_reads.clear();self.assertEqual(self.cycle()['unchanged'],1)
        self.assertEqual(self.hash_reads,[])
        self.assertEqual(self.business_snapshot(),before)

    def test_successful_transfer_reuses_original_witness_without_downloads(self):
        self.warm();self.abs.set(25)
        with patch.object(self.bo,'file_sha256',side_effect=AssertionError('progress-only media read')):
            self.assertEqual(self.cycle()['synced'],1)
        self.assertEqual(self.hash_reads,[])
        self.assertEqual(self.bo.raw['positionMs'],25000)
        self.assertTrue(self.bo.writes)

    def test_logical_write_check_has_no_physical_fallback_on_missing_cache(self):
        self.warm();before=self.business_snapshot()
        self.bo._native_full_hash_cache={}
        from media_observation import ObservationStore
        ObservationStore(self.root).forget('native-readalong',self.mapping['abs_id'])
        with patch.object(self.bo,'file_sha256',side_effect=AssertionError('logical fallback download')):
            with self.assertRaises(native_discovery.NativeMediaVerificationRequired):
                self.bridge.verify_transfer_media(self.mapping)
        self.assertEqual(self.business_snapshot(),before)

    def test_changed_api_identity_during_transfer_blocks_before_write_without_download(self):
        self.warm();self.abs.set(25);pair,raw=self.bridge.read_pair(self.mapping)
        self.bo.bd['files'][1]['contentVersion']='2026-10-31T02:00:00.000Z'
        with patch.object(self.bo,'file_sha256',side_effect=AssertionError('write-time source download')):
            with self.assertRaises(native_discovery.NativeMediaVerificationRequired):
                self.bridge.apply(self.mapping,pair,raw,'abs_to_bo',self.initial_state['baseline'])
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_logical_folder_guard_is_used_for_progress_transfer(self):
        self.warm();self.abs.set(25)
        from unittest.mock import Mock
        logical=Mock(return_value='unchanged-mount')
        self.bridge.media_observation_guard=logical
        self.bridge.media_guard=Mock(side_effect=AssertionError('physical folder check'))
        self.assertEqual(self.cycle()['synced'],1)
        self.assertGreaterEqual(logical.call_count,2)

    def test_definitive_original_failures_revoke_transfer_witness_across_restart(self):
        label='GET /api/v1/books/files/{file_id}/download: '
        failures=[RequestError(label+'HTTP 404',404),RequestError(label+'HTTP 410',410),
            RequestError(label+'invalid original-file response',200),
            RequestError(label+'invalid original-file body',200),
            RequestError(label+'original-file size changed',200),
            RequestError(label+'original-file body incomplete',200),None]
        for failure in failures:
            with self.subTest(failure=str(failure)):
                f=NativeDurableIOTests();f.setUp()
                try:
                    f.warm();before=f.approval_snapshot()
                    with patch.object(f.bo,'file_sha256',side_effect=failure,return_value=None):
                        with self.assertRaises((RequestError,ValueError)):
                            f.bridge.verify_transfer_media(f.mapping,force_media=True)
                    self.assertEqual(f.bo._native_full_hash_cache,{})
                    f.restart();f.abs.set(25)
                    with patch.object(f.bo,'file_sha256',side_effect=AssertionError('revoked proof must not rehash automatically')):
                        result=f.cycle()
                    self.assertEqual(len(result['blocked']),1)
                    self.assertFalse(f.abs.writes or f.bo.writes)
                    self.assertEqual(f.approval_snapshot(),before)
                finally:f.doCleanups()

    def test_authentication_transport_and_server_failures_preserve_media_witness(self):
        label='GET /api/v1/books/files/{file_id}/download: '
        failures=[RequestError(label+'HTTP '+str(code),code) for code in (401,403,429,500,503)]
        failures += [RequestError(label+'original-file transport failure'),
                     RequestError('POST /api/v1/auth/login: HTTP 404',404)]
        for failure in failures:
            with self.subTest(failure=str(failure)):
                f=NativeDurableIOTests();f.setUp()
                try:
                    f.warm();before=f.approval_snapshot()
                    with patch.object(f.bo,'file_sha256',side_effect=failure):
                        with self.assertRaises(RequestError):f.bridge.verify_transfer_media(f.mapping,force_media=True)
                    f.restart();f.abs.set(25)
                    with patch.object(f.bo,'file_sha256',side_effect=AssertionError('transient failure lost media witness')):
                        self.assertEqual(f.cycle()['synced'],1)
                    self.assertEqual(f.approval_snapshot()['approvals'],before['approvals'])
                finally:f.doCleanups()

    def test_failed_review_download_revokes_old_normal_transfer_witness(self):
        self.warm();before=self.approval_snapshot()
        error=RequestError('GET /api/v1/books/files/{file_id}/download: HTTP 404',404)
        with patch.object(self.bo,'file_sha256',side_effect=error):
            with self.assertRaises(RequestError):
                native_discovery.collect_for_review(self.bo,self.mapping,self.abs.item(self.mapping['abs_id']),
                                                    self.bo.detail(self.mapping['bo_id']),self.bo.manifest(self.mapping['bo_id']))
        self.restart();self.abs.set(25)
        with patch.object(self.bo,'file_sha256',side_effect=AssertionError('review failure must not rehash automatically')):
            self.assertEqual(len(self.cycle()['blocked']),1)
        self.assertEqual(self.approval_snapshot(),before)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_successful_explicit_verification_repairs_missing_original_revocation(self):
        self.warm();before=self.approval_snapshot()
        error=RequestError('GET /api/v1/books/files/{file_id}/download: HTTP 410',410)
        with patch.object(self.bo,'file_sha256',side_effect=error):
            with self.assertRaises(RequestError):self.bridge.verify_transfer_media(self.mapping,force_media=True)
        self.bridge.verify_transfer_media(self.mapping,force_media=True)
        self.assertEqual(len(self.hash_reads),2)
        self.restart();self.hash_reads.clear();self.abs.set(25)
        with patch.object(self.bo,'file_sha256',side_effect=AssertionError('repaired witness was not retained')):
            self.assertEqual(self.cycle()['synced'],1)
        self.assertEqual(self.approval_snapshot()['approvals'],before['approvals'])

    def test_successful_initial_byte_witness_can_clear_previous_admission_failure(self):
        from media_observation import ObservationStore
        self.warm();bo_id=self.mapping['bo_id'];abs_id=self.mapping['abs_id']
        args=(self.bo,self.mapping,self.abs.item(abs_id),self.bo.detail(bo_id),self.bo.manifest(bo_id))
        error=RequestError('GET /api/v1/books/files/{file_id}/download: HTTP 404',404)
        with patch.object(self.bo,'file_sha256',side_effect=error):
            with self.assertRaises(RequestError):native_discovery.collect(*args)
        native_discovery.collect(*args)
        path=ObservationStore(self.root).directory/ObservationStore(self.root)._name('native-revocation',abs_id)
        self.assertFalse(path.exists())

    def test_logical_cache_hits_do_not_rewrite_or_fsync_revocation_history(self):
        self.warm()
        from media_observation import ObservationStore
        with patch.object(ObservationStore,'forget',side_effect=AssertionError('idle cache hit rewrote state')):
            self.bridge.verify_transfer_media(self.mapping)


if __name__=='__main__':unittest.main()
