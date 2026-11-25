import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from series_imports import ImportQueue, publish, receipt
from series_listenarr import Defer
from series_publication import OWNER, finish_committed


class Interruption(BaseException):
    """An instantaneous process loss; normal error handlers must not run."""


class MergerfsPublicationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / 'media'
        self.root.mkdir()
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        self.queue = ImportQueue(self.base)
        self.job = receipt('shelfmark', ['Author', 'Serie', '11 - Titel'], None, 'event-1')
        self.key = self.queue.enqueue(self.job)
        self.src = self.root / '.series-import/shelfmark' / self.job['folder']
        self.dst = self.root / self.job['target']
        self.stage = self.root / '.series-work'
        for rel in ('Disc/1.mp3', 'Disc/2.mp3', 'cover.jpg'):
            file = self.src / rel
            file.parent.mkdir(exist_ok=True, parents=True)
            file.write_bytes(rel.encode())
        self.rename = patch('series_imports._rename_exclusive', side_effect=OSError(errno.EINVAL, 'mergerfs'))
        self.rename_mock = self.rename.start()
        self.addCleanup(self.rename.stop)

    def run_job(self):
        return publish(self.fd, self.key, self.job, self.queue)

    def interrupt_during_tracks(self):
        link = os.link

        def stop(src, dst, **opts):
            link(src, dst, **opts)
            if src == '1.mp3' and (self.dst / '.ignore').exists():
                raise Interruption

        with patch('os.link', side_effect=stop), self.assertRaises(Interruption):
            self.run_job()
        self.assertTrue((self.dst / OWNER).is_file())
        self.assertTrue((self.dst / '.ignore').is_file())
        self.assertTrue((self.dst / 'Disc/1.mp3').samefile(self.src / 'Disc/1.mp3'))
        self.assertFalse((self.dst / 'Disc/2.mp3').exists())

    def complete(self):
        self.assertIsNone(self.queue.publication(self.key))
        self.assertEqual(self.queue.queued(), [])
        self.assertEqual(sorted(p.name for p in self.stage.iterdir()), ['.ignore'])
        for rel in ('Disc/1.mp3', 'Disc/2.mp3', 'cover.jpg'):
            self.assertTrue((self.src / rel).samefile(self.dst / rel))
        self.assertFalse((self.dst / '.ignore').exists())
        self.assertFalse((self.dst / OWNER).exists())

    def test_exclusive_hardlinks_complete_before_exposure_and_no_replay(self):
        link = os.link
        inserted = []
        def observe(src, dst, **opts):
            if self.dst.is_dir() and dst.endswith(('.mp3','.jpg')):
                self.assertTrue((self.dst / '.ignore').is_file())
                inserted.append(dst)
            return link(src, dst, **opts)
        with patch('os.link', side_effect=observe):
            self.assertTrue(self.run_job())
        self.assertEqual(set(inserted), {'1.mp3', '2.mp3', 'cover.jpg'})
        self.complete()
        with patch('os.link', side_effect=AssertionError('unerwartete Änderung')):
            self.assertFalse(self.run_job())

    def test_recovers_after_first_audio_link(self):
        self.interrupt_during_tracks()
        self.queue = ImportQueue(self.base)
        self.assertTrue(self.run_job())
        self.complete()

    def test_foreign_empty_directory_after_native_failure_is_never_adopted(self):
        def reserve(*_):
            self.dst.mkdir()
            raise OSError(errno.EINVAL, 'mergerfs')
        self.rename_mock.side_effect = reserve
        with self.assertRaises(Defer):
            self.run_job()
        self.assertEqual(list(self.dst.iterdir()), [])
        with self.assertRaises(Defer):
            self.run_job()

    def test_unrelated_error_is_never_reinterpreted_as_missing_support(self):
        for error in (errno.EACCES, errno.EEXIST, errno.EXDEV):
            with self.subTest(errno=error):
                self.rename_mock.side_effect = OSError(error, 'other')
                with self.assertRaises(OSError) as caught:
                    self.run_job()
                self.assertEqual(caught.exception.errno, error)
                self.assertIsNone(self.queue.publication(self.key))
                self.assertFalse(self.dst.exists())

    def test_foreign_content_survives_recovery_and_stays_hidden(self):
        self.interrupt_during_tracks()
        wrong = self.dst / 'Disc/2.mp3'
        wrong.write_bytes(b'keep')
        with self.assertRaises(Defer):
            self.run_job()
        self.assertEqual(wrong.read_bytes(), b'keep')
        self.assertTrue((self.dst / '.ignore').exists())

    def test_symlink_cannot_replace_owner_or_any_nested_directory(self):
        self.interrupt_during_tracks()
        (self.dst / OWNER).unlink()
        (self.dst / OWNER).symlink_to(self.dst / '.ignore')
        with self.assertRaises(Defer):
            self.run_job()
        self.assertTrue((self.dst / '.ignore').exists())

    def interrupt_after_visibility(self):
        unlink = os.unlink
        def stop(name, **opts):
            unlink(name, **opts)
            if name == '.ignore' and not (self.dst / '.ignore').exists():
                raise Interruption
        with patch('os.unlink', side_effect=stop), self.assertRaises(Interruption):
            self.run_job()
        self.assertEqual(self.queue.publication(self.key)[1], 1)
        self.assertTrue((self.dst / OWNER).exists())

    def test_recovers_after_scanner_marker_removed(self):
        self.interrupt_after_visibility()
        self.queue = ImportQueue(self.base)
        self.assertTrue(self.run_job())
        self.complete()

    def test_identical_files_in_replacement_dir_are_not_accepted_as_our_claim(self):
        self.interrupt_after_visibility()
        self.dst.rename(self.dst.with_name('moved'))
        for rel in ('Disc/1.mp3', 'Disc/2.mp3', 'cover.jpg'):
            path = self.dst / rel
            path.parent.mkdir(exist_ok=True, parents=True)
            os.link(self.src / rel, path)
        with self.assertRaises(Defer):
            self.run_job()
        self.assertIsNotNone(self.queue.publication(self.key))
        self.assertFalse((self.dst / '.ignore').exists())
        self.assertFalse((self.dst / OWNER).exists())

    def test_lost_ready_target_is_not_recreated(self):
        self.interrupt_after_visibility()
        self.dst.rename(self.dst.with_name('moved'))
        with self.assertRaises(FileNotFoundError):
            self.run_job()
        self.assertFalse(self.dst.exists())

    def test_committed_work_finalizes_after_restart_without_source_or_catalog(self):
        with patch('series_publication.cleanup', side_effect=Interruption), self.assertRaises(Interruption):
            self.run_job()
        self.queue = ImportQueue(self.base)
        proof, phase = self.queue.publication(self.key)
        self.assertEqual(phase, 2)
        self.assertEqual(self.queue.queued(), [(self.key,self.job)])
        self.src.rename(self.src.with_name('moved'))
        finish_committed(self.fd, self.queue, self.key, proof)
        self.assertIsNone(self.queue.publication(self.key))
        self.assertEqual(self.queue.queued(), [])
        self.assertEqual(sorted(p.name for p in self.stage.iterdir()), ['.ignore'])
        self.assertFalse((self.dst / OWNER).exists())
        self.assertTrue((self.dst / 'Disc/1.mp3').is_file())

    def test_completed_job_never_changes_a_replacement_folder(self):
        with patch('series_publication.cleanup', side_effect=Interruption), self.assertRaises(Interruption):
            self.run_job()
        proof, phase = self.queue.publication(self.key)
        self.assertEqual(phase, 2)
        self.dst.rename(self.dst.with_name('moved'))
        self.dst.mkdir()
        foreign = self.dst / OWNER
        foreign.write_bytes(b'keep')
        finish_committed(self.fd, self.queue, self.key, proof)
        self.assertEqual(foreign.read_bytes(), b'keep')
        self.assertIsNone(self.queue.publication(self.key))
        self.assertEqual(self.queue.queued(), [])


if __name__ == '__main__':
    unittest.main()
