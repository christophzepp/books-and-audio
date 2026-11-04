"""Folder aliases affect actual pairing/progress validation, not just GUI data."""
import copy
import hashlib
import unittest

import test_mapping as mapping_fixtures
import test_discovery as discovery_fixtures
import test_native_discovery as native_fixtures
import test_native_worker_receipt as receipt_fixtures
import test_jellyfin_ebook as ebook_fixtures
from folder_manager import default_rows
from mapping import build_mapping
from discovery import discover
import native_discovery as native
from jellyfin_audio import bind, TICKS


def config(abs_root='/abs/audio', bo_root='/bo/audio', jf_root='/jf/audio'):
    return dict(folder_mappings=[dict(id='audio', name='Audio', kind='audiobooks', active=True,
       paths=dict(worker='/worker/audio', abs=abs_root, bo=bo_root, jf=jf_root, bb='/bb/audio'))])


def repath(a, b, abs_root='/abs/audio', bo_root='/bo/audio'):
    a['path'] = a['path'].replace('/audiobooks', abs_root, 1)
    for row in a['media']['tracks']:
        row['metadata']['path'] = row['metadata']['path'].replace('/audiobooks', abs_root, 1)
    b['folderPath'] = b['folderPath'].replace('/audiobooks', bo_root, 1)
    for row in b['files']:
        row['absolutePath'] = row['absolutePath'].replace('/audiobooks', bo_root, 1)


