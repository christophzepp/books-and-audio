"""Exercise EPUB idle checkpoints with the real shared folder validator."""
import copy
from pathlib import Path
import unittest

from folder_manager import FolderManager, FolderManagerError
from io_metrics import snapshot
from jellyfin_ebook import EbookCoordinator
import test_jellyfin_ebook as fixtures


class MediaIOIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.EbookTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.b = self.f.c.b
        self.media = self.f.path.parent.resolve()
        self.f.m['local_path'] = str(self.f.path.resolve())
        self.b.root = self.media / 'ssd-state'
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
        with self.assertRaisesRegex(FolderManagerError, 'folder_mount_unavailable'):
            self.f.run_cycle()
        self.assertEqual(snapshot(self.b)['ebook_source_reads'], before)
        self.assertEqual(self.f.writes, [])

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
