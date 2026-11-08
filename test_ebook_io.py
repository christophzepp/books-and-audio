import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from epub_index_cache import EpubIndexCache, verified_bytes
from epub_locations import EpubLocations
from jellyfin_ebook import EbookCoordinator
import test_jellyfin_ebook as fixtures


class EbookIOTests(fixtures.EbookTests):
    def setUp(self):
        super().setUp()
        self.b=self.c.b
        self.b.root=Path(self.tmp.name)/'ssd-state'
        self.b.root.mkdir(mode=0o700)
        self.b.config={}

    def test_idle_cycles_do_not_lookup_or_open_media_or_rebuild_index(self):
        self.assertEqual(self.run_cycle(),'unchanged')
        with patch('jellyfin_ebook.Path.resolve',side_effect=AssertionError('media lookup')), \
                patch('epub_index_cache.os.open',side_effect=AssertionError('media open')), \
                patch.object(EpubLocations,'from_bytes',side_effect=AssertionError('index rebuild')):
            for _ in range(100):
                # Each ordinary coordinator is new, while the bridge survives.
                self.c=EbookCoordinator(self.b)
                self.assertEqual(self.run_cycle(),'unchanged')
        self.assertFalse(self.writes)

    def test_restart_preserves_idle_checkpoint_without_media_access(self):
        self.run_cycle()
        self.b.__dict__.pop('_ebook_index_cache',None)
        self.c=EbookCoordinator(self.b)
        with patch('jellyfin_ebook.Path.resolve',side_effect=AssertionError('media lookup')):
            self.assertEqual(self.run_cycle(),'unchanged')

    def test_changed_progress_after_restart_uses_disk_index_and_current_hash(self):
        self.run_cycle()
        self.b.__dict__.pop('_ebook_index_cache',None)
        self.c=EbookCoordinator(self.b);self.set_bo(3)
        with patch.object(EpubLocations,'from_bytes',side_effect=AssertionError('index rebuilt')):
            self.assertEqual(self.run_cycle(),'synced')
        self.assertEqual(self.e.ticks_to_index(self.user['PlaybackPositionTicks']),3)

    def test_changed_same_size_original_blocks_even_with_valid_disk_index(self):
        self.run_cycle();self.b.__dict__.pop('_ebook_index_cache',None)
        data=self.path.read_bytes();self.path.write_bytes(data.replace(b'Hello',b'Other',1))
        self.set_bo(3)
        with self.assertRaisesRegex(ValueError,'changed'):self.run_cycle()
        self.assertFalse(self.writes)

    def test_source_edit_during_final_request_keeps_pending_journal(self):
        self.run_cycle();self.set_bo(3)
        original=self.jf.update
        def edit_after_update(ident,values):
            original(ident,values)
            data=self.path.read_bytes();self.path.write_bytes(data.replace(b'Hello',b'Other',1))
        self.jf.update=edit_after_update
        with self.assertRaisesRegex(ValueError,'changed'):self.run_cycle()
        self.assertIn('pending',self.store['_jellyfin_ebook:1'])

    def test_pending_journal_never_uses_idle_fast_path(self):
        self.run_cycle()
        self.store['_jellyfin_ebook:1']['pending']=dict(goal=dict(position=20,status='in_progress'))
        with patch('epub_index_cache.verified_bytes',side_effect=ValueError('required current source')):
            with self.assertRaisesRegex(ValueError,'required current source'):self.run_cycle()
        self.assertIn('pending',self.store['_jellyfin_ebook:1'])

    def test_invalid_checkpoint_timestamps_force_validation_when_audits_disabled(self):
        self.run_cycle()
        good=copy.deepcopy(self.store['_jellyfin_ebook:1'])
        for value in (None,True,False,'1000',float('nan'),float('inf'),float('-inf'),-1,1001):
            with self.subTest(value=value):
                self.store['_jellyfin_ebook:1']=copy.deepcopy(good)
                self.store['_jellyfin_ebook:1']['checkpoint']['verified_at']=value
                with patch('epub_index_cache.verified_bytes',wraps=verified_bytes) as check:
                    self.assertEqual(self.run_cycle(),'unchanged');self.assertEqual(check.call_count,1)
                self.assertEqual(self.store['_jellyfin_ebook:1']['checkpoint']['verified_at'],1000)
        self.store['_jellyfin_ebook:1']=copy.deepcopy(good)
        del self.store['_jellyfin_ebook:1']['checkpoint']['verified_at']
        with patch('epub_index_cache.verified_bytes',wraps=verified_bytes) as check:
            self.assertEqual(self.run_cycle(),'unchanged');self.assertEqual(check.call_count,1)

    def test_optional_integrity_verification_is_due_only_at_interval(self):
        clock=[1000];self.b.clock=lambda:clock[0]
        self.b.config={'io_policy':{'ebook_verify_interval_seconds':60}}
        self.run_cycle();clock[0]=1059
        with patch('jellyfin_ebook.Path.resolve',side_effect=AssertionError('not due')):
            self.assertEqual(self.run_cycle(),'unchanged')
        clock[0]=1060
        with patch('epub_index_cache.verified_bytes',wraps=verified_bytes) as check:
            self.assertEqual(self.run_cycle(),'unchanged');self.assertEqual(check.call_count,1)

    def test_corrupt_or_obsolete_cache_rebuilds_only_from_verified_source(self):
        self.run_cycle()
        file=next((self.b.root/'ebook-index-cache').glob('*.json'))
        for content in ('not json',json.dumps({'version':'old'})):
            file.write_text(content)
            self.b.__dict__.pop('_ebook_index_cache',None);self.set_bo(3)
            with patch.object(EpubLocations,'from_bytes',wraps=EpubLocations.from_bytes) as builder:
                self.assertEqual(self.run_cycle(),'synced');self.assertEqual(builder.call_count,1)
            self.set_bo(0);self.run_cycle()

    def test_cache_checksum_and_internal_node_bounds_are_checked(self):
        self.run_cycle();file=next((self.b.root/'ebook-index-cache').glob('*.json'))
        row=json.loads(file.read_text());row['data']['locations'][0][0][3]=9999999
        raw=json.dumps(row['data'],sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        row['data_sha256']=hashlib.sha256(raw).hexdigest();file.write_text(json.dumps(row))
        self.b.__dict__.pop('_ebook_index_cache',None);self.set_bo(3)
        with patch.object(EpubLocations,'from_bytes',wraps=EpubLocations.from_bytes) as builder:
            self.assertEqual(self.run_cycle(),'synced');self.assertEqual(builder.call_count,1)

    def test_api_content_version_change_invalidates_unchanged_checkpoint(self):
        self.run_cycle();original=self.bo.detail
        self.bo.detail=lambda ident:dict(original(ident),files=[dict(original(ident)['files'][0],updatedAt='new')])
        with patch('epub_index_cache.verified_bytes',wraps=verified_bytes) as check:
            self.assertEqual(self.run_cycle(),'unchanged');self.assertEqual(check.call_count,1)

    def test_mount_generation_change_or_missing_evidence_disables_idle_shortcut(self):
        generation=['mount-1'];self.b.media_generation=lambda kind:generation[0]
        self.run_cycle();generation[0]='mount-2'
        with patch('epub_index_cache.verified_bytes',wraps=verified_bytes) as check:
            self.assertEqual(self.run_cycle(),'unchanged');self.assertEqual(check.call_count,1)
        def missing(kind):raise ValueError('generation unavailable')
        self.b.media_generation=missing
        with patch('epub_index_cache.verified_bytes',wraps=verified_bytes) as check:
            self.assertEqual(self.run_cycle(),'unchanged')
            self.assertEqual(self.run_cycle(),'unchanged')
            self.assertEqual(check.call_count,2)

    def test_changed_mapping_does_not_reuse_checkpoint(self):
        self.run_cycle();self.m=dict(self.m,local_path='/missing/test.epub')
        with self.assertRaises(OSError):self.run_cycle()
        self.assertFalse(self.writes)

    def test_media_guard_runs_only_for_file_work_and_before_writes(self):
        calls=[];self.b.media_guard=lambda kind:calls.append(kind)
        self.run_cycle();self.assertEqual(calls,['ebooks']);calls.clear()
        self.run_cycle();self.assertEqual(calls,[])
        self.set_bo(3);self.run_cycle();self.assertGreaterEqual(len(calls),3)

    def test_serialized_index_has_identical_conversions(self):
        data=json.loads(json.dumps(self.e.cache_data()))
        loaded=EpubLocations.from_cache_data(data,self.e.hash)
        for index in range(self.e.total+1):
            self.assertEqual(loaded.index_to_bo(index),self.e.index_to_bo(index))
            self.assertEqual(loaded.bo_to_index(loaded.index_to_bo(index)[0]),index)
            self.assertEqual(loaded.ticks_to_index(loaded.index_to_ticks(index)),index)

    def test_disk_cache_obeys_configured_byte_budget(self):
        cache=EpubIndexCache(self.b.root/'tiny-cache',max_bytes=100)
        cache.get(self.path,self.m['sha256'],self.m['size'])
        self.assertEqual(list((self.b.root/'tiny-cache').glob('*.json')),[])

    def test_seed_reuses_validated_index_but_never_skips_current_source_hash(self):
        cache=EpubIndexCache(self.b.root/'seed-cache');cache.seed(self.e)
        with patch.object(EpubLocations,'from_bytes',side_effect=AssertionError('rebuild')):
            self.assertEqual(cache.get(self.path,self.m['sha256'],self.m['size']).hash,self.e.hash)
        data=self.path.read_bytes();self.path.write_bytes(data.replace(b'Hello',b'Other',1))
        with self.assertRaisesRegex(ValueError,'changed'):
            cache.get(self.path,self.m['sha256'],self.m['size'])

    def test_parent_rename_then_symlink_to_same_inode_during_read_is_rejected(self):
        parent=Path(self.tmp.name)/'source';parent.mkdir()
        source=parent/'book.epub';source.write_bytes(self.path.read_bytes())
        moved=Path(self.tmp.name)/'renamed-source'
        real_fdopen=os.fdopen
        class SwappingSource:
            def __init__(self,stream):self.stream=stream
            def __enter__(self):return self
            def __exit__(self,*args):return self.stream.__exit__(*args)
            def fileno(self):return self.stream.fileno()
            def read(self):
                data=self.stream.read()
                parent.rename(moved);parent.symlink_to(moved,target_is_directory=True)
                return data
        with patch('epub_index_cache.os.fdopen',side_effect=lambda *a,**kw:SwappingSource(real_fdopen(*a,**kw))):
            with self.assertRaisesRegex(ValueError,'changed during verification'):
                verified_bytes(source,self.m['sha256'],self.m['size'])
        # This is the same original inode; a final stat() alone cannot catch it.
        self.assertEqual(source.stat().st_ino,(moved/'book.epub').stat().st_ino)

    def test_stable_initial_parent_alias_is_allowed_but_leaf_symlink_is_refused(self):
        alias=Path(self.tmp.name)/'stable-alias'
        alias.symlink_to(self.path.parent,target_is_directory=True)
        self.assertEqual(hashlib.sha256(verified_bytes(alias/self.path.name,self.m['sha256'],self.m['size'])).hexdigest(),self.e.hash)
        leaf=Path(self.tmp.name)/'leaf.epub';leaf.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError,'symlink'):
            verified_bytes(leaf,self.m['sha256'],self.m['size'])

    def test_metrics_count_idle_skips_without_additional_source_reads(self):
        self.run_cycle();before=copy.deepcopy(self.b._io_metrics)
        for _ in range(4):self.run_cycle()
        self.assertEqual(self.b._io_metrics['ebook_source_reads'],before['ebook_source_reads'])
        self.assertEqual(self.b._io_metrics['ebook_source_read_bytes'],before['ebook_source_read_bytes'])
        self.assertEqual(self.b._io_metrics['ebook_idle_skips'],4)


if __name__=='__main__':unittest.main()
