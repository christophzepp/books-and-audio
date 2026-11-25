import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from series_arr import resolve
from series_imports import ARR, Defer, ImportQueue, from_event, publish_arr


class NativeArrTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY); self.addCleanup(os.close,self.fd)

    def fixture(self, source):
        base = '/data/audio-dramas/.series-import/' + source + '/Author'
        tracks = [dict(path=base+'/downloaded title.m4b',bookId=5,size=5,id=7)]
        event = dict(eventType='Download',book=dict(id=5),author=dict(id=9),bookFiles=tracks)
        with patch.dict(os.environ,{'SERIES_'+source.upper()+'_ORIGIN':'http://private'}):
            job = from_event(source,event)
        records = {'book/5':dict(id=5,authorId=9,title='Ein Titel'), 'series':[dict(id=2,title='Serienname',links=[dict(seriesId=2,bookId=5,position='3.5')])], 'bookfile':tracks}
        return job, records

    def test_all_three_native_catalogs_support_unstructured_files_and_multi_author_series(self):
        for source in ARR:
            job,data = self.fixture(source)
            folder,target,sizes = result = resolve(job,lambda route,params:data[route])
            self.assertEqual((folder,target,sizes),('Author','Serienname/Folge 003.5 - Ein Titel',{'downloaded title.m4b':5}))
            base=self.root/'.series-import'/source/'Author';base.mkdir(parents=True)
            original=base/'downloaded title.m4b';original.write_bytes(b'12345');(base/'another book.mp3').write_bytes(b'other')
            key=ImportQueue(self.root).enqueue(job)
            self.assertTrue(publish_arr(self.fd,key,job,result))
            dst=self.root/target
            self.assertTrue(os.path.samefile(dst/original.name, original))
            self.assertEqual([f.name for f in dst.iterdir()],[original.name])
            self.assertFalse(publish_arr(self.fd,key,job,result))
            (dst/original.name).unlink();dst.rmdir()

    def test_multiple_series_or_rebound_file_rejected(self):
        job, data = self.fixture('chaptarr')
        data['series'].append(dict(id=3,title='Anders',links=[dict(seriesId=3,bookId=5,position='2')]))
        with self.assertRaises(Defer): resolve(job,lambda route,params:data[route])
        data['series'].pop(); data['bookfile'][0]['id'] = 101
        with self.assertRaises(Defer): resolve(job,lambda route,params:data[route])
