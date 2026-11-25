from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import series_listenarr as n


class NativeAPI:
    def __init__(self):
        self.old = '/audio-dramas/Autor/Serie/Titel'
        self.new = '/audio-dramas/Serie/Folge 007 - Titel'
        self.path = self.old
        self.calls = []
        self.catalog_file_count = 1
        self.blocked_download = False
        self.extra = []

    def row(self):
        return dict(id=7,basePath=self.path,series='Serie',title='Titel',seriesNumber='7',authors=['Autor'],asin='ABC',fileCount=self.catalog_file_count)

    def call(self, route, body=None, query=None):
        self.calls.append((route, body, query))
        if route == 'library':
            return [self.row(), *self.extra]
        if route == 'library/7':
            return dict(self.row(),files=[dict(id=42,size=100,durationSeconds=10,path=self.path+'/01.mp3')])
        if route == 'configuration/settings':
            return {'folderNamingPattern': n.PATTERN}
        if route == 'rootfolders':
            return [{'path':'/audiobooks'}, {'path':'/audio-dramas'}]
        if route == 'library/preview-path':
            return dict(root='/audio-dramas',fullPath=self.old)
        if route == 'downloads':
            return [{'audiobookId':7,'status':'downloading'}] if self.blocked_download else []
        if route == 'library/7/move/recovery':
            return {'hasUnresolvedMove':False}
        if route == 'filesystem/browse':
            path=query['path']
            if path == self.path:
                return dict(currentPath=path,items=[dict(path=path+'/01.mp3',name='01.mp3',isDirectory=False)])
            if path == '/audio-dramas':
                return dict(currentPath=path,items=[dict(path=path+'/Autor',name='Autor',isDirectory=True)])
            raise n.urllib.error.HTTPError('',404,'missing',None,None)
        if route == 'library/7/move':
            assert body == {'sourcePath':self.old,'destinationPath':self.new,'moveFiles':True,'deleteEmptySource':True}
            assert n.PENDING.exists()
            self.path=self.new
            return {'jobId':'11111111-1111-1111-1111-111111111111'}
        if route.startswith('library/move/'):
            return dict(status='Completed',sourceRetained=False)
        raise AssertionError(route)


class NativeSeriesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name);self.api=NativeAPI()
        for p in (patch.object(n,'PENDING',self.state/'offener-auftrag.json'),patch.object(n,'MAINTENANCE_LOCKS',()),patch.dict(n.os.environ,{'SERIES_DRAMA_MOUNT':str(self.state)})):
            p.start();self.addCleanup(p.stop)

    def test_native_move_checks_again_after_change_and_never_replays(self):
        (self.state/'Autor'/'Serie').mkdir(parents=True)
        result=n.run(self.api,True)
        self.assertEqual(result, dict(moved=1,empty_dirs_removed=2,skipped=0,conflicts=0))
        self.assertFalse(n.PENDING.exists());self.assertFalse((self.state/'Autor').exists())
        self.api.calls.clear();self.assertEqual(n.run(self.api,True)['skipped'],1)
        self.assertEqual([x[0] for x in self.api.calls],['library'])

    def test_no_media_calls_for_books_or_pending_downloads(self):
        self.api.catalog_file_count=0
        self.api.extra=[dict(self.api.row(),id=99,basePath='/audiobooks/Autor/Serie/Buch')]
        out=n.run(self.api,True)
        self.assertEqual(out['moved'],0)
        self.assertEqual([x[0] for x in self.api.calls],['library'])

    def test_in_progress_download_stops_before_filesystem_browse_or_move(self):
        self.api.blocked_download=True
        self.assertEqual(n.run(self.api,True)['conflicts'],1)
        self.assertFalse(any(x[0] in ('filesystem/browse','library/7/move') for x in self.api.calls))

    def test_duplicate_series_target_reserved_even_if_other_episode_has_no_files(self):
        self.api.extra=[dict(self.api.row(),id=9,basePath='/audio-dramas/Anderer/Serie/Titel',fileCount=0)]
        self.assertEqual(n.run(self.api,True)['conflicts'],1)
        self.assertFalse(any(x[0]=='library/7/move' for x in self.api.calls))

    def test_resume_completed_job_before_discovery_without_double_post(self):
        self.api.path=self.api.new
        n.save_pending(7,self.api.old,self.api.new,{'01.mp3':(42,100,10)},{'01.mp3':False})
        n.remember_job_id(7,self.api.old,self.api.new,'11111111-1111-1111-1111-111111111111')
        out=n.run(self.api,True)
        self.assertEqual(out['moved'],1)
        self.assertFalse(any(x[0]=='library/7/move' for x in self.api.calls))
        self.assertFalse(n.PENDING.exists())
