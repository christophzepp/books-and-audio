import contextlib
import io
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from admin_store import AdminStore
import bookstart_review as bookstart
import test_sync
import test_jellyfin_ebook
import test_provider_runtime
from test_jellyfin_transactions import FakeJellyfin


class AudioReviewTests(unittest.TestCase):
    def setUp(self):
        test_sync.SyncIntegrationTests.setUp(self)
        self.bridge.config['auto_discovery'] = dict(abs_library_id='abs-lib',bo_library_ids=[1])
        self.abs.ai['libraryId'] = 'abs-lib'
        self.abs._request = lambda *_: {'id':'abs-user'}
        self.bo._request = lambda *_: {'id':'bo-user'}
        key = self.root/'key'
        key.write_bytes(Fernet.generate_key())
        self.store = AdminStore(self.root,key)
        profiles = [dict(id=k,kind=k,active=True,identity={'user_id':user}) for k,user in
                    [('audiobookshelf','abs-user'),('bookorbit','bo-user'),('jellyfin','jf-user')]]
        self.state = dict(revision=0,profiles=profiles)
        self.admin = SimpleNamespace(store=self.store,root=self.root,manager=SimpleNamespace(_read=lambda:(self.state,None)))
        self.abs.set(30)
        self.bo.set(65)

    def command(self, kind, payload):
        ident = self.store.submit(kind,payload)['id']
        self.store.claim()
        with contextlib.redirect_stdout(io.StringIO()):
            bookstart.process(self.admin,self.bridge,kind,payload,ident)
        return self.store.command(ident)

    def preview(self):
        cmd = self.command('bookstart.preview',{'id':'abs-book','media':'audiobook'})
        self.assertEqual(cmd['status'],'success',cmd)
        return cmd['result']

    def apply(self, preview, source):
        data = {k:preview[k] for k in ('digest','review_id')}
        return self.command('bookstart.apply',dict(data,source=source,confirm=True))

    def test_two_sources_apply_once_with_journal(self):
        preview = self.preview()
        self.assertEqual([(s['source'],s['position']) for s in preview['states']], [('abs',30),('bo',65)])
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertTrue(self.apply(preview,'abs')['result']['applied'])
        self.assertEqual(self.bo.raw['positionMs'],30000)
        count = len(self.bo.writes)
        self.assertEqual(self.apply(preview,'abs')['status'],'failed')
        self.assertEqual(len(self.bo.writes),count)
        self.assertEqual(self.bridge.db.execute("SELECT count(*) FROM operations WHERE status='complete'").fetchone()[0],1)

    def test_same_position_with_new_revision_invalidates_preview(self):
        review = self.preview()
        self.bo.raw['revision'] += 1
        self.assertEqual(self.apply(review,'bo')['status'],'failed')
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_third_source_and_scope_rechecked(self):
        jf = FakeJellyfin()
        jf.user = 'jf-user'
        self.bridge.jellyfin = jf
        self.bridge.config['jellyfin'] = dict(enabled=True,audio_library_id='jf-lib')
        self.bridge.config['_bookorbit_activation'] = 'new-scope'
        jf.data['PlaybackPositionTicks'] = 780000000
        review = self.preview()
        self.assertEqual(len(review['states']),3)
        jf.data['PlaybackPositionTicks'] = 760000000
        self.assertEqual(self.apply(review,'abs')['status'],'failed')
        self.assertFalse(self.abs.writes or self.bo.writes or jf.writes)
        review = self.preview()
        self.assertEqual(self.apply(review,'jf')['status'],'success')
        self.assertEqual((self.abs.raw['currentTime'],self.bo.raw['positionMs']), (76,76000))
        self.assertFalse(jf.writes)
        self.assertEqual(self.bridge.load('_bookorbit_scope_seen:audio-jf:abs-book')['activation'],'new-scope')

    def test_open_journal_pause_account_revision_or_expiry_blocks(self):
        review = self.preview()
        self.store.pause(True)
        self.assertEqual(self.apply(review,'abs')['status'],'failed')
        self.store.pause(False)
        self.bo._request = lambda *_: {'id':'other'}
        self.assertEqual(self.apply(review,'abs')['status'],'failed')
        self.bo._request = lambda *_: {'id':'bo-user'}
        self.state['revision'] = 1
        self.assertEqual(self.apply(review,'abs')['status'],'failed')
        self.state['revision'] = 0
        with patch.object(bookstart.time,'time',return_value=time.time()+301):
            self.assertEqual(self.apply(review,'abs')['status'],'failed')
        self.bridge.write_record('pending','abs-book',{})
        self.assertEqual(self.apply(review,'abs')['status'],'failed')
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_established_conflict_cannot_be_approved_as_first(self):
        pair,_ = self.bridge.read_pair(self.mapping)
        pair['abs']['position'] = pair['bo']['position'] = 40
        self.bridge.save('abs-book',dict(baseline=pair))
        self.assertEqual(self.command('bookstart.preview',dict(id='abs-book',media='audiobook'))['status'],'failed')
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_bookorbit_source_can_be_earlier_than_abs_and_jellyfin(self):
        jf = FakeJellyfin()
        jf.user = 'jf-user'
        jf.data['PlaybackPositionTicks'] = 800000000
        self.bridge.jellyfin = jf
        self.bridge.config['jellyfin'] = dict(enabled=True,audio_library_id='jf-lib')
        self.abs.set(70)
        self.bo.set(25)
        result = self.apply(self.preview(),'bo')
        self.assertEqual(result['status'],'success',result)
        self.assertEqual(self.abs.raw['currentTime'],25)
        self.assertEqual(jf.data['PlaybackPositionTicks'],250000000)
        self.assertFalse(self.bo.writes)

    def test_changed_jellyfin_during_abs_to_bookorbit_stops_transfer_and_replay(self):
        jf = FakeJellyfin()
        jf.user = 'jf-user'
        jf.data['PlaybackPositionTicks'] = 800000000
        self.bridge.jellyfin = jf
        self.bridge.config['jellyfin'] = dict(enabled=True,audio_library_id='jf-lib')
        preview = self.preview()
        self.bo.after_write = lambda _: jf.data.update(PlaybackPositionTicks=120000000)
        self.assertEqual(self.apply(preview,'abs')['status'],'failed')
        self.assertTrue(self.bridge.has_pending_transfer('abs-book'))
        self.assertFalse(jf.writes)
        writes = len(self.bo.writes)
        self.assertEqual(self.apply(preview,'abs')['status'],'failed')
        self.assertEqual(len(self.bo.writes),writes)
        self.assertEqual(jf.data['PlaybackPositionTicks'],120000000)

    def test_position_zero_cannot_replace_existing_progress(self):
        self.abs.set(0)
        self.bridge.config['_bookorbit_activation'] = 'changed-account'
        review = self.preview()
        self.assertFalse(next(s['selectable'] for s in review['states'] if s['source'] == 'abs'))
        self.assertEqual(self.apply(review,'abs')['status'],'failed')
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertEqual(self.apply(review,'bo')['status'],'success')
        self.assertEqual(self.abs.raw['currentTime'],65)

    def test_jellyfin_source_cannot_partially_write_unrepresentable_progress(self):
        jf = FakeJellyfin()
        jf.user = 'jf-user'
        jf.data['PlaybackPositionTicks'] = 990000000
        self.bridge.jellyfin = jf
        self.bridge.config['jellyfin'] = dict(enabled=True,audio_library_id='jf-lib')
        preview = self.preview()
        row = next(s for s in preview['states'] if s['source'] == 'jf')
        self.assertFalse(row['selectable'])
        self.assertIn('BookOrbit',row['reason'])
        self.assertEqual(self.apply(preview,'jf')['status'],'failed')
        self.assertFalse(self.abs.writes or self.bo.writes or jf.writes)
        self.assertFalse(self.bridge.has_pending_transfer('abs-book'))

    def test_jellyfin_to_abs_stops_on_concurrent_target_edit_before_next_write(self):
        jf = FakeJellyfin()
        jf.user = 'jf-user'
        jf.data.update(PlaybackPositionTicks=900000000,Played=True,LastPlayedDate='2026-10-30T12:00:00Z')
        self.bridge.jellyfin = jf
        self.bridge.config['jellyfin'] = dict(enabled=True,audio_library_id='jf-lib')
        preview = self.preview()
        self.abs.after_write = lambda _: self.abs.set(18)
        self.assertEqual(self.apply(preview,'jf')['status'],'failed')
        self.assertEqual(self.abs.writes,[('position',90)])
        self.assertFalse(self.bo.writes or jf.writes)
        self.assertTrue(self.bridge.has_pending_transfer('abs-book'))