class RuntimeFolders(unittest.TestCase):
    def test_default_aliases_preserve_every_existing_mapping_field(self):
        before = build_mapping(*mapping_fixtures.fixture())
        cfg = dict(folder_mappings=default_rows({}))
        self.assertEqual(build_mapping(*mapping_fixtures.fixture(), folder_config=cfg), before)

    def test_distinct_service_paths_pair_same_relative_files(self):
        a, b, m = mapping_fixtures.fixture(); repath(a,b)
        result = build_mapping(a,b,m,folder_config=config())
        self.assertEqual(result['folder'], '/abs/audio/Example')
        self.assertEqual(result['files'][0]['path'], '/abs/audio/Example/01.mp3')
        with self.assertRaises(ValueError): build_mapping(a,b,m)

    def test_bo_alias_change_does_not_change_abs_identity(self):
        a,b,m = mapping_fixtures.fixture()
        before = build_mapping(a,b,m)
        repath(a,b,abs_root='/audiobooks')
        self.assertEqual(build_mapping(a,b,m,folder_config=config(abs_root='/audiobooks')), before)

    def test_file_outside_declared_bo_root_is_not_accepted(self):
        a,b,m = mapping_fixtures.fixture(); repath(a,b)
        b['files'][0]['absolutePath'] = '/bo/audio-elsewhere/Example/01.mp3'
        with self.assertRaises(ValueError): build_mapping(a,b,m,folder_config=config())

    def test_abs_root_changes_cannot_preserve_old_fingerprint(self):
        a,b,m = mapping_fixtures.fixture(); before = build_mapping(a,b,m)
        repath(a,b)
        self.assertNotEqual(build_mapping(a,b,m,folder_config=config())['fingerprint'],before['fingerprint'])

    def test_discovery_with_aliases_waits_then_pairs_new_title(self):
        row = discovery_fixtures.book(); repath(row[0],row[1])
        a,b = discovery_fixtures.FakeABS([row]),discovery_fixtures.FakeBO([row])
        first = discover(a,b,[],discovery_fixtures.ABS_LIBRARY,[7],{},1000,folder_config=config())
        self.assertTrue(first['scan_ok']); self.assertEqual(first['summary']['waiting'],1)
        second = discover(a,b,[],discovery_fixtures.ABS_LIBRARY,[7],first['candidates'],1301,folder_config=config())
        self.assertTrue(second['scan_ok']); self.assertEqual(len(second['new_mappings']),1)
        self.assertEqual(second['new_mappings'][0]['folder'],'/abs/audio/One')

    def test_discovery_respects_existing_pairs_under_alias(self):
        row = discovery_fixtures.book(); repath(row[0],row[1])
        a,b = discovery_fixtures.FakeABS([row]),discovery_fixtures.FakeBO([row])
        mapped = build_mapping(*row,folder_config=config())
        result = discover(a,b,[mapped],discovery_fixtures.ABS_LIBRARY,[7],{},1000,folder_config=config())
        self.assertTrue(result['scan_ok']); self.assertFalse(result['new_mappings']); self.assertFalse(result['issues'])

    def test_jellyfin_binding_uses_configured_actual_path(self):
        a,b,m = mapping_fixtures.fixture(); repath(a,b)
        mapped = build_mapping(a,b,m,folder_config=config()); inventory=[]
        for i,f in enumerate(mapped['files']):
            path=f['path'].replace('/abs/audio','/jf/audio',1)
            inventory.append(dict(Id=str(i),Type='AudioBook',Path=path,RunTimeTicks=round(f['duration']*TICKS),MediaSources=[dict(Path=path,Size=f['size'])]))
        tracks=bind(mapped,inventory,config())
        self.assertEqual(tracks[0]['path'],'/jf/audio/Example/01.mp3')
        with self.assertRaises(ValueError): bind(mapped,inventory)
        inventory[0]['MediaSources'][0]['Size']+=1
        with self.assertRaisesRegex(ValueError,'differs'): bind(mapped,inventory,config())

    def test_native_epub_proof_keeps_actual_bo_path(self):
        expected,a,b,m,cap,job=native_fixtures.evidence(mapping_fixtures.fixture)
        repath(a,b,abs_root='/audiobooks')
        proof,current=native.inspect(expected,a,b,m,cap,job,folder_config=config(abs_root='/audiobooks'))
        self.assertEqual(current,expected)
        self.assertEqual(proof['source']['pin']['overlay_path'],'/bo/audio/Example/source.epub')
        self.assertEqual(proof['overlay']['pin']['overlay_path'],'/bo/audio/Example/CD 0/generated.readaloud.epub')

    def test_late_native_receipt_uses_unmodified_bo_path_and_checksum(self):
        f=receipt_fixtures.WorkerReceiptTests('test_late_first_observation_uses_early_receipt_and_keeps_actual_api_time')
        f.setUp(); self.addCleanup(f.doCleanups)
        repath(f.ai,f.bo.bd,abs_root='/audiobooks')
        f.detail=copy.deepcopy(f.bo.bd)
        f.bo.folder_config=config(abs_root='/audiobooks')
        for row in [f.receipt['source'], *f.receipt['audio']]:
            row['path']=row['path'].replace('/audiobooks','/bo/audio',1)
        request=f.receipt['request'];request['epubPath']=f.receipt['source']['path']
        request['audioPaths']=[r['path'] for r in f.receipt['audio']]
        f.receipt['request_sha256']=hashlib.sha256(receipt_fixtures.encoded(request)).hexdigest()
        f.write_receipt()
        before=copy.deepcopy(f.receipt)
        proof,current=f.collect()
        self.assertEqual(current,f.expected)
        self.assertEqual(proof['sha256_evidence']['kind'],'worker-artifact')
        self.assertEqual(f.receipt,before)
        self.assertEqual(proof['source']['pin']['overlay_path'],request['epubPath'])

    def test_ebook_progress_resolves_configured_mount_both_directions(self):
        f=ebook_fixtures.EbookTests('test_both_directions_idempotent');f.setUp();self.addCleanup(f.doCleanups)
        f.m['local_path']=str(f.path.resolve())
        f.c.b.config=dict(folder_mappings=[dict(id='text',name='Text',active=True,kind='ebooks',paths=dict(worker=str(f.path.resolve().parent),bo='/books',jf='/data/ebooks'))])
        f.test_both_directions_idempotent()
        f.c.b.config['folder_mappings'][0]['paths']['jf']='/wrong'
        with self.assertRaisesRegex(ValueError,'aliases'): f.run_cycle()


if __name__=='__main__':unittest.main()
