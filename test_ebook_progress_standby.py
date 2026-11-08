"""Progress-only EPUB transactions use API evidence and NVMe derived indexes."""
import copy
from contextlib import contextmanager, ExitStack
from pathlib import Path
import os
from unittest.mock import patch
import unittest

from epub_index_cache import verified_bytes
from epub_locations import EpubLocations
from jellyfin_ebook import EbookCoordinator
from test_jellyfin_ebook import EbookTests


class EbookProgressStandbyTests(EbookTests):
    def setUp(self):
        super().setUp()
        self.b=self.c.b
        self.b.root=Path(self.tmp.name)/'nvme';self.b.root.mkdir(mode=0o700)
        self.b.config={}
        self.media=Path(self.tmp.name)/'hdd';self.media.mkdir()
        target=self.media/self.path.name;self.path.rename(target);self.path=target
        self.m['local_path']=str(target)
        self.now=1000;self.b.clock=lambda:self.now
        self.physical=[];self.logical=[]
        self.b.media_guard=lambda kind:self.physical.append(kind)
        self.b.media_observation_guard=lambda kind:self.logical.append(kind)
        self.b.media_observation_generation=lambda kind:'same-ebook-mount'

    @contextmanager
    def no_media(self):
        """Permit NVMe cache I/O, reject any media lookup/content work."""
        physical=self.b.media_guard
        self.b.media_guard=lambda kind:(_ for _ in ()).throw(AssertionError('physical media guard'))
        stat=Path.stat;lstat=Path.lstat;opening=os.open
        def is_media(path):
            if not isinstance(path,(str,bytes,os.PathLike)):return False
            value=os.fsdecode(path)
            return value==str(self.media) or value.startswith(str(self.media)+'/')
        def checked_stat(path,*args,**kwargs):
            if is_media(path):raise AssertionError('media stat')
            return stat(path,*args,**kwargs)
        def checked_lstat(path,*args,**kwargs):
            if is_media(path):raise AssertionError('media lstat')
            return lstat(path,*args,**kwargs)
        def checked_open(path,*args,**kwargs):
            if is_media(path):raise AssertionError('media open')
            return opening(path,*args,**kwargs)
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(Path,'resolve',side_effect=AssertionError('media resolve')))
                stack.enter_context(patch.object(Path,'stat',checked_stat))
                stack.enter_context(patch.object(Path,'lstat',checked_lstat))
                stack.enter_context(patch('epub_index_cache.os.open',checked_open))
                stack.enter_context(patch('epub_index_cache.verified_bytes',side_effect=AssertionError('media hash/read')))
                stack.enter_context(patch.object(EpubLocations,'from_bytes',side_effect=AssertionError('index rebuild')))
                yield
        finally:self.b.media_guard=physical

    def restart_index(self):
        self.b.__dict__.pop('_ebook_index_cache',None);self.c=EbookCoordinator(self.b)

    def test_real_transfers_both_directions_after_restart_do_no_media_io(self):
        self.assertEqual(self.run_cycle(),'unchanged')
        proof=copy.deepcopy(self.store['_jellyfin_ebook:1']['media_proof'])
        self.physical.clear();self.now=2000;self.set_bo(3);self.restart_index()
        with self.no_media():self.assertEqual(self.run_cycle(),'synced')
        self.assertEqual(self.e.ticks_to_index(self.user['PlaybackPositionTicks']),3)
        self.now=3000;self.set_jf(5);self.restart_index()
        with self.no_media():self.assertEqual(self.run_cycle(),'synced')
        self.assertEqual(self.e.bo_to_index(self.progress['text']['cfi']),5)
        self.assertEqual(self.progress['koreader'],'keep')
        state=self.store['_jellyfin_ebook:1']
        self.assertEqual(state['media_proof'],proof)
        self.assertEqual(state['checkpoint']['schema'],2)
        self.assertEqual(state['checkpoint']['observed_at'],3000)
        self.assertNotIn('verified_at',state['checkpoint'])
        self.assertEqual(self.physical,[])
        self.assertGreaterEqual(len(self.logical),7)

    def test_first_admission_hashes_once_and_waiting_reuses_that_proof(self):
        self.set_bo(3)
        with patch('epub_index_cache.verified_bytes',wraps=verified_bytes) as check:
            self.assertEqual(self.c.handle(self.m,True,60),'waiting')
            self.assertEqual(check.call_count,1)
        self.now=1010
        with self.no_media():self.assertEqual(self.c.handle(self.m,True,60),'waiting')
        self.now=1060
        with self.no_media():self.assertEqual(self.c.handle(self.m,True,60),'synced')
        self.assertEqual(self.store['_jellyfin_ebook:1']['media_proof']['verified_at'],1000)

    def test_completed_and_reopened_status_transfers_do_no_media_io(self):
        self.run_cycle()
        with self.no_media():
            self.set_jf(5,True);self.assertEqual(self.run_cycle(),'synced')
            self.assertEqual(self.status['status'],'read')
            self.set_jf(2);self.assertEqual(self.run_cycle(),'synced')
            self.assertEqual(self.status['status'],'reading')
            self.set_bo(3,True);self.assertEqual(self.run_cycle(),'synced')
            self.assertTrue(self.user['Played'])

    def test_beta11_proof_migrates_without_renewing_original_physical_time(self):
        self.run_cycle();state=self.store['_jellyfin_ebook:1']
        proof=state.pop('media_proof')
        state['checkpoint']=dict(schema=1,binding=proof['binding'],verified_at=proof['verified_at'],
                                 raw_sha256=state['checkpoint']['raw_sha256'])
        self.now=5000;self.set_bo(3);self.restart_index()
        with self.no_media():self.assertEqual(self.run_cycle(),'synced')
        self.assertEqual(self.store['_jellyfin_ebook:1']['media_proof'],proof)
        self.assertEqual(self.store['_jellyfin_ebook:1']['checkpoint']['observed_at'],5000)

    def test_missing_index_requires_one_physical_rebuild_before_transfer(self):
        self.run_cycle()
        next((self.b.root/'ebook-index-cache').glob('*.json')).unlink()
        self.restart_index();self.now=2000;self.set_bo(3)
        with patch('epub_index_cache.verified_bytes',wraps=verified_bytes) as check:
            self.assertEqual(self.run_cycle(),'synced');self.assertEqual(check.call_count,1)
        self.assertEqual(self.store['_jellyfin_ebook:1']['media_proof']['verified_at'],2000)

    def test_corrupt_physical_proof_requires_validation_before_transfer(self):
        self.run_cycle();self.store['_jellyfin_ebook:1']['media_proof']['schema']=True
        self.set_bo(3)
        with patch('epub_index_cache.verified_bytes',wraps=verified_bytes) as check:
            self.assertEqual(self.run_cycle(),'synced');self.assertEqual(check.call_count,1)

    def test_corrupt_api_checkpoint_is_rebuilt_from_live_api_and_historical_proof(self):
        self.run_cycle();proof=copy.deepcopy(self.store['_jellyfin_ebook:1']['media_proof'])
        self.store['_jellyfin_ebook:1']['checkpoint']='corrupt observation'
        self.set_bo(3)
        with self.no_media():self.assertEqual(self.run_cycle(),'synced')
        self.assertEqual(self.store['_jellyfin_ebook:1']['media_proof'],proof)
        self.assertEqual(self.store['_jellyfin_ebook:1']['checkpoint']['schema'],2)

    def test_missing_index_after_remote_write_does_not_trigger_midtransaction_io(self):
        self.run_cycle();self.set_bo(3);original=self.jf.update
        def update(ident,values):
            original(ident,values)
            next((self.b.root/'ebook-index-cache').glob('*.json')).unlink()
            self.b.__dict__.pop('_ebook_index_cache',None)
        self.jf.update=update
        with self.no_media():self.assertEqual(self.run_cycle(),'synced')
        self.assertNotIn('pending',self.store['_jellyfin_ebook:1'])

    def test_pending_confirmed_goal_uses_fresh_api_without_replaying_or_media(self):
        self.run_cycle();self.set_bo(3);original=self.jf.update
        def update(ident,values):original(ident,values);raise RuntimeError('uncertain response')
        self.jf.update=update
        with self.no_media():
            with self.assertRaisesRegex(RuntimeError,'uncertain'):self.run_cycle()
        self.assertIn('pending',self.store['_jellyfin_ebook:1'])
        writes=list(self.writes);self.restart_index()
        with self.no_media():self.assertEqual(self.run_cycle(),'unchanged')
        self.assertEqual(self.writes,writes)
        self.assertNotIn('pending',self.store['_jellyfin_ebook:1'])

    def test_pending_missing_index_after_restart_never_rebuilds_media_implicitly(self):
        self.run_cycle();self.set_bo(3);original=self.jf.update
        def update(ident,values):original(ident,values);raise RuntimeError('uncertain response')
        self.jf.update=update
        with self.assertRaisesRegex(RuntimeError,'uncertain'):self.run_cycle()
        next((self.b.root/'ebook-index-cache').glob('*.json')).unlink()
        self.restart_index();writes=list(self.writes)
        with self.no_media():
            with self.assertRaisesRegex(RuntimeError,'cached-index review'):self.run_cycle()
        self.assertEqual(self.writes,writes)
        self.assertIn('pending',self.store['_jellyfin_ebook:1'])
        # A deliberate physical check can repair the disposable index without
        # replaying the uncertain remote write; normal recovery is API-only.
        with patch('epub_index_cache.verified_bytes',wraps=verified_bytes) as check:
            self.assertEqual(self.c.handle(self.m,False,0,force_media=True),'unchanged')
            self.assertEqual(check.call_count,1)
        with self.no_media():self.assertEqual(self.run_cycle(),'unchanged')
        self.assertEqual(self.writes,writes)

    def test_pending_missing_physical_proof_blocks_without_reading_media(self):
        self.run_cycle();self.set_bo(3);self.fail=True
        with self.assertRaisesRegex(RuntimeError,'uncertain'):self.run_cycle()
        self.store['_jellyfin_ebook:1'].pop('media_proof')
        self.fail=False;self.restart_index();writes=list(self.writes)
        with self.no_media():
            with self.assertRaisesRegex(RuntimeError,'physical-proof review'):self.run_cycle()
        self.assertEqual(self.writes,writes)
        self.assertIn('pending',self.store['_jellyfin_ebook:1'])

    def test_fresh_logical_guard_failure_blocks_cached_transfer(self):
        self.run_cycle();self.set_bo(3)
        self.b.media_observation_guard=lambda kind:(_ for _ in ()).throw(RuntimeError('deployment unavailable'))
        with self.no_media():
            with self.assertRaisesRegex(RuntimeError,'deployment unavailable'):self.run_cycle()
        self.assertEqual(self.writes,[])

    def test_concurrent_progress_change_still_blocks_without_media_io(self):
        self.run_cycle();self.set_bo(3);calls=[]
        def guard():
            calls.append(1)
            if len(calls)==2:self.set_bo(4)
        self.b.guard=guard
        with self.no_media():
            with self.assertRaisesRegex(RuntimeError,'Concurrent EPUB progress'):self.run_cycle()
        self.assertEqual(self.writes,[])

    def test_api_source_version_change_before_write_blocks_cached_transfer(self):
        version=['old'];original=self.bo.detail
        self.bo.detail=lambda ident:dict(original(ident),files=[dict(original(ident)['files'][0],updatedAt=version[0])])
        self.run_cycle();self.set_bo(3);calls=[]
        def guard():
            calls.append(1)
            if len(calls)==2:version[0]='new'
        self.b.guard=guard
        with self.no_media():
            with self.assertRaisesRegex(RuntimeError,'source version changed'):self.run_cycle()
        self.assertEqual(self.writes,[])

    def test_api_source_version_change_during_final_request_keeps_pending_journal(self):
        version=['old'];original_detail=self.bo.detail
        self.bo.detail=lambda ident:dict(original_detail(ident),files=[dict(original_detail(ident)['files'][0],updatedAt=version[0])])
        self.run_cycle();self.set_bo(3);original_update=self.jf.update
        def update(ident,values):original_update(ident,values);version[0]='new'
        self.jf.update=update
        with self.no_media():
            with self.assertRaisesRegex(RuntimeError,'source version changed'):self.run_cycle()
        self.assertIn('pending',self.store['_jellyfin_ebook:1'])

    def test_manual_check_still_hashes_even_when_progress_and_version_unchanged(self):
        self.run_cycle();proof=copy.deepcopy(self.store['_jellyfin_ebook:1']['media_proof'])
        self.now=2000
        with patch('epub_index_cache.verified_bytes',wraps=verified_bytes) as check:
            self.assertEqual(self.c.handle(self.m,False,0,force_media=True),'unchanged')
            self.assertEqual(check.call_count,1)
        self.assertEqual(self.store['_jellyfin_ebook:1']['media_proof'],proof)

    def test_manual_rejected_alias_revokes_index_before_later_progress_transfer(self):
        self.path=self.path.resolve();self.m['local_path']=str(self.path)
        self.b.config={'folder_mappings':[dict(id='ebook',kind='ebooks',active=True,
            paths=dict(worker=str(self.path.parent),bo='/books',jf='/data/ebooks'))]}
        self.run_cycle()
        moved=self.path.with_name('same-content.epub');self.path.rename(moved);self.path.symlink_to(moved)
        with self.assertRaisesRegex(ValueError,'symlink'):
            self.c.handle(self.m,False,0,force_media=True)
        self.set_bo(3)
        with self.assertRaisesRegex(ValueError,'symlink'):self.run_cycle()
        self.assertEqual(self.writes,[])


if __name__=='__main__':unittest.main()