class BookBridgeReviewTests(unittest.TestCase):
    def setUp(self):
        test_provider_runtime.Tests.setUp(self)
        self.bridge = self.b
        self.abs._request = lambda *_: {'id':'abs-user','username':'reader'}
        self.jf.user = 'jf-user'
        key = self.root/'key'
        key.write_bytes(Fernet.generate_key())
        self.store = AdminStore(self.root,key)
        state = dict(revision=0,profiles=[
            dict(id='abs',kind='audiobookshelf',active=True,identity={'user_id':'abs-user'}),
            dict(id='jf',kind='jellyfin',active=True,identity={'user_id':'jf-user'}),
            dict(id='bb',kind='bookbridge',active=True,settings={'username':'user','version':'7.11.0'})])
        self.admin = SimpleNamespace(store=self.store,root=self.root,manager=SimpleNamespace(_read=lambda:(state,None)))
        self.abs.set(20)
        self.jf.data.update(PlaybackPositionTicks=420000000)
        report = test_provider_runtime.Tests.cycle(self)
        self.assertTrue(report['blocked'][0]['initial_review'])

    command = AudioReviewTests.command
    preview = AudioReviewTests.preview
    apply = AudioReviewTests.apply

    def test_paired_audio_abs_source_only_updates_jellyfin(self):
        preview = self.preview()
        self.assertEqual({r['source'] for r in preview['states']},{'abs','jf'})
        self.assertEqual(self.apply(preview,'bo')['status'],'failed')
        self.assertEqual(self.apply(preview,'abs')['status'],'success')
        self.assertEqual(self.jf.data['PlaybackPositionTicks'],200000000)
        self.assertIsNone(self.b.bo)
        self.assertFalse(self.abs.writes)

    def test_paired_audio_jellyfin_source_and_changed_mapping(self):
        preview = self.preview()
        self.bb.pairs[0]['ebook_path'] = '/ebooks/new.epub'
        self.assertEqual(self.apply(preview,'jf')['status'],'failed')
        self.assertFalse(self.abs.writes)
        self.bb.pairs[0]['ebook_path'] = '/ebooks/Example.epub'
        self.assertEqual(self.apply(preview,'jf')['status'],'success')
        self.assertEqual(self.abs.raw['currentTime'],42)
        self.assertTrue(self.b.load('_bookbridge_audio:abs-book')['baseline'])
        self.assertFalse(self.b.load('abs-book'))


