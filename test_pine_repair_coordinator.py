import copy
import unittest
from unittest.mock import patch

from pinepods_client import EpisodeUnavailable
from podcast_sync import preview
import test_podcast_sync as fixtures
from test_podcast_sync import TICKS


class RepairCoordinatorTests(unittest.TestCase):
    setUp = fixtures.Tests.setUp
    write_gate = fixtures.Tests.write_gate
    load = fixtures.Tests.load
    save = fixtures.Tests.save
    write_record = fixtures.Tests.write_record
    cycle = fixtures.Tests.cycle
    add_pine = fixtures.Tests.add_pine
    optional_pine = fixtures.Tests.optional_pine

    def test_preview_never_starts_media_probe_or_repairs(self):
        pine = self.optional_pine()
        with patch.object(pine, 'read_episode', side_effect=EpisodeUnavailable('duration_mismatch')), \
             patch('pine_metadata_repair.PineMetadataRepair.repair_if_needed') as repair:
            report = preview(self.b)
        repair.assert_not_called()
        self.assertTrue(report['ok'])
        self.assertEqual(report['pine_waiting'], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM state').fetchone()[0], 0)

    def test_verified_repair_reenters_normal_strict_sync(self):
        pine = self.optional_pine()
        original = pine.read_episode
        attempts = []
        def reading(*args):
            attempts.append(1)
            if len(attempts) == 1:
                raise EpisodeUnavailable('duration_mismatch')
            return original(*args)
        with patch.object(pine, 'read_episode', side_effect=reading), \
             patch('pine_metadata_repair.PineMetadataRepair.repair_if_needed',
                   return_value={'outcome': 'corrected', 'reason': 'measured_duration_repaired'}) as repair:
            report = self.cycle()
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['pine_enrolled'], 1)
        self.assertEqual(repair.call_count, 1)
        self.assertGreaterEqual(len(attempts), 2)
        self.assertFalse(self.abs.writes or self.jf.writes or pine.writes)

    def test_postrepair_media_race_retains_optional_core_sync(self):
        pine = self.optional_pine()
        self.abs.set(40)
        with patch.object(pine, 'read_episode', side_effect=[EpisodeUnavailable('duration_mismatch'),
                                                           EpisodeUnavailable('episode_missing')]), \
             patch('pine_metadata_repair.PineMetadataRepair.repair_if_needed',
                   return_value={'outcome': 'corrected', 'reason': 'measured_duration_repaired'}):
            report = self.cycle()
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['episodes'][0]['pine_reason_code'], 'episode_missing')
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'], 40*TICKS)
        self.assertFalse(pine.writes)

    def test_uncertain_metadata_intent_cannot_enroll_even_if_duration_matches(self):
        pine = self.optional_pine()
        self.save('_pine_metadata:_podcast:show:episode', {'stage': 'intent'})
        with patch('pine_metadata_repair.PineMetadataRepair.repair_if_needed',
                   return_value={'outcome': 'waiting', 'reason': 'interrupted_repair_requires_review'}), \
             patch.object(pine, 'read_episode', side_effect=AssertionError('Unverified participant read')):
            report = self.cycle()
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['episodes'][0]['pine_reason_code'], 'metadata_repair_pending')
        self.assertEqual(report['episodes'][0]['participants'], ['abs', 'jf'])

    def test_enrolled_pine_cannot_be_dropped_when_repair_waits(self):
        pine = self.optional_pine()
        self.assertTrue(self.cycle()['ok'])
        baseline = copy.deepcopy(self.load('_podcast:show:episode'))
        with patch.object(pine, 'read_episode', side_effect=EpisodeUnavailable('duration_mismatch')), \
             patch('pine_metadata_repair.PineMetadataRepair.repair_if_needed',
                   return_value={'outcome': 'waiting', 'reason': 'measured_audio_duration_differs'}):
            report = self.cycle()
        self.assertFalse(report['ok'])
        self.assertEqual(report['pine_blocked'], 1)
        self.assertEqual(self.load('_podcast:show:episode')['baseline'], baseline['baseline'])
        self.assertFalse(self.abs.writes or self.jf.writes or pine.writes)

    def test_orphaned_metadata_intent_is_reported_as_blocked(self):
        self.optional_pine()
        self.write_record('metadata-1', '_pine_metadata:_podcast:gone:episode', {'stage': 'intent'})
        report = self.cycle()
        self.assertFalse(report['ok'])
        self.assertEqual(report['pine_metadata_orphaned'], ['_podcast:gone:episode'])
        self.assertIn('metadata correction', report['blocked'][0]['reason'])


if __name__ == '__main__':
    unittest.main()
