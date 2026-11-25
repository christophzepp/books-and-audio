import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from series_files import components
from series_cleanup import prune_empty_authors
from series_imports import Defer, ImportQueue, from_event, publish
from series_readmeabook import finished_requests, match_requests


class SeriesImportsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'audio-dramas'
        self.root.mkdir()
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        self.queue = ImportQueue(self.base)

    def create(self, source):
        folder = self.root / '.series-import' / source / 'Autor' / 'Serie' / '0007 - Titel'
        folder.mkdir(parents=True)
        audio = folder / '01.m4b'
        audio.write_bytes(b'audio')
        (folder / 'cover.jpg').write_bytes(b'cover')
        path = '/data/audio-dramas/' + str(audio.relative_to(self.root))
        if source == 'shelfmark':
            payload = {'phase': 'post_transfer', 'output': {'mode': 'folder'}, 'task': {'task_id': 't1'}, 'paths': {'final_paths': [path]}}
        else:
            payload = {'eventType': 'Download', 'bookFiles': [{'path': path, 'id': 1, 'size': 5}]}
        return audio, from_event(source, payload)

    def test_native_arr_and_shelfmark_receipts_produce_same_series_and_keep_source(self):
        for source in ('readarr', 'bookshelf', 'chaptarr', 'shelfmark'):
            with self.subTest(source=source):
                audio, job = self.create(source)
                key = self.queue.enqueue(job)
                self.assertTrue(publish(self.fd, key, job))
                self.assertFalse(publish(self.fd, key, job))
                dst = self.root / 'Serie' / 'Folge 007 - Titel'
                self.assertTrue(os.path.samefile(audio, dst / '01.m4b'))
                self.assertTrue(os.path.samefile(audio.parent / 'cover.jpg', dst / 'cover.jpg'))
                for f in dst.iterdir():
                    f.unlink()
                dst.rmdir()

    def test_other_media_tests_grabs_and_incomplete_imports_ignore(self):
        for source in ('readarr', 'bookshelf', 'chaptarr'):
            for event in ('Test', 'Grab', 'AuthorDelete'):
                self.assertIsNone(from_event(source, {'eventType': event}))
            for path in ('/data/audiobooks/Someone/F/1.mp3', '/data/audio-dramas/Unregistered/01.m4b', '/data/ebooks/Something.epub'):
                self.assertIsNone(from_event(source, {'eventType':'Download', 'bookFiles':[{'path':path}]}))
        self.assertIsNone(from_event('shelfmark', {'phase':'post_upload', 'output':{'mode':'booklore'}}))

    def test_non_enrolled_paths_can_never_be_mixed_into_one_event(self):
        with self.assertRaises(ValueError):
            from_event('readarr', {'eventType':'Download','bookFiles':[{'path':'/audio-dramas/.series-import/readarr/a/s/3 - t/1.mp3'},{'path':'/audiobooks/01.mp3'}]})

    def test_conflicting_published_item_never_overwritten_and_no_follow(self):
        audio, j = self.create('readarr')
        key = self.queue.enqueue(j)
        destination = self.root / 'Serie' / 'Folge 007 - Titel'
        destination.mkdir(parents=True)
        (destination / 'other.mp3').write_bytes(b'unchanged')
        with self.assertRaises(Defer):
            publish(self.fd, key, j)
        self.assertEqual((destination / 'other.mp3').read_bytes(), b'unchanged')
        with self.assertRaises((Defer, ValueError)):
            (audio.parent / 'art.jpg').symlink_to('/tmp')
            publish(self.fd, key, j)

    def test_missing_audio_in_event_blocks_whole_import(self):
        audio, job = self.create('readarr')
        (audio.parent / '02.m4b').write_bytes(b'chapter2')
        with self.assertRaises(Defer):
            publish(self.fd, self.queue.enqueue(job), job)
        self.assertFalse((self.root / 'Serie').exists())

    def test_dedupe_and_resume_after_publish_or_restart(self):
        _, j = self.create('shelfmark')
        key = self.queue.enqueue(j)
        publish(self.fd, key, j)
        fresh = ImportQueue(self.base)
        self.assertEqual(key, fresh.enqueue(j))
        self.assertTrue(fresh.known(j['source'], j['folder'], j['revision']))
        self.assertEqual(len(fresh.queued()), 1)
        self.assertFalse(publish(self.fd, key, j))
        fresh.mark(key,'done')
        self.assertEqual(fresh.queued(), [])

    def test_rejects_escape_and_hidden_names(self):
        for invalid in ('/outside', '../outside', 'a/../x', 'a/.', 'a//b', 'a/b/', 'a\\b', 'a/\nx'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                components(invalid)

    def test_listenarr_removes_only_vacated_unowned_empty_ancestors(self):
        old, new = '/audio-dramas/Autor/Serie/Titel', '/audio-dramas/Serie/Folge 007 - Titel'
        (self.root / 'Autor' / 'Serie').mkdir(parents=True)
        book = dict(basePath=new,files=[{'path':new+'/1.mp3'}]);job=dict(status='Completed',sourceRetained=False)
        def prune():
            return prune_empty_authors(old,new,book,job,manifest_verified=True,catalog_base_paths=[new],host_root=self.root)
        job['sourceRetained'] = True
        self.assertEqual(prune(), [])
        job['sourceRetained'] = False
        (self.root/'Autor'/'another').touch()
        self.assertEqual(prune(), ['/audio-dramas/Autor/Serie'])
        self.assertTrue((self.root/'Autor'/'another').is_file())
        (self.root/'Autor'/'another').unlink()
        self.assertEqual(prune(), ['/audio-dramas/Autor'])

    def test_readmeabook_requires_completed_request_unique_asin_and_authorized_library(self):
        def fetch(params):
            return dict(page=1,totalPages=1,requests=[dict(asin='A',status=params['status'],inLibrary=True,type='audiobook',requestId='r',libraryAsin='A'),dict(asin='Wrong',status='processing',inLibrary=True,type='audiobook',requestId='u')])
        done=finished_requests(fetch)
        self.assertEqual(done, {'A':'r'})
        path='/audio-dramas/.series-import/readmeabook/Author/Serie/007 - Titel'
        item=dict(id='one', libraryId='lib',mediaType='book',path=path,updatedAt=2,media=dict(metadata=dict(asin='A'),tracks=[dict(metadata={'path':path+'/01.m4b'})]))
        api=Mock();api.inventory.return_value=[item];api.item.return_value=item
        jobs=list(match_requests(done,api,'lib'))
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['target'],'Serie/Folge 007 - Titel')
        self.queue.enqueue(jobs[0]);api.item.reset_mock()
        self.assertEqual(list(match_requests(done,api,'lib',self.queue.known)),[]);api.item.assert_not_called()
        api.inventory.return_value=[item,dict(item,id='two')]
        self.assertEqual(list(match_requests(done,api,'lib')),[])
