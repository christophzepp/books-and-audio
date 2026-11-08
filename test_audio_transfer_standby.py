"""Real three-source native audio transactions retain standby-friendly proofs.

Only HTTP clients are fakes. Enrollment, durable SHA witnesses, coordinator
selection, CAS writes, journals, readback and recovery use production code.
"""
from contextlib import ExitStack
import copy
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from jellyfin_audio import AudioCoordinator, TICKS
from media_observation import ObservationStore
import native_discovery
import test_native_discovery as fixture
from test_jellyfin_transactions import FakeJellyfin


class AudioTransferStandbyTests(unittest.TestCase):
    setUp=fixture.NativeEnrollmentTests.setUp
    request=fixture.NativeEnrollmentTests.request
    cycle=fixture.NativeEnrollmentTests.cycle
    accept=fixture.NativeEnrollmentTests.accept
    count=fixture.NativeEnrollmentTests.count

    def warm_audio(self):
        self.accept();self.assertEqual(self.cycle()['unchanged'],1)
        self.jf=FakeJellyfin();self.bridge.jellyfin=self.jf
        self.config['jellyfin']={'audio_library_id':'lib'}
        self.jf.data.update(PlaybackPositionTicks=10*TICKS,PlayCount=1)
        result=self.cycle();self.assertFalse(result['blocked'],result)
        self.assertEqual(result['unchanged'],1)
        self.assertFalse(self.abs.writes or self.bo.writes or self.jf.writes)
        self.hash_reads.clear();self.logical=Mock(return_value='unchanged-media-mount')
        self.bridge.media_observation_guard=self.logical
        self.bridge.media_guard=Mock(side_effect=AssertionError('physical folder verification'))
        self.key='_jellyfin_audio:'+self.mapping['abs_id']
        self.original_approvals=self.approvals()

    def approvals(self):
        return {name:self.bridge.db.execute('SELECT * FROM '+name+' ORDER BY id').fetchall()
                for name in ('native_readalong_enrollments','native_readalong_witnesses')}

    def no_media(self):
        stack=ExitStack()
        stack.enter_context(patch.object(self.bo,'file_sha256',side_effect=AssertionError('EPUB download/hash')))
        for method in ('resolve','stat','glob','rglob'):
            stack.enter_context(patch.object(Path,method,side_effect=AssertionError('physical media path inspection')))
        return stack

    def assert_standby(self):
        self.assertEqual(self.hash_reads,[])
        self.bridge.media_guard.assert_not_called()
        self.assertGreaterEqual(self.logical.call_count,1)
        self.assertTrue(all(call.args==('audiobooks',) for call in self.logical.call_args_list))
        self.assertEqual(self.approvals(),self.original_approvals)

    def test_native_abs_progress_reaches_bookorbit_and_jellyfin_without_reopening_media(self):
        self.warm_audio();self.abs.set(25)
        with self.no_media():result=self.cycle()
        self.assertFalse(result['blocked'],result);self.assertEqual(result['synced'],1)
        self.assertEqual(self.bo.raw['positionMs'],25000)
        self.assertEqual(self.jf.data['PlaybackPositionTicks'],25*TICKS)
        self.assertTrue(self.bo.writes and self.jf.writes)
        stored=self.bridge.load(self.key)
        self.assertNotIn('pending',stored)
        self.assertTrue(all(row['position']==25 for row in stored['baseline'].values()))
        self.assert_standby()

    def test_jellyfin_progress_reaches_native_abs_and_bookorbit_without_media_reads(self):
        self.warm_audio();self.jf.data.update(PlaybackPositionTicks=45*TICKS)
        with self.no_media():result=self.cycle()
        self.assertFalse(result['blocked'],result);self.assertEqual(result['synced'],1)
        self.assertEqual(self.abs.raw['currentTime'],45)
        self.assertEqual(self.bo.raw['positionMs'],45000)
        self.assertEqual(self.jf.data['PlaybackPositionTicks'],45*TICKS)
        self.assertTrue(self.abs.writes and self.bo.writes)
        self.assert_standby()

    def test_changed_native_version_before_audio_write_stops_without_download_fallback(self):
        self.warm_audio();self.abs.set(25)
        pair,raw=self.bridge.read_pair(self.mapping)
        self.bo.bd['files'][1]['contentVersion']='2026-10-31T02:00:00.000Z'
        with self.no_media():
            with self.assertRaises(native_discovery.NativeMediaVerificationRequired):
                AudioCoordinator(self.bridge,self.jf).handle(self.mapping,pair,raw,True,0)
        self.assertFalse(self.abs.writes or self.bo.writes or self.jf.writes)
        self.assertNotIn('pending',self.bridge.load(self.key))
        self.assert_standby()

    def interrupt_final_commit(self):
        self.abs.set(25);original=self.bridge.write_record
        def crash(oid,key,record,status='pending'):
            if key==self.key and status=='complete':raise RuntimeError('crash after checked Jellyfin write')
            return original(oid,key,record,status)
        with self.no_media(),patch.object(self.bridge,'write_record',side_effect=crash):result=self.cycle()
        self.assertEqual(len(result['blocked']),1)
        self.assertIn('pending',self.bridge.load(self.key))
        self.assertEqual(self.jf.data['PlaybackPositionTicks'],25*TICKS)
        return copy.deepcopy((self.abs.writes,self.bo.writes,self.jf.writes))

    def test_completed_jellyfin_journal_recovers_from_durable_witness_without_replay(self):
        self.warm_audio();writes=self.interrupt_final_commit()
        self.bo._native_full_hash_cache={}
        self.logical.reset_mock()
        with self.no_media():result=self.cycle()
        self.assertFalse(result['blocked'],result);self.assertEqual(result['unchanged'],1)
        self.assertNotIn('pending',self.bridge.load(self.key))
        self.assertEqual((self.abs.writes,self.bo.writes,self.jf.writes),writes)
        self.assertGreaterEqual(self.bridge._io_metrics.get('native_observation_durable_hits',0),1)
        self.assert_standby()

    def test_missing_witness_during_completed_journal_recovery_keeps_journal_and_never_hashes(self):
        self.warm_audio();writes=self.interrupt_final_commit()
        self.bo._native_full_hash_cache={}
        ObservationStore(self.root).forget('native-readalong',self.mapping['abs_id'])
        before=copy.deepcopy(self.bridge.load(self.key))
        with self.no_media():result=self.cycle()
        self.assertEqual(len(result['blocked']),1,result)
        self.assertIn('media verification required',result['blocked'][0]['reason'])
        self.assertEqual(result['synced'],0)
        self.assertEqual(self.bridge.load(self.key),before)
        self.assertEqual((self.abs.writes,self.bo.writes,self.jf.writes),writes)
        self.assert_standby()


if __name__=='__main__':unittest.main()
