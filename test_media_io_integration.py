"""Exercise EPUB idle checkpoints with the real shared folder validator."""
import copy
from pathlib import Path
import unittest

from folder_manager import FolderManager, FolderManagerError
from io_metrics import snapshot
from jellyfin_ebook import EbookCoordinator
import test_jellyfin_ebook as fixtures
import test_folder_transfer_io as folder_tests


class MediaIOIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.EbookTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.b = self.f.c.b
        temporary = self.f.path.parent.resolve()
        self.media = temporary / 'hdd'
        self.media.mkdir()
        target = self.media / self.f.path.name
        self.f.path.rename(target)
        self.f.path = target
        self.f.m['local_path'] = str(self.f.path.resolve())
        self.b.root = temporary / 'ssd-state'
        self.b.root.mkdir(mode=0o700)
        self.b.config = {'folder_mappings': [dict(id='text', kind='ebooks', name='Text', active=True,
            paths={'worker': str(self.media), 'bo': '/books', 'jf': '/data/ebooks'})]}
        self.mount, self.available = 'generation-1', True
        self.folders = FolderManager(self.b.root, self.b.config, [{'worker': str(self.media)}],
            mount_checker=lambda path: self.available,
            generation_reader=lambda: self.mount, clock=lambda: 1000)
        self.folders.seed()
        self.context = {'folder_gate_ok': True}
        self.b.media_generation = lambda kind: self.folders.generation(kind, context=self.context)
        self.b.media_guard = lambda kind: self.folders.require(kind, context=self.context)
        self.b.media_observation_generation = lambda kind: self.folders.observation_generation(kind, context=self.context)
        self.b.media_observation_guard = lambda kind: self.folders.require_observation(kind, context=self.context)

    def test_hundred_combined_idle_ticks_do_not_repeat_media_work(self):
        self.assertEqual(self.f.run_cycle(), 'unchanged')
        folder_before = snapshot(self.folders)['folder_checks']
        media_before = copy.deepcopy(snapshot(self.b))
        for _ in range(100):
            self.folders.effective(context=self.context)
            self.folders.public()
            self.f.c = EbookCoordinator(self.b)
            self.assertEqual(self.f.run_cycle(), 'unchanged')
        self.assertEqual(snapshot(self.folders)['folder_checks'], folder_before)
        self.assertEqual(snapshot(self.b)['ebook_source_reads'], media_before['ebook_source_reads'])
        self.assertEqual(snapshot(self.b)['ebook_index_builds'], media_before['ebook_index_builds'])
        self.assertEqual(snapshot(self.b)['ebook_idle_skips'], 100)

    def test_changed_mount_cannot_use_old_checkpoint_or_write_progress(self):
        self.f.run_cycle()
        before = snapshot(self.b)['ebook_source_reads']
        self.mount, self.available = 'generation-2', False
        self.f.set_bo(3)
        with self.assertRaisesRegex(FolderManagerError, 'folder_verification_required'):
            self.f.run_cycle()
        self.assertEqual(snapshot(self.b)['ebook_source_reads'], before)
        self.assertEqual(self.f.writes, [])

    def test_real_transfers_reuse_durable_folder_and_epub_proofs_after_restart(self):
        self.assertEqual(self.f.run_cycle(), 'unchanged')
        physical = copy.deepcopy(self.f.store['_jellyfin_ebook:1']['media_proof'])
        self.folders = FolderManager(self.b.root, self.b.config, [{'worker': str(self.media)}],
            mount_checker=lambda path: self.available,
            generation_reader=lambda: self.mount, clock=lambda: 1000 + 400 * 86400)
        # Do not seed/refresh the replacement: the durable proof must survive restart.
        self.b.__dict__.pop('_ebook_index_cache', None)
        self.f.c = EbookCoordinator(self.b)
        with folder_tests.FolderTransferIOTests.no_media(self, self.folders):
            self.f.set_bo(3)
            self.assertEqual(self.f.run_cycle(), 'synced')
            self.f.set_jf(5)
            self.assertEqual(self.f.run_cycle(), 'synced')
        self.assertEqual(self.f.e.bo_to_index(self.f.progress['text']['cfi']), 5)
        self.assertEqual(self.f.e.ticks_to_index(self.f.user['PlaybackPositionTicks']), 5)
        self.assertEqual(self.f.store['_jellyfin_ebook:1']['media_proof'], physical)
        self.assertEqual(snapshot(self.folders).get('folder_checks', 0), 0)

    def test_recreated_folder_validator_reuses_restart_stable_idle_binding(self):
        self.f.run_cycle()
        before = snapshot(self.b)['ebook_source_reads']
        replacement = FolderManager(self.b.root, self.b.config, [{'worker': str(self.media)}],
            mount_checker=lambda path: self.available,
            generation_reader=lambda: self.mount, clock=lambda: 1000)
        replacement.seed()
        self.folders = replacement
        self.f.c = EbookCoordinator(self.b)
        self.assertEqual(self.f.run_cycle(), 'unchanged')
        self.assertEqual(snapshot(self.b)['ebook_source_reads'], before)


if __name__ == '__main__':
    unittest.main()
