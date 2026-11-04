import unittest
from unittest.mock import Mock
from episode_editor import candidate, ReviewError

class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.p=Mock()
        self.p._ensure_native.return_value=3
        self.m=dict(feed_url='http://local/feed',enclosure_url='http://local/ep',show='Sendung',title='Titel')
        self.pods=[dict(feedurl='http://local/feed',podcastid=2,podcastname='Sendung')]
    def search(self,rows,old=None):
        self.p._request.side_effect=[{'pods':self.pods},{'total':len(rows),'episodes':rows}]
        return candidate(self.p,self.m,old)
    def test_ambiguous_title_in_same_feed_never_selects_first(self):
        self.assertIsNone(self.search([dict(Episodeid=i,Episodeurl='http://different/'+str(i),Episodetitle='Titel') for i in (1,2)]))
    def test_changed_feed_requires_independent_episode_binding(self):
        self.pods[0]['feedurl']='http://new/feed'
        self.assertIsNone(self.search([dict(Episodeid=5,Episodeurl='http://new/ep',Episodetitle='Titel')]))
        self.assertEqual(self.search([dict(Episodeid=5,Episodeurl='http://local/ep',Episodetitle='Titel')])['native_episode_id'],5)
    def test_earlier_id_cannot_be_replaced_by_matching_title(self):
        self.assertIsNone(self.search([dict(Episodeid=5,Episodeurl='http://local/ep',Episodetitle='Titel')],9))
    def test_incomplete_inventory_never_selects(self):
        self.p._request.side_effect=[{'pods':self.pods},{'total':9,'episodes':[]}]
        with self.assertRaises(ReviewError):candidate(self.p,self.m)

class AssignedStatusTests(unittest.TestCase):
    from test_podcast_status_sync import StatusTests as fixture
    setUp=fixture.setUp
    write_gate=fixture.write_gate
    load=fixture.load
    save=fixture.save
    write_record=fixture.write_record
    setup_variant=fixture.setup_variant
    cycle=fixture.cycle
    okay=fixture.okay
    advance=fixture.advance
    settle_all=fixture.settle_all

    def test_mark_heard_and_reset_across_all_four_services(self):
        from podcast_status_sync import state_key
        from podcast_sync import fingerprint
        self.setup_variant()
        self.okay(self.cycle())
        key=state_key('_podcast:show:episode')
        for target in ('finished','not_started'):
            self.advance()
            state=self.load(key)
            state['reviewed_source']=dict(target=target,snapshot=fingerprint(state['raw']))
            self.save(key,state)
            self.settle_all()
            self.assertEqual({row['status'] for row in self.load(key)['baseline'].values()},{target})
            self.assertEqual(self.pine.completed,target=='finished')
            if target=='not_started':self.assertEqual(self.pine.position,0)

class QueueTests(unittest.TestCase):
    def test_preview_can_pass_episode_but_not_connection_change(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from cryptography.fernet import Fernet
        from admin_store import AdminStore
        with TemporaryDirectory() as td:
            key=Path(td)/'k';key.write_bytes(Fernet.generate_key());s=AdminStore(td,key)
            a=s.submit('episode.status_apply',{})['id'];b=s.submit('review.preview',{})['id']
            self.assertEqual(s.claim(preview_only=True)['id'],b)
            self.assertIsNone(s.claim(preview_only=True))
            self.assertEqual(s.claim()['id'],a)
            s.submit('connection.activate',{});s.submit('review.preview',{})
            self.assertIsNone(s.claim(preview_only=True))
