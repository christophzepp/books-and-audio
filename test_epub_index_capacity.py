"""NVMe retention supports large libraries while RAM and disk remain bounded."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from epub_index_cache import EpubIndexCache, MAX_DISK_BYTES, MAX_DISK_ENTRIES, MAX_MEMORY_ENTRIES
from epub_locations import EpubLocations
from jellyfin_ebook import EbookCoordinator


def epub_index(paragraphs=1,characters=12000):
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('META-INF/container.xml','<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
        archive.writestr('content.opf','<package><metadata/><manifest><item id="s" href="s.xhtml"/></manifest><spine><itemref idref="s"/></spine></package>')
        archive.writestr('s.xhtml','<html xmlns="http://www.w3.org/1999/xhtml"><head/><body>'
                         +''.join('<p>'+('x'*characters)+'</p>' for _ in range(paragraphs))+'</body></html>')
    return EpubLocations.from_bytes(output.getvalue())


class EpubIndexCapacityTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)/'nvme-cache'
        self.index=epub_index()

    def seed_books(self,cache,count):
        data=json.loads(json.dumps(self.index.cache_data()));hashes=[]
        for number in range(count):
            # Distinct EPUB metadata can have identical indexed text/positions.
            sha=hashlib.sha256(('book-'+str(number)).encode()).hexdigest()
            cache.seed(EpubLocations.from_cache_data(data,sha));hashes.append(sha)
            self.assertLessEqual(len(cache.memory),MAX_MEMORY_ENTRIES)
        return hashes

    def test_default_disk_budget_retains_thousand_indexes_across_restart(self):
        cache=EpubIndexCache(self.root)
        hashes=self.seed_books(cache,1000)
        files=list(self.root.glob('*.json'))
        self.assertEqual(len(files),1000)
        self.assertLess(sum(path.stat().st_size for path in files),MAX_DISK_BYTES)
        restored=EpubIndexCache(self.root)
        with patch('epub_index_cache.verified_bytes',side_effect=AssertionError('media read')):
            for sha in hashes:
                index=restored.cached(sha)
                self.assertIsNotNone(index)
                self.assertEqual(index.hash,sha)
                self.assertEqual(index.index_to_bo(3),self.index.index_to_bo(3))
                self.assertLessEqual(len(restored.memory),MAX_MEMORY_ENTRIES)
        self.assertEqual(MAX_DISK_ENTRIES,4096)
        self.assertEqual(MAX_DISK_BYTES,1024**3)

    def test_configured_entry_budget_evicts_only_when_capacity_is_exceeded(self):
        cache=EpubIndexCache(self.root,max_entries=3)
        hashes=self.seed_books(cache,5)
        self.assertEqual(len(list(self.root.glob('*.json'))),3)
        restored=EpubIndexCache(self.root,max_entries=3)
        for sha in hashes[-3:]:self.assertIsNotNone(restored.cached(sha))
        for sha in hashes[:-3]:self.assertIsNone(restored.cached(sha))

    def test_configured_byte_budget_still_bounds_disk_growth(self):
        first=EpubIndexCache(self.root);first.seed(self.index)
        size=next(self.root.glob('*.json')).stat().st_size
        budget=size*2
        cache=EpubIndexCache(self.root,max_bytes=budget,max_entries=100)
        hashes=self.seed_books(cache,5)
        files=list(self.root.glob('*.json'))
        self.assertEqual(len(files),2)
        self.assertLessEqual(sum(path.stat().st_size for path in files),budget)
        restored=EpubIndexCache(self.root,max_bytes=budget,max_entries=100)
        for sha in hashes[-2:]:self.assertIsNotNone(restored.cached(sha))

    def test_coordinator_passes_both_limits_and_reloads_only_nvme_after_reconfigure(self):
        self.root.mkdir(mode=0o700)
        bridge=SimpleNamespace(root=self.root,jellyfin=None)
        coordinator=EbookCoordinator(bridge)
        config={'io_policy':dict(ebook_cache_max_bytes=2*1024**3,ebook_cache_max_entries=8192)}
        cache=coordinator.index_cache(config);cache.seed(self.index)
        self.assertEqual(cache.max_bytes,2*1024**3);self.assertEqual(cache.max_entries,8192)
        config['io_policy']['ebook_cache_max_entries']=10000
        replacement=coordinator.index_cache(config)
        self.assertIsNot(cache,replacement)
        with patch('epub_index_cache.verified_bytes',side_effect=AssertionError('media read')):
            self.assertIsNotNone(replacement.cached(self.index.hash))

    def test_synthetic_half_megabyte_text_index_has_room_for_thousand_books(self):
        index=epub_index(paragraphs=1000,characters=500)
        cache=EpubIndexCache(self.root);cache.seed(index)
        size=next(self.root.glob('*.json')).stat().st_size
        self.assertLess(size*1000,MAX_DISK_BYTES)
        self.assertLessEqual(len(cache.memory),MAX_MEMORY_ENTRIES)

    def test_constructor_rejects_unbounded_or_invalid_limits(self):
        for value in (None,True,0,-1,100001,float('inf')):
            with self.subTest(entries=value),self.assertRaises(ValueError):
                EpubIndexCache(self.root,max_entries=value)
        for value in (None,True,0,-1,16*1024**3+1,float('inf')):
            with self.subTest(bytes=value),self.assertRaises(ValueError):
                EpubIndexCache(self.root,max_bytes=value)


if __name__=='__main__':unittest.main()
