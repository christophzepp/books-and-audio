"""Large cache imports stay incremental, bounded and safe across writers."""
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cache_maintenance
from epub_index_cache import EpubIndexCache
from epub_locations import EpubLocations
from media_observation import ObservationStore
import test_epub_index_capacity as epub_fixture


def write_cache_process(root, number):
    store = ObservationStore(Path(root), max_entries=24)
    for index in range(60):
        if not store.put('example', [number, index], 'identity', {'value': index}):
            raise RuntimeError('Cache writer failed')


class CacheMaintenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        cache_maintenance._catalogs.clear()

    def test_thousand_observation_inserts_from_short_lived_stores_scan_once(self):
        original = os.scandir
        with patch('cache_maintenance.os.scandir', wraps=original) as scan:
            for index in range(1000):
                self.assertTrue(ObservationStore(self.root).put('example', index, 'identity', {'value': index}))
        self.assertEqual(scan.call_count, 1)
        store = ObservationStore(self.root)
        self.assertEqual(len(list(store.directory.glob('*.json'))), 1000)
        for index in (0, 500, 999):
            self.assertEqual(store.get('example', index, 'identity'), {'value': index})

    def test_thousand_epub_index_insertions_scan_once_and_survive_new_instances(self):
        directory = self.root / 'indexes'
        data = json.loads(json.dumps(epub_fixture.epub_index().cache_data()))
        with patch('cache_maintenance.os.scandir', wraps=os.scandir) as scan:
            for index in range(1000):
                sha = hashlib.sha256(str(index).encode()).hexdigest()
                EpubIndexCache(directory).seed(EpubLocations.from_cache_data(data, sha))
        self.assertEqual(scan.call_count, 1)
        cache = EpubIndexCache(directory)
        for index in (0, 500, 999):
            self.assertIsNotNone(cache.cached(hashlib.sha256(str(index).encode()).hexdigest()))

    def test_replacements_and_forgets_do_not_repeat_directory_scans(self):
        store = ObservationStore(self.root, max_entries=3)
        with patch('cache_maintenance.os.scandir', wraps=os.scandir) as scan:
            for index in range(1000):
                self.assertTrue(store.put('example', 'same', 'identity', {'value': index}))
            store.forget('example', 'same')
            store.forget('example', 'absent')
            self.assertTrue(store.put('example', 'other', 'identity', {'value': 1}))
        self.assertEqual(scan.call_count, 1)
        self.assertIsNone(store.get('example', 'same', 'identity'))
        self.assertEqual(store.get('example', 'other', 'identity'), {'value': 1})
        self.assertLessEqual(sum(len(c.heap) for c in cache_maintenance._catalogs.values()), 68)

    def test_external_directory_changes_and_policy_change_reconcile_before_eviction(self):
        store = ObservationStore(self.root, max_entries=3)
        store.put('example', 'one', 'identity', {'value': 1})
        (store.directory / store._name('example', 'one')).unlink()
        foreign = store.directory / ('f' * 64 + '.json')
        foreign.write_text('{}'); foreign.chmod(0o600)
        os.utime(foreign, ns=(1, 1))
        with patch('cache_maintenance.os.scandir', wraps=os.scandir) as scan:
            self.assertTrue(store.put('example', 'two', 'identity', {'value': 2}))
            constrained = ObservationStore(self.root, max_entries=1)
            self.assertTrue(constrained.put('example', 'three', 'identity', {'value': 3}))
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(len(list(store.directory.glob('*.json'))), 1)
        self.assertEqual(store.get('example', 'three', 'identity'), {'value': 3})

    def test_mount_epochs_survive_pressure_and_symlink_targets_are_never_touched(self):
        store = ObservationStore(self.root, max_entries=2)
        for kind in ('all', 'audiobooks', 'ebooks', 'podcasts'):
            self.assertTrue(store.put('folder-epoch', kind, 'identity', {'epoch': kind}))
        outside = self.root / 'unchanged'; outside.write_text('keep')
        link = store.directory / ('e' * 64 + '.json'); link.symlink_to(outside)
        for index in range(10): store.put('example', index, 'identity', {'value': index})
        for kind in ('all', 'audiobooks', 'ebooks', 'podcasts'):
            self.assertEqual(store.get('folder-epoch', kind, 'identity'), {'epoch': kind})
        self.assertTrue(link.is_symlink()); self.assertEqual(outside.read_text(), 'keep')

    def test_interrupted_post_rename_update_reconciles_orphan_on_next_write(self):
        store = ObservationStore(self.root, max_entries=1)
        with patch.object(cache_maintenance.Catalog, 'record', side_effect=OSError('interrupted bookkeeping')):
            self.assertFalse(store.put('example', 'orphan', 'identity', {'value': 1}))
        self.assertTrue(store.put('example', 'next', 'identity', {'value': 2}))
        self.assertEqual(len(list(store.directory.glob('*.json'))), 1)
        self.assertIsNone(store.get('example', 'orphan', 'identity'))
        self.assertEqual(store.get('example', 'next', 'identity'), {'value': 2})

    def test_parallel_processes_keep_exact_entry_cap_and_valid_payloads(self):
        context = multiprocessing.get_context('spawn')
        processes = [context.Process(target=write_cache_process, args=(str(self.root), number)) for number in range(3)]
        for process in processes: process.start()
        for process in processes:
            process.join(20)
            if process.is_alive(): process.terminate(); process.join(); self.fail('Cache writer stalled')
            self.assertEqual(process.exitcode, 0)
        files = list((self.root / 'media-observations').glob('*.json'))
        self.assertEqual(len(files), 24)
        for path in files:
            value = json.loads(path.read_text())
            from media_observation import fingerprint
            self.assertEqual(value['sha256'], fingerprint({k: v for k, v in value.items() if k != 'sha256'}))

    def test_catalog_memory_is_bounded_across_unrelated_directories(self):
        for index in range(12):
            root = self.root / str(index); root.mkdir(mode=0o700)
            self.assertTrue(ObservationStore(root).put('example', 1, 'identity', {'value': 1}))
        self.assertLessEqual(len(cache_maintenance._catalogs), cache_maintenance.MAX_CATALOGS)


if __name__ == '__main__': unittest.main()