class EbookReviewTests(unittest.TestCase):
    def setUp(self):
        test_jellyfin_ebook.EbookTests.setUp(self)
        self.bridge = self.c.b
        self.bridge.config = dict(_bookorbit_activation='scope')
        self.bridge._book_observations = None
        self.review = dict(request={'media':'ebook','id':'ebook:1'},scope=['scope'])
        test_jellyfin_ebook.EbookTests.set_bo(self,3)
        test_jellyfin_ebook.EbookTests.set_jf(self,5)

    request = test_jellyfin_ebook.EbookTests.request
    update = test_jellyfin_ebook.EbookTests.update
    write_status = test_jellyfin_ebook.EbookTests.write_status

    def preview(self):
        with self.assertRaises(bookstart.PreviewReady) as cm:
            bookstart.run(self.bridge,self.m,self.review)
        return cm.exception.result

    def test_percent_sources_apply_confirmation(self):
        result = self.preview()
        self.assertEqual([s['unit'] for s in result['states']],['percent','percent'])
        self.assertAlmostEqual(result['states'][1]['position'],100*5/self.e.total,places=3)
        review = dict(self.review,digest=result['digest'],source='jf',expires_at=time.time()+300)
        self.assertFalse(self.writes)
        self.assertEqual(bookstart.run(self.bridge,self.m,review),'synced')
        self.assertEqual(self.e.bo_to_index(self.progress['text']['cfi']),5)
        self.assertIn('_bookorbit_scope_seen:_jellyfin_ebook:1',self.store)
        self.assertEqual(self.c.handle(self.m,True,0),'unchanged')

    def test_scoped_zero_source_and_stale_change_refused(self):
        self.user.update(PlaybackPositionTicks=0,Played=False,PlayCount=0)
        result = self.preview()
        self.assertFalse(next(s['selectable'] for s in result['states'] if s['source']=='jf'))
        review = dict(self.review,digest=result['digest'],source='jf',expires_at=time.time()+300)
        with self.assertRaises(bookstart.ReviewError):
            bookstart.run(self.bridge,self.m,review)
        self.user['PlaybackPositionTicks'] = self.e.index_to_ticks(2)
        with self.assertRaises(bookstart.ReviewError):
            bookstart.run(self.bridge,self.m,dict(review,source='bo'))
        self.assertFalse(self.writes)

    def test_ebook_bookorbit_source_verifies_jellyfin_readback(self):
        result = self.preview()
        review = dict(self.review,digest=result['digest'],source='bo',expires_at=time.time()+300)
        self.assertEqual(bookstart.run(self.bridge,self.m,review),'synced')
        self.assertEqual(self.e.ticks_to_index(self.user['PlaybackPositionTicks']),3)
        self.assertEqual(self.writes,['jf'])

    def test_ebook_rejects_concurrent_source_and_will_not_repeat_partial_write(self):
        result = self.preview()
        review = dict(self.review,digest=result['digest'],source='jf',expires_at=time.time()+300)
        self.concurrent = True
        with self.assertRaisesRegex(RuntimeError,'not confirmed'):
            bookstart.run(self.bridge,self.m,review)
        with self.assertRaisesRegex(bookstart.ReviewError,'Journal'):
            bookstart.run(self.bridge,self.m,review)
        self.assertEqual(self.writes,['bo'])

    def test_incompatible_cfi_cannot_be_approved(self):
        result = self.preview()
        review = dict(self.review,digest=result['digest'],source='jf',expires_at=time.time()+300)
        self.progress['text']['cfi'] = 'epubcfi(/7999!/2/1:0)'
        with self.assertRaises(ValueError):
            bookstart.run(self.bridge,self.m,review)
        self.assertFalse(self.writes)


if __name__ == '__main__':
    unittest.main()
