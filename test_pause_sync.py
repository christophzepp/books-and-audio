"""Intentional per-book pauses with real SQLite and transaction code."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sync
from mapping import build_mapping
from test_sync import FakeABS, FakeBO, fixture


class PauseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        ai, bd, bm = fixture()
        self.mapping = build_mapping(ai, bd, bm)
        self.id = self.mapping['abs_id']
        self.abs, self.bo = FakeABS(ai), FakeBO(bd, bm)
        self.config = {'abs_url': 'http://abs', 'bo_url': 'http://bo',
                       'abs_version': '2.37.1', 'mappings': [self.mapping]}
        (self.root / 'deployment.json').write_text(json.dumps({'ok': True, 'checked_at': 1000}))
        self.bridge = self.open_bridge()

    def open_bridge(self):
        with patch('sync.ABSClient', return_value=self.abs), patch('sync.BOClient', return_value=self.bo):
            bridge = sync.Bridge(self.root, self.config,
                                 {'abs_token': 'dummy', 'bo_username': 'dummy', 'bo_password': 'dummy'},
                                 clock=lambda: 1000)
        self.addCleanup(bridge.db.close)
        return bridge

    def pause(self):
        self.config['paused_mappings'] = {self.id: 'EPUB-Zuordnung wird vorbereitet'}

    def cycle(self, apply=True):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.bridge.cycle(apply=apply, settle=0)

    def snapshot(self):
        return {name: self.bridge.db.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall()
                for (name,) in self.bridge.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}

    def test_paused_pair_has_no_book_reads_or_writes(self):
        self.pause()
        self.abs.set(25)
        with patch.object(self.abs, 'item') as ai, patch.object(self.abs, 'progress') as ap, \
                patch.object(self.bo, 'detail') as bd, patch.object(self.bo, 'manifest') as bm, \
                patch.object(self.bo, 'playback') as bp, patch.object(self.bo, 'library') as bl:
            result = self.cycle()
        for call in (ai, ap, bd, bm, bp, bl):
            call.assert_not_called()
        self.assertEqual(result['mapped'], 1)
        self.assertEqual(result['synced'], 0)
        self.assertEqual(result['unchanged'], 0)
        self.assertEqual(result['blocked'], [])
        self.assertEqual(result['proposed'], [])
        self.assertEqual(result['paused'], [{'abs_id': self.id, 'bo_id': 23, 'title': 'Example',
                                            'reason': self.config['paused_mappings'][self.id],
                                            'pending_operations': 0, 'requires_review': False}])
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_baseline_candidate_error_and_pending_are_preserved_verbatim(self):
        self.abs.set(25)
        self.bo.set(25)
        pair, raw = self.bridge.read_pair(self.mapping)
        state = {'baseline': pair, 'candidate': {'signature': 'retained', 'since': 900},
                 'last_error': 'historical issue'}
        self.bridge.save(self.id, state)
        # Even an already reached pending goal must not be reconciled in pause.
        self.bridge.write_record('operation', self.id, {'goal': pair['abs'], 'before': raw,
                                                       'stage': 'sending_status'})
        before = self.snapshot()
        self.pause()
        result = self.cycle()
        after = self.snapshot()
        for table in ('operations', 'mapping_revision_overrides', 'events', 'notifications'):
            self.assertEqual(after[table], before[table])
        self.assertEqual(self.bridge.load(self.id), state)
        self.assertEqual(result['paused'][0]['pending_operations'], 1)
        self.assertTrue(result['paused'][0]['requires_review'])
        self.assertEqual(result['blocked'], [])

    def test_paused_dry_run_makes_no_database_change(self):
        self.pause()
        before = self.snapshot()
        result = self.cycle(apply=False)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(result['paused']), 1)

    def test_paused_mapping_remains_reserved_and_is_passed_to_discovery(self):
        self.pause()
        self.config['auto_discovery'] = {'enabled': True, 'abs_library_id': 'library',
                                         'bo_library_ids': [1], 'interval_seconds': 300,
                                         'stable_seconds': 300}
        scan = {'scan_ok': True, 'new_mappings': [], 'candidates': {}, 'issues': [], 'summary': {}}
        with patch('sync.discover', return_value=scan) as discover:
            result = self.cycle()
        self.assertEqual(discover.call_args.args[2], [self.mapping])
        self.assertEqual(self.bridge.list_mappings(), [self.mapping])
        self.assertEqual(len(result['paused']), 1)

    def test_auto_discovered_mapping_can_be_paused(self):
        self.config['mappings'] = []
        self.bridge.db.execute('INSERT INTO auto_mappings VALUES (?,?,?)',
                               (self.id, self.mapping['bo_id'], sync.dump(self.mapping)))
        self.bridge.db.commit()
        self.pause()
        result = self.cycle()
        self.assertEqual(len(result['paused']), 1)
        self.assertEqual(self.bridge.list_mappings(), [self.mapping])

    def test_pause_is_retained_across_restart(self):
        self.pause()
        self.bridge.write_record('uncertain', self.id, {'stage': 'sending_position'})
        self.bridge.db.close()
        self.bridge = self.open_bridge()
        result = self.cycle()
        self.assertEqual(result['paused'][0]['pending_operations'], 1)
        self.assertTrue(result['paused'][0]['requires_review'])

    def test_other_book_continues_through_real_sync(self):
        self.pause()
        ai, bd, bm = fixture()
        ai['id'], ai['path'] = 'other-book', '/audiobooks/Other'
        ai['media']['tracks'][0]['metadata']['path'] = '/audiobooks/Other/01.mp3'
        ai['media']['metadata']['title'] = 'Other'
        bd['id'], bd['folderPath'] = 24, '/audiobooks/Other'
        bd['files'][0]['absolutePath'] = '/audiobooks/Other/01.mp3'
        bm['book']['id'] = 24
        other = build_mapping(ai, bd, bm)
        self.config['mappings'].append(other)
        self.abs.ai, self.bo.bd, self.bo.bm = ai, bd, bm
        self.abs.set(25)
        with patch.object(self.abs, 'item', wraps=self.abs.item) as get_item, \
                patch.object(self.bo, 'detail', wraps=self.bo.detail) as get_detail:
            result = self.cycle()
        self.assertEqual(result['synced'], 1, result)
        self.assertEqual(result['blocked'], [])
        self.assertEqual(len(result['paused']), 1)
        self.assertEqual(self.bo.raw['positionMs'], 25000)
        self.assertTrue(all(c.args == ('other-book',) for c in get_item.call_args_list))
        self.assertTrue(all(c.args == (24,) for c in get_detail.call_args_list))
        self.assertEqual(self.bridge.load(self.id), {})

    def test_direct_apply_cannot_bypass_pause_in_either_direction(self):
        self.abs.set(25)
        pair, raw = self.bridge.read_pair(self.mapping)
        self.pause()
        for direction in ('abs_to_bo', 'bo_to_abs'):
            with self.subTest(direction=direction), patch.object(self.bo, 'library') as library:
                with self.assertRaises(sync.MappingPaused):
                    self.bridge.apply(self.mapping, pair, raw, direction, None)
                library.assert_not_called()
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertEqual(self.bridge.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0], 0)

    def test_direct_prepare_cannot_migrate_paused_identity(self):
        self.pause()
        self.bo.bm['revision'] = 'b' * 64
        with patch.object(self.abs, 'item') as item:
            with self.assertRaises(sync.MappingPaused):
                self.bridge.prepare_pair(self.mapping, {}, True)
        item.assert_not_called()
        self.assertEqual(self.bridge.db.execute('SELECT COUNT(*) FROM mapping_revision_overrides').fetchone()[0], 0)

    def test_pause_after_position_stops_status_and_keeps_pending_journal(self):
        self.abs.set(25)
        pair, raw = self.bridge.read_pair(self.mapping)
        self.bo.after_write = lambda label: self.pause()
        with self.assertRaises(sync.MappingPaused):
            self.bridge.apply(self.mapping, pair, raw, 'abs_to_bo', None)
        self.assertEqual(self.bo.writes, [('position', 25000)])
        self.assertEqual(self.bridge.load(self.id), {})
        rows = self.bridge.db.execute("SELECT value FROM operations WHERE status='pending'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][0])['stage'], 'sending_position')
        result = self.cycle()
        self.assertTrue(result['paused'][0]['requires_review'])
        self.assertEqual(self.bo.writes, [('position', 25000)])

    def test_last_gate_after_journal_stops_first_remote_write(self):
        self.abs.set(25)
        pair, raw = self.bridge.read_pair(self.mapping)
        original = self.bridge.write_record

        def pause_at_send(oid, id, record, status='pending'):
            original(oid, id, record, status)
            if record['stage'].startswith('sending_'):
                self.pause()

        with patch.object(self.bridge, 'write_record', side_effect=pause_at_send):
            with self.assertRaises(sync.MappingPaused):
                self.bridge.apply(self.mapping, pair, raw, 'abs_to_bo', None)
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertEqual(self.bridge.db.execute("SELECT COUNT(*) FROM operations WHERE status='pending'").fetchone()[0], 1)

    def test_cycle_reports_mid_operation_pause_without_error_or_state_reset(self):
        self.abs.set(25)
        self.bo.after_write = lambda label: self.pause()
        result = self.cycle()
        self.assertEqual(result['blocked'], [])
        self.assertEqual(result['synced'], 0)
        self.assertTrue(result['paused'][0]['requires_review'])
        self.assertNotIn('last_error', self.bridge.load(self.id))
        self.assertNotIn('baseline', self.bridge.load(self.id))
        self.assertEqual(self.bridge.db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 0)

    def test_unpause_keeps_baseline_and_detects_progress_made_during_pause(self):
        self.abs.set(10)
        self.bo.set(10)
        self.cycle()
        before = copy.deepcopy(self.bridge.load(self.id))
        self.pause()
        self.abs.set(25)
        self.cycle()
        self.assertEqual(self.bridge.load(self.id), before)
        self.config['paused_mappings'] = {}
        result = self.cycle()
        self.assertEqual(result['synced'], 1, result)
        self.assertEqual(result['paused'], [])
        self.assertEqual(self.bo.raw['positionMs'], 25000)

    def test_unpause_does_not_abandon_unresolved_partial_operation(self):
        self.abs.set(25)
        self.bridge.write_record('uncertain', self.id,
                                 {'goal': {'position': 25, 'status': 'in_progress'}, 'stage': 'sending_position'})
        self.pause()
        self.cycle()
        before = self.bridge.db.execute('SELECT * FROM operations').fetchall()
        self.config['paused_mappings'] = {}
        result = self.cycle()
        self.assertEqual(len(result['blocked']), 1)
        self.assertEqual(self.bridge.db.execute('SELECT * FROM operations').fetchall(), before)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_invalid_pause_configuration_fails_before_any_api_read(self):
        invalid = [None, [], '', True, {None: 'pause'}, {1: 'pause'}, {'': 'pause'},
                   {' ': 'pause'}, {self.id + '\n': 'pause'}, {'unknown-book': 'pause'},
                   {self.id: None}, {self.id: True}, {self.id: ''}, {self.id: '   '},
                   {self.id: 'line\nbreak'}, {self.id: 'x' * 501}]
        for value in invalid:
            with self.subTest(value=value):
                self.config['paused_mappings'] = value
                with patch.object(self.abs, 'version') as version, \
                        patch.object(self.bridge, 'prepare_pair') as prepare:
                    with self.assertRaises(ValueError):
                        self.cycle()
                version.assert_not_called()
                prepare.assert_not_called()
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_invalid_or_unknown_pause_also_blocks_direct_apply(self):
        self.abs.set(25)
        pair, raw = self.bridge.read_pair(self.mapping)
        for value in ({'unknown-book': 'pause'}, {self.id: ''}):
            with self.subTest(value=value):
                self.config['paused_mappings'] = value
                with self.assertRaises(ValueError):
                    self.bridge.apply(self.mapping, pair, raw, 'abs_to_bo', None)
        self.assertFalse(self.abs.writes or self.bo.writes)


if __name__ == '__main__':
    unittest.main()
