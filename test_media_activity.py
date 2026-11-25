from contextlib import closing
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import media_activity as a
from io_metrics import increment
import watcher_import


class MediaActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.j = a.Journal(self.root)
        self.now = 2000000000

    def test_restart_idle_and_no_fake_measurements(self):
        self.assertIsNone(self.j.report(1, now=self.now)['first_cycle'])
        self.assertFalse(self.j.path.exists())
        with a.recording(lambda:self.now) as batch:
            a.count('cycles')
            increment(SimpleNamespace(), 'api_observation_requests', 200)
            self.j.flush(batch)
            self.j.flush(batch)
        r=a.Journal(self.root).report(1,now=self.now)
        self.assertEqual(r['totals'], {'cycles':1,'api':200})
        self.assertEqual((r['watcher'],r['details']), ([],[]))

    def test_true_timestamp_title_and_keyset_pages_during_new_events(self):
        with a.recording(lambda:self.now) as batch:
            for n in range(106):
                a.record('podcast', 'Folge '+str(n), 'duration')
            self.j.flush(batch)
            one=self.j.report(7, now=self.now)
            a.record('series', 'Neu', 'series_import')
            self.j.flush(batch)
        rest=self.j.report(7, before=one['next'],now=self.now)
        self.assertEqual([r['title'] for r in one['details']+rest['details']],
                         ['Folge '+str(n) for n in reversed(range(106))])
        self.assertEqual(one['totals']['checks'],106)
        self.assertEqual(one['details'][0]['at'],self.now)
        self.assertIsNone(rest['next'])

    def test_retention_and_separate_rolling_windows(self):
        at=[self.now-8*86400]
        with a.recording(lambda:at[0]) as b:
            for day in (8,3,0):
                at[0]=self.now-day*86400
                a.record('ebook','Tag '+str(day),'discovery')
                self.j.flush(b)
        self.assertEqual(self.j.report(1,now=self.now)['totals']['checks'],1)
        self.assertEqual(self.j.report(7,now=self.now)['totals']['checks'],2)

    def test_series_packet_survives_retry_and_replay(self):
        d=self.root/'series'
        d.mkdir()
        with a.recording(lambda:self.now) as b:
            a.record('series','Testfolge','series_target')
            a.count('series_runs')
            a.spool_series(d,b)
        path,=d.glob('*.json')
        raw=path.read_bytes()
        self.j.import_series(d,self.now)
        path.write_bytes(raw)
        self.j.import_series(d,self.now)
        self.assertEqual(self.j.report(1,now=self.now)['totals'], {'checks':1,'series_runs':1})
        self.assertFalse(path.exists())

    def test_failed_flush_keeps_diagnostics_and_does_not_block_work(self):
        with a.recording(lambda:self.now) as b:
            a.record('folder','Podcasts','mount')
            with patch.object(self.j, 'save', side_effect=OSError()), self.assertLogs(level='WARNING'):
                self.j.flush(b)
            self.j.flush(b)
        self.assertEqual(self.j.report(1,now=self.now)['details'][0]['title'],'Podcasts')

    def test_watcher_distinguishes_measured_idle_from_missing_and_skips_resets(self):
        def dump(at,r,w):
            return json.dumps(dict(utc=at,kind='diskstats', disks=[{
                'role':'data','kernel_dev':123,'values':[r,0,0,0,w,0,0,0,0,0,0]}]))+'\n'
        start=self.now//60*60-300
        source=''.join(dump(start+n,r,w) for n,r,w in [(0,10,5),(5,10,5),(65,12,6),(70,0,0),(250,99,99)])
        self.assertEqual(watcher_import.update(StringIO(source), self.root,self.now),2)
        watcher_import.update(StringIO(source), self.root,self.now)
        self.assertEqual(self.j.report(1,now=self.now)['watcher'],[
            {'at':start,'reads':0,'writes':0},{'at':start+60,'reads':2,'writes':1}])

    def test_oldest_minute_uses_one_boundary_across_sources_and_no_old_records_are_kept(self):
        cutoff=self.now-a.WEEK
        oldest=(cutoff+59)//60*60
        for at in (oldest-60,oldest):
            with a.recording(lambda:at) as b:
                a.record('podcast','Earliest','duration')
                a.count('cycles')
                self.j.flush(b)
        with a.recording(lambda:self.now) as b:
            a.count('cycles')
            self.j.flush(b)
        (self.root/'disk-observations.json').write_text(json.dumps({'schema':1,'minutes':[[oldest-60,1,0],[oldest,1,0]]}))
        report=self.j.report(7,now=self.now)
        self.assertGreaterEqual(report['from'],cutoff)
        self.assertEqual(report['first_cycle'],oldest)
        self.assertEqual(report['totals'],{'checks':1,'cycles':2})
        self.assertEqual(report['watcher'],[{'at':oldest,'reads':1,'writes':0}])
        self.assertEqual([entry['at'] for entry in report['details']],[oldest])
        with closing(a.sqlite3.connect(self.j.path)) as db:
            self.assertEqual(db.execute('SELECT MIN(at) FROM details').fetchone(),(oldest,))
        with self.assertRaises(ValueError):
            self.j.report(8,now=self.now)

    def test_normal_worker_flush_and_authenticated_web_path(self):
        import admin_runtime
        import test_admin_web
        from types import SimpleNamespace
        case=test_admin_web.WebTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        control=case.root/'control'
        control.mkdir()
        case.store.control=control
        def cycle(*unused):
            a.record('folder','Medien','mount')
            a.count('cycles')
        with patch.object(admin_runtime,'_admin_loop',side_effect=cycle):
            admin_runtime.run_admin_loop(SimpleNamespace(),case.root,{}, {},None)
        from disk_activity import Sampler
        minute=int(a.time.time())//60*60
        Sampler(control).save(minute,17,9)
        (control/'disk-observations.json').write_text(json.dumps({'schema':1,'minutes':[[minute,0,0]]}))
        self.assertEqual(case.request('GET','/api/media-activity/7')[0],401)
        case.bootstrap()
        for days in (1,7):
            code,raw,headers=case.request('GET','/api/media-activity/'+str(days))
            self.assertEqual(code,200)
            report=json.loads(raw)
            self.assertEqual(report['totals'],{'checks':1,'cycles':1})
            self.assertEqual(report['details'][0]['title'],'Medien')
            self.assertIn(('cache-control','no-store'),headers)
            self.assertEqual(report['watcher'],[{'at':minute,'reads':0,'writes':0}])
            self.assertEqual(report['kernel_watcher'],[{'at':minute,'reads':17,'writes':9}])
        for path in ('/api/media-activity/8','/api/media-activity/7/-1-2','/api/media-activity/7/1-0'):
            self.assertEqual(case.request('GET',path)[0],404)
        self.assertEqual(case.request('GET','/api/media-activity/7',host='public.invalid')[0],403)
        case.request('POST','/api/logout',{})
        self.assertEqual(case.request('GET','/api/media-activity/1')[0],401)

    def test_delayed_series_import_at_capacity_keeps_most_recent_measurements(self):
        with patch.object(a,'MAX_DETAILS',2):
            for at,title in ((self.now-5,'A'),(self.now-4,'B'),(self.now-500,'Alter Import')):
                self.j.save({'details':[(at,'series',title,'series_target')],'counts':[]},self.now)
        with closing(a.sqlite3.connect(self.j.path)) as db:
            self.assertEqual(db.execute('SELECT title FROM details ORDER BY at DESC').fetchall(),[('B',),('A',)])
