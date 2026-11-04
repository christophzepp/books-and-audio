import copy
import unittest
from test_sync import SyncIntegrationTests, STAMP

class FakeJellyfin:
    def __init__(self):
        self.data=dict(PlaybackPositionTicks=0,Played=False,PlayCount=0)
        self.writes=[];self.fail=False;self.active=set();self.after_write=None
    def guard(self): pass
    def active_ids(self): return self.active
    def items(self,**kw):
        return [dict(Id='a'*32,Type='AudioBook',Path='/data/audiobooks/Example/01.mp3',
                     RunTimeTicks=1000000000,MediaSources=[dict(Path='/data/audiobooks/Example/01.mp3',Size=1000)],UserData=copy.deepcopy(self.data))]
    def update(self,id,values):
        self.writes.append(copy.deepcopy(values))
        if self.fail:raise RuntimeError('simulated uncertain write')
        self.data.update(values)
        if self.after_write:self.after_write()

class Transactions(unittest.TestCase):
    cycle=SyncIntegrationTests.cycle
    def setUp(self):
        SyncIntegrationTests.setUp(self)
        self.jf=FakeJellyfin();self.bridge.jellyfin=self.jf
        self.bridge.config['jellyfin']={'audio_library_id':'lib'}
    def assert_ok(self,r):self.assertFalse(r['blocked'],r)
    def test_initial_abs_then_bidirectional_and_idempotent(self):
        self.abs.set(25);r=self.cycle();self.assert_ok(r)
        self.assertEqual(self.jf.data['PlaybackPositionTicks'],250000000)
        self.assertEqual(self.bo.raw['positionMs'],25000)
        self.jf.data.update(PlaybackPositionTicks=450000000)
        self.assert_ok(self.cycle());self.assertEqual(self.abs.raw['currentTime'],45);self.assertEqual(self.bo.raw['positionMs'],45000)
        self.bo.set(60);self.assert_ok(self.cycle());self.assertEqual(self.jf.data['PlaybackPositionTicks'],600000000)
        counts=(len(self.abs.writes),len(self.bo.writes),len(self.jf.writes))
        self.assert_ok(self.cycle());self.assertEqual(counts,(len(self.abs.writes),len(self.bo.writes),len(self.jf.writes)))
    def test_jellyfin_completion_and_reopening(self):
        self.abs.set(25);self.assert_ok(self.cycle())
        self.jf.data.update(PlaybackPositionTicks=1000000000,Played=True,LastPlayedDate='2026-10-30T12:00:00Z')
        self.assert_ok(self.cycle());self.assertTrue(self.abs.raw['isFinished']);self.assertEqual(self.bo.bd['readStatus']['status'],'read')
        self.jf.data.update(PlaybackPositionTicks=100000000,Played=False)
        self.assert_ok(self.cycle());self.assertFalse(self.abs.raw['isFinished']);self.assertEqual(self.bo.raw['positionMs'],10000)
    def test_abs_completion_and_reopening(self):
        self.abs.set(25);self.assert_ok(self.cycle())
        self.abs.set(25,True,STAMP);self.assert_ok(self.cycle());self.assertTrue(self.jf.data['Played'])
        self.abs.set(10);self.assert_ok(self.cycle());self.assertFalse(self.jf.data['Played'])
    def test_conflicting_edits_do_not_write(self):
        self.abs.set(25);self.assert_ok(self.cycle())
        self.abs.set(30);self.jf.data['PlaybackPositionTicks']=400000000
        counts=(len(self.abs.writes),len(self.bo.writes),len(self.jf.writes))
        self.assertTrue(self.cycle()['blocked']);self.assertEqual(counts,(len(self.abs.writes),len(self.bo.writes),len(self.jf.writes)))
    def test_uncertain_write_never_retried(self):
        self.abs.set(25);self.jf.fail=True
        self.assertTrue(self.cycle()['blocked']);count=len(self.jf.writes)
        self.jf.fail=False;self.assertTrue(self.cycle()['blocked']);self.assertEqual(count,len(self.jf.writes))
    def test_active_jellyfin_player_waits(self):
        self.abs.set(25);self.jf.active={'a'*32}
        r=self.cycle();self.assertEqual(r['waiting'],1);self.assertFalse(self.bo.writes or self.jf.writes)
    def test_parallel_abs_write_is_detected(self):
        self.abs.set(25);self.jf.after_write=lambda:self.abs.set(80)
        self.assertTrue(self.cycle()['blocked'])
    def test_parallel_jellyfin_write_during_bo_update_is_detected(self):
        self.abs.set(25);self.bo.after_write=lambda _:self.jf.data.update(PlaybackPositionTicks=750000000)
        self.assertTrue(self.cycle()['blocked']);self.assertEqual(self.jf.data['PlaybackPositionTicks'],750000000)

if __name__=='__main__':unittest.main()
