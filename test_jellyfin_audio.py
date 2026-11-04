import copy
import unittest
from jellyfin_audio import choose, desired, normalize, bind, TICKS

def state(pos=0,status='not_started',fingerprint='x'):
    return dict(position=pos,status=status,fingerprint=fingerprint)

class Decisions(unittest.TestCase):
    def test_single_initial_progress(self):
        for source in ('abs','bo','jf'):
            s={k:state() for k in ('abs','bo','jf')};s[source]=state(45,'in_progress')
            self.assertEqual(choose(s),source)
    def test_initial_agreed_pair(self):
        self.assertEqual(choose(dict(abs=state(40,'in_progress'),bo=state(40,'in_progress'),jf=state())),'abs')
    def test_conflict_not_highest_wins(self):
        s=dict(abs=state(90,'in_progress'),bo=state(70,'in_progress'),jf=state())
        with self.assertRaises(ValueError): choose(s)
    def test_backward_seek_and_reopening(self):
        old={k:state(50,'finished') for k in ('abs','bo','jf')}
        new=copy.deepcopy(old);new['jf']=state(10,'in_progress')
        self.assertEqual(choose(new,old),'jf')
    def test_two_conflicting_changes(self):
        old={k:state(50,'in_progress') for k in ('abs','bo','jf')};new=copy.deepcopy(old)
        new['jf']=state(10,'in_progress');new['bo']=state(70,'in_progress')
        with self.assertRaises(ValueError):choose(new,old)
    def test_deletion_blocked(self):
        old={k:state(50,'in_progress') for k in ('abs','bo','jf')};new=copy.deepcopy(old);new['jf']=state()
        with self.assertRaises(ValueError):choose(new,old)
    def test_identity_change(self):
        old={k:state() for k in ('abs','bo','jf')};new=copy.deepcopy(old);new['jf']['fingerprint']='y'
        with self.assertRaises(ValueError):choose(new,old)

class Chapters(unittest.TestCase):
    def setUp(self):
        self.tracks=[dict(id=str(i),start=i*100,duration=100) for i in range(3)]
        self.rows=[dict(Played=False,PlaybackPositionTicks=0,PlayCount=0,LastPlayedDate=None) for i in range(3)]
    def test_multi_track_roundtrips(self):
        for p in (0,45,100,145.35,299,300):
            for s in ('in_progress','finished'):
                d=desired(self.rows,self.tracks,state(p,s)); n=normalize(d,self.tracks)
                self.assertAlmostEqual(n['position'],p,places=6);self.assertEqual(n['status'],s)
    def test_short_track_supported(self):
        n=normalize(desired(self.rows,self.tracks,state(15,'in_progress')),self.tracks)
        self.assertEqual(n['position'],15)
    def test_write_confirmation_compares_timestamp_instants(self):
        from jellyfin_audio import rows_equal
        a=[dict(Played=True,LastPlayedDate='2026-10-29T08:01:46.546000+00:00')]
        b=[dict(Played=True,LastPlayedDate='2026-10-29T09:01:46.546+01:00')]
        self.assertTrue(rows_equal(a,b))
        b[0]['Played']=False
        self.assertFalse(rows_equal(a,b))
    def test_track_completion_not_book_completion(self):
        self.rows[0]['Played']=True
        self.assertEqual(normalize(self.rows,self.tracks)['position'],100)
        self.assertEqual(normalize(self.rows,self.tracks)['status'],'in_progress')
    def test_all_completed(self):
        for d in self.rows:d['Played']=True
        self.assertEqual(normalize(self.rows,self.tracks)['status'],'finished')
    def test_completion_keeps_unheard_chapter_dates(self):
        rows=desired(self.rows,self.tracks,state(45,'finished'),'2026-10-30T20:00:00Z')
        self.assertTrue(all(x['Played'] for x in rows))
        self.assertIsNone(rows[1]['LastPlayedDate'])
        self.assertIsNone(rows[2]['LastPlayedDate'])
        self.assertEqual(rows[0]['LastPlayedDate'],'2026-10-30T20:00:00Z')
    def test_progress_keeps_finished_chapter_history(self):
        old=desired(self.rows,self.tracks,state(145),'2026-10-29T20:00:00Z')
        new=desired(old,self.tracks,state(155),'2026-10-30T20:00:00Z')
        self.assertEqual(new[0],old[0])
        self.assertEqual(new[1]['LastPlayedDate'],'2026-10-30T20:00:00Z')
    def test_ambiguous_positions(self):
        self.rows[0]['PlaybackPositionTicks']=10*TICKS;self.rows[2]['PlaybackPositionTicks']=20*TICKS
        with self.assertRaises(ValueError):normalize(self.rows,self.tracks)
    def test_latest_changed_track_can_go_backward(self):
        old=copy.deepcopy(self.rows);old[2]['PlaybackPositionTicks']=20*TICKS
        new=copy.deepcopy(old);new[0]['PlaybackPositionTicks']=10*TICKS
        self.assertEqual(normalize(new,self.tracks,old)['position'],10)
    def test_gap_is_not_completion(self):
        self.rows[2]['Played']=True
        with self.assertRaises(ValueError):normalize(self.rows,self.tracks)

if __name__=='__main__':unittest.main()
