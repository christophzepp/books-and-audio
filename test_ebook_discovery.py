import copy
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import patch

from ebook_discovery import discover, refresh
from watch_deployment import validate_ebook_mounts, DeploymentError
from test_jellyfin_ebook import EbookTests


class DiscoveryTests(EbookTests):
    def setUp(self):
        super().setUp()
        self.spec = dict(enabled=True, bo_root='/books', jf_root='/data/ebooks',
                         local_root=self.tmp.name, bo_library_id=2, jf_library_id='library')
        self.cards = [dict(id=1, libraryId=2)]
        self.files = [dict(id=1, format='epub', absolutePath='/books/test.epub', sizeBytes=self.path.stat().st_size)]
        self.bo.query = lambda: copy.deepcopy(self.cards)
        old = self.bo.detail
        self.bo.detail = lambda i: dict(old(i), id=i, libraryId=2, files=copy.deepcopy(self.files))
        self.jitems = [dict(Id='a'*32, Path='/data/ebooks/test.epub', Type='Book')]
        self.discovery_jf = SimpleNamespace(items=lambda **kw:copy.deepcopy(self.jitems), guard=lambda:None)

    def discover(self, previous=None, now=1000, existing=None):
        return discover(self.bo, self.discovery_jf, self.spec, existing or [], previous or {}, now)

    def test_new_book_requires_two_stable_observations_then_both_directions(self):
        new, obs, report = self.discover()
        self.assertEqual(new, []); self.assertEqual(report['waiting'], 1)
        new, _, report = self.discover(obs, 1061)
        self.assertEqual(len(new), 1)
        self.m = new[0]
        self.assertEqual(self.run_cycle(), 'unchanged')
        self.set_bo(3); self.assertEqual(self.run_cycle(), 'synced')
        self.set_jf(5); self.assertEqual(self.run_cycle(), 'synced')
        self.assertEqual(self.e.bo_to_index(self.progress['text']['cfi']), 5)
        self.assertFalse(self.discover(existing=new)[0])

    def test_duplicate_jellyfin_path_blocked(self):
        self.jitems += [dict(self.jitems[0], Id='b'*32)]
        new, obs, r = self.discover()
        self.assertFalse(new or obs); self.assertIn('Ambiguous',r['blocked'][0]['reason'])

    def test_duplicate_bookorbit_path_blocked(self):
        self.cards.append(dict(id=2, libraryId=2))
        new, obs, r = self.discover()
        self.assertFalse(new or obs); self.assertEqual(len(r['blocked']), 2)

    def test_readalong_filename_and_mixed_book_excluded(self):
        self.files[0]['absolutePath']='/books/test.readalong.epub'
        self.assertFalse(self.discover()[1])
        self.files[0]['absolutePath']='/books/test.epub'
        self.files.append(dict(format='mp3', id=2))
        self.assertFalse(self.discover()[1])

    def test_embedded_readalong_is_not_admitted(self):
        with zipfile.ZipFile(self.path, 'a') as z:z.writestr('overlay.smil','<smil/>')
        self.files[0]['sizeBytes']=self.path.stat().st_size
        new, obs, r = self.discover()
        self.assertFalse(new); self.assertEqual(r['waiting'], 1)
        new, _, r = self.discover(obs, 1061)
        self.assertFalse(new); self.assertIn('Read-along',r['blocked'][0]['reason'])

    def test_missing_index_waits_and_changed_bytes_restart_observation(self):
        _, obs, _ = self.discover()
        with zipfile.ZipFile(self.path, 'a') as z:z.writestr('metadata.txt','change')
        self.files[0]['sizeBytes']=self.path.stat().st_size
        new, updated, r = self.discover(obs, 1100)
        self.assertFalse(new);self.assertEqual(updated['1']['since'],1100)
        self.jitems=[]
        self.assertEqual(self.discover(updated, 1200)[2]['waiting'],1)

    def test_first_and_unstable_observations_never_read_epub_contents(self):
        with patch('ebook_discovery.inspect_epub', side_effect=AssertionError('content read')) as inspect:
            _, obs, r = self.discover()
            self.assertEqual(r['waiting'], 1)
            for at in (1001, 1020, 1059):
                additions, obs, _ = self.discover(obs, at)
                self.assertFalse(additions)
            self.files[0]['sizeBytes'] += 1
            self.assertFalse(self.discover(obs, 1100)[0])
            inspect.assert_not_called()

    def test_same_size_edit_with_restored_mtime_restarts_stability(self):
        _, obs, _ = self.discover()
        original = self.path.stat()
        raw = self.path.read_bytes()
        self.path.write_bytes(raw.replace(b'Hello', b'Hallo'))
        os.utime(self.path, ns=(original.st_atime_ns, original.st_mtime_ns))
        with patch('ebook_discovery.inspect_epub', side_effect=AssertionError('content read')) as inspect:
            additions, updated, _ = self.discover(obs, 1100)
            self.assertFalse(additions)
            self.assertEqual(updated['1']['since'], 1100)
            inspect.assert_not_called()

    def test_mutation_during_content_validation_never_admits(self):
        _, obs, _ = self.discover()
        from ebook_discovery import inspect_epub
        def change(path, **kwargs):
            index = inspect_epub(path, **kwargs)
            with zipfile.ZipFile(path, 'a') as archive:
                archive.writestr('late-metadata.txt', 'changed while checking')
            return index
        with patch('ebook_discovery.inspect_epub', side_effect=change):
            additions, observed, report = self.discover(obs, 1061)
        self.assertFalse(additions or observed)
        self.assertIn('changed during validation', report['blocked'][0]['reason'])

    def test_no_new_api_candidates_does_not_request_physical_folder_guard(self):
        from unittest.mock import Mock
        guard = Mock(side_effect=AssertionError('unnecessary media traversal'))
        self.cards = []
        additions, _, _ = discover(self.bo, self.discovery_jf, self.spec, [], {}, 1000, media_guard=guard)
        self.assertFalse(additions)
        guard.assert_not_called()

    def test_admission_seeds_reusable_index_after_stable_validation(self):
        from epub_index_cache import EpubIndexCache
        from epub_locations import EpubLocations
        cache = EpubIndexCache()
        _, obs, _ = self.discover()
        additions, _, _ = discover(self.bo, self.discovery_jf, self.spec, [], obs, 1061, index_cache=cache)
        self.assertEqual(len(additions), 1)
        with patch.object(EpubLocations, 'from_bytes', side_effect=AssertionError('second index build')):
            index = cache.get(self.path, additions[0]['sha256'], additions[0]['size'])
        self.assertEqual(index.hash, additions[0]['sha256'])

    def test_mount_proof_rejects_changed_image_source_and_nested_mount(self):
        spec=dict(self.spec, jf_image='jf', shared_host_root='/media/ebooks')
        cfg=dict(jellyfin=dict(auto_ebooks=spec),deployment=dict(bookorbit=dict(image='bo')))
        cs=[dict(Name='/bookorbit',Image='bo',State=dict(Status='running'),Mounts=[dict(Source='/media/ebooks',Destination='/books')]),
            dict(Name='/jellyfin',Image='jf',State=dict(Status='running'),Mounts=[dict(Source='/media',Destination='/data')])]
        validate_ebook_mounts(cs,cfg)
        for change in ('image','source','nested'):
            bad=copy.deepcopy(cs)
            if change=='image':bad[1]['Image']='different'
            if change=='source':bad[1]['Mounts'][0]['Source']='/different'
            if change=='nested':bad[1]['Mounts'].append(dict(Source='/elsewhere',Destination='/data/ebooks/nested'))
            with self.assertRaises(DeploymentError):validate_ebook_mounts(bad,cfg)

    def test_persistent_promotion_dry_run_and_incomplete_scan(self):
        clock=[1000];store={};events=[]
        b=SimpleNamespace(config=dict(jellyfin=dict(auto_ebooks=self.spec,ebooks=[])),
            bo=self.bo,jellyfin=self.discovery_jf,clock=lambda:clock[0],guard=lambda:None,
            load=lambda k:copy.deepcopy(store.get(k,{})),save=lambda k,v:store.update({k:copy.deepcopy(v)}),
            event=lambda *x:events.append(x),alert=lambda *x:events.append(x))
        self.assertEqual(refresh(b,False)[0],[]);self.assertEqual(store,{})
        self.assertEqual(refresh(b,True)[0],[])
        clock[0]+=301
        oldquery=self.bo.query
        self.bo.query=lambda:(_ for _ in ()).throw(RuntimeError('Incomplete inventory'))
        self.assertEqual(refresh(b,True)[0],[])
        self.assertEqual(store['_ebook_discovery']['observations'],{})
        self.bo.query=oldquery
        self.assertEqual(refresh(b,True)[0],[])
        clock[0]+=301
        new,_=refresh(b,True);self.assertEqual(len(new),1)
        self.assertEqual(len(store['_ebook_discovery']['mappings']),1)
        self.assertEqual(refresh(b,True)[0],new)

if __name__=='__main__':unittest.main()
