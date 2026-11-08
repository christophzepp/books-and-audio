"""Native audiobook evidence remains historical until a real transfer needs bytes."""
import copy
import json
import unittest
from unittest.mock import patch

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
        self.bridge.verify_transfer_media(self.mapping)
        self.assertEqual({x[0] for x in self.hash_reads},{19,20});self.assertEqual(len(self.hash_reads),2)
        self.assertEqual(self.business_snapshot(),before);self.assertFalse(self.abs.writes or self.bo.writes)

    def test_hidden_file_replacement_blocks_actual_transfer_without_changing_approvals(self):
        self.warm();before=self.approval_snapshot();self.hashes[20]='c'*64
        self.abs.set(25)
        result=self.cycle()
        self.assertEqual(len(result['blocked']),1)
        self.assertIn('SHA256 changed',result['blocked'][0]['reason'])
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertEqual(self.approval_snapshot(),before)
        self.assertEqual(len(self.hash_reads),2)
        self.hash_reads.clear();self.restart()
        self.assertEqual(len(self.cycle()['blocked']),1)
        self.assertEqual(self.hash_reads,[])  # Known mismatch is not re-downloaded every idle cycle.
        self.assertEqual(self.approval_snapshot(),before)

    def test_untrusted_cached_hashes_cannot_replace_a_strong_approval(self):
        self.warm();before=self.approval_snapshot();self.bo._native_full_hash_cache={}
        with patch('media_observation.ObservationStore.get',return_value={'source':'c'*64,'overlay':'a'*64}):
            result=self.cycle()
        self.assertEqual(len(result['blocked']),1)
        self.assertIn('SHA256 changed',result['blocked'][0]['reason'])
        self.assertEqual(self.approval_snapshot(),before)
        self.assertFalse(self.hash_reads or self.abs.writes or self.bo.writes)

    def test_successful_explicit_repair_replaces_negative_checkpoint_after_restart(self):
        self.warm();before=self.business_snapshot();self.hashes[20]='c'*64
        with self.assertRaisesRegex(ValueError,'SHA256 changed'):
            self.bridge.verify_transfer_media(self.mapping)
        self.assertEqual(len(self.hash_reads),2)
        self.hashes[20]='b'*64
        self.bridge.verify_transfer_media(self.mapping)
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

    def test_successful_transfer_verifies_originals_before_and_after_writes(self):
        self.warm();self.abs.set(25)
        self.assertEqual(self.cycle()['synced'],1)
        self.assertGreaterEqual(sum(file_id==19 for file_id,_ in self.hash_reads),2)
        self.assertGreaterEqual(sum(file_id==20 for file_id,_ in self.hash_reads),2)
        self.assertEqual(self.bo.raw['positionMs'],25000)
        self.assertTrue(self.bo.writes)


if __name__=='__main__':unittest.main()
