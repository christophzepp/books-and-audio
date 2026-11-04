import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sync
from provider_runtime import BookBridgeCoordinator, provider_preview, build_audio_mapping
from test_sync import FakeABS, fixture, STAMP
from test_jellyfin_transactions import FakeJellyfin


class BB:
    def __init__(self):
        self.pairs = [dict(id='abs-book', abs_id='abs-book', audio_source='ABS', audio_source_id='abs-book',
                           title='Example', ebook_path='/ebooks/Example.epub', ebook_source='Local', ebook_source_id=None)]
        self.evidence = dict(ok=True, version='7.11.0', username='user', abs_username='reader', abs_enabled=True, bookorbit_enabled=False)
    def inventory(self): return copy.deepcopy(self.pairs)
    def probe(self): return dict(self.evidence)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        item, _, _ = fixture(); item['libraryId'] = 'library'
        self.abs = FakeABS(item); self.abs._request = lambda *args: {'username': 'reader'}
        self.bb, self.jf = BB(), FakeJellyfin()
        self.clock = 1000
        self.config = dict(mapping_provider='bookbridge', abs_url='http://abs', abs_version='2.37.1',
                           auto_discovery={'abs_library_id':'library'},
                           bookbridge={'url':'http://bb', 'username':'user', 'version':'7.11.0', 'expected_abs_username':'reader'},
                           folder_mappings=[dict(id='audio',kind='audiobooks',paths={'worker':'/audiobooks','server':'/srv/example-media/audiobooks','abs':'/audiobooks','jf':'/data/audiobooks'})])
        self.gate=dict(bookbridge_ok=True,checked_at=self.clock,folder_gate_ok=True,folder_mounts={
            role:[dict(source='/srv/example-media/audiobooks',destination=dest,rw=role=='abs')]
            for role,dest in [('worker','/audiobooks'),('abs','/audiobooks'),('jf','/data/audiobooks')]})
        self.root.joinpath('deployment.json').write_text(json.dumps(self.gate))
        with patch('sync.ABSClient', return_value=self.abs), patch('bookbridge_client.BookBridgeClient', return_value=self.bb), patch('sync.BOClient', side_effect=AssertionError('BO used')):
            self.b = sync.Bridge(self.root, self.config, {'abs_token':'dummy','bookbridge_password':'dummy'}, clock=lambda:self.clock)
        self.addCleanup(self.b.db.close)
        self.b.jellyfin=self.jf
        self.b.config['jellyfin']={'audio_library_id':'jfaudio','enabled':True}
    def cycle(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()): return self.b.cycle(apply=True,settle=0,**kw)
    def okay(self,result): self.assertFalse(result['blocked'],result)
    def test_no_bookorbit_and_readonly_preview(self):
        self.abs.set(20)
        r=provider_preview(self.b)
        self.assertEqual(r['mapped'],1); self.assertEqual(r['matched_jellyfin'],1)
        self.assertFalse(r['jellyfin_ebook_supported']); self.assertIsNone(self.b.bo)
        self.assertFalse(self.abs.writes or self.jf.writes)
        self.assertEqual(self.b.db.execute('SELECT COUNT(*) FROM state').fetchone()[0],0)
    def test_bidirectional_position_finish_and_reopen(self):
        self.abs.set(25); self.okay(self.cycle())
        self.assertEqual(self.jf.data['PlaybackPositionTicks'],250000000)
        self.jf.data['PlaybackPositionTicks']=450000000; self.okay(self.cycle())
        self.assertEqual(self.abs.raw['currentTime'],45)
        self.jf.data.update(PlaybackPositionTicks=1000000000,Played=True,LastPlayedDate='2026-10-30T12:00:00Z')
        self.okay(self.cycle()); self.assertTrue(self.abs.raw['isFinished'])
        self.jf.data.update(PlaybackPositionTicks=100000000,Played=False)
        self.okay(self.cycle()); self.assertFalse(self.abs.raw['isFinished']); self.assertEqual(self.abs.raw['currentTime'],10)
        self.abs.set(20,True,STAMP); self.okay(self.cycle()); self.assertTrue(self.jf.data['Played'])
        self.abs.set(5); self.okay(self.cycle()); self.assertFalse(self.jf.data['Played'])
    def test_unchanged_no_writes_and_original_baseline_preserved(self):
        self.b.save('abs-book',{'baseline':{'original':'unchanged'}})
        self.abs.set(25); self.okay(self.cycle())
        count=(len(self.abs.writes),len(self.jf.writes)); self.okay(self.cycle())
        self.assertEqual(count,(len(self.abs.writes),len(self.jf.writes)))
        self.assertEqual(self.b.load('abs-book'),{'baseline':{'original':'unchanged'}})
    def test_conflicts_and_active_player_do_not_write(self):
        self.abs.set(20); self.jf.active={'a'*32}
        self.assertEqual(self.cycle()['waiting'],1); self.assertFalse(self.jf.writes)
        self.jf.active.clear(); self.okay(self.cycle())
        self.abs.set(25); self.jf.data['PlaybackPositionTicks']=300000000
        count=(len(self.abs.writes),len(self.jf.writes))
        self.assertTrue(self.cycle()['blocked']); self.assertEqual(count,(len(self.abs.writes),len(self.jf.writes)))
    def test_uncertain_write_not_retried(self):
        self.abs.set(20); self.jf.fail=True; self.assertTrue(self.cycle()['blocked'])
        count=len(self.jf.writes); self.jf.fail=False
        self.assertTrue(self.cycle()['blocked']); self.assertEqual(count,len(self.jf.writes))
    def test_native_bookbridge_change_mid_transfer_is_detected(self):
        self.abs.set(20); self.jf.after_write=lambda:self.abs.set(60)
        self.assertTrue(self.cycle()['blocked'])
        count=len(self.jf.writes); self.jf.after_write=None
        self.assertTrue(self.cycle()['blocked']); self.assertEqual(count,len(self.jf.writes))
    def test_concurrent_abs_edit_after_position_stops_before_finish(self):
        self.abs.set(20); self.okay(self.cycle())
        self.jf.data.update(PlaybackPositionTicks=900000000, Played=True, LastPlayedDate='2026-10-30T12:00:00Z')
        self.abs.after_write=lambda label:self.abs.set(60) if label=='position' else None
        self.assertTrue(self.cycle()['blocked'])
        self.assertFalse(self.abs.raw['isFinished'])
        self.assertEqual(self.abs.raw['currentTime'],60)
        self.assertEqual(self.abs.writes[-1][0],'position')
    def test_uncertain_write_already_reached_goal_is_reconciled(self):
        self.abs.set(20)
        update=self.jf.update
        def arrived(ident,values):
            update(ident,values)
            raise RuntimeError('Lost response')
        self.jf.update=arrived
        self.assertTrue(self.cycle()['blocked'])
        count=len(self.jf.writes); self.jf.update=update
        self.okay(self.cycle()); self.assertEqual(count,len(self.jf.writes))
        self.assertNotIn('pending',self.b.load('_bookbridge_audio:abs-book'))
    def test_pair_reassignment_blocks_existing_baseline(self):
        self.abs.set(20); self.okay(self.cycle())
        self.bb.pairs[0]['ebook_path']='/ebooks/Other.epub'; self.abs.set(30)
        count=len(self.jf.writes); self.assertTrue(self.cycle()['blocked']); self.assertEqual(count,len(self.jf.writes))
    def test_additional_new_pair_automatically_discovered(self):
        self.bb.pairs=[]; self.assertEqual(self.cycle()['mapped'],0)
        self.bb.pairs=BB().pairs; self.abs.set(20)
        self.assertEqual(self.cycle()['mapped'],1); self.assertEqual(self.jf.data['PlaybackPositionTicks'],200000000)
    def test_missing_jellyfin_index_waits(self):
        self.jf.items=lambda **kw: []
        result=self.cycle(); self.assertEqual(result['waiting'],1); self.assertFalse(result['blocked'])
    def test_unsupported_bookorbit_pairs_do_not_write(self):
        self.bb.pairs[0].update(audio_source='BookOrbit',abs_id=None)
        result=self.cycle(); self.assertEqual(result['mapped'],0); self.assertEqual(len(result['bookbridge']['unsupported']),1)
        self.assertFalse(self.abs.writes or self.jf.writes)
    def test_abs_library_and_same_file_guard(self):
        self.abs.ai['libraryId']='other'
        with self.assertRaises(ValueError): provider_preview(self.b)
        self.abs.ai['libraryId']='library'; self.abs.ai['media']['tracks'][0]['metadata']['size']=2000
        with self.assertRaises(ValueError): provider_preview(self.b)
    def test_abs_account_or_bb_writer_mismatch_blocks(self):
        for update in ({'abs_username':'other'},{'abs_enabled':False},{'bookorbit_enabled':True}):
            self.bb.evidence=BB().evidence; self.bb.evidence.update(update)
            with self.assertRaises(RuntimeError): self.b.guard()
    def test_provider_requires_separate_deployment_proof(self):
        self.root.joinpath('deployment.json').write_text(json.dumps(dict(ok=True, checked_at=self.clock)))
        with self.assertRaises(RuntimeError): self.b.guard()
    def test_original_graph_interrupted_write_blocks_switch(self):
        self.b.save('_jellyfin_audio:abs-book',{'pending':{'goal':{}}})
        with self.assertRaises(RuntimeError): self.b.guard()
    def test_bookorbit_mode_rejects_active_bookbridge_writers(self):
        self.b.provider='bookorbit'
        self.root.joinpath('deployment.json').write_text(json.dumps(dict(self.gate,ok=True)))
        with self.assertRaises(RuntimeError): self.b.guard()
        self.bb.evidence.update(abs_enabled=False,bookorbit_enabled=False)
        self.b.guard()
        self.b.save('_bookbridge_audio:abs-book',{'pending':{'goal':{}}})
        with self.assertRaises(RuntimeError): self.b.guard()
    def test_removed_mapping_with_uncertain_transfer_is_not_hidden(self):
        self.abs.set(20); self.jf.fail=True; self.assertTrue(self.cycle()['blocked'])
        self.bb.pairs=[]
        with self.assertRaises(RuntimeError): self.cycle()
    def test_dry_run_proposes_without_writes_or_new_baselines(self):
        self.abs.set(20)
        with contextlib.redirect_stdout(io.StringIO()): result=self.b.cycle(apply=False,settle=0)
        self.assertEqual(len(result['proposed']),1)
        self.assertFalse(self.abs.writes or self.jf.writes)
        self.assertEqual(self.b.db.execute('SELECT COUNT(*) FROM state').fetchone()[0],0)
    def test_alias_for_different_host_folder_is_blocked(self):
        self.config['folder_mappings'][0]['paths']['jf']='/data/audiobooks/Other'
        with self.assertRaises(RuntimeError): self.b.guard()
    def test_provider_works_without_bookorbit_mount_receipt(self):
        self.assertNotIn('bo',self.gate['folder_mounts'])
        self.b.guard()
    def test_configured_audio_alias_used(self):
        self.config['folder_mappings'][0]['paths']['jf']='/media/books'
        mapping=build_audio_mapping(self.bb.pairs[0],self.abs.ai,self.config)
        self.assertEqual(mapping['files'][0]['jf_path'],'/media/books/Example/01.mp3')

if __name__=='__main__': unittest.main()
