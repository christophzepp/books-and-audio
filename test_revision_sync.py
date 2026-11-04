"""Real-SQLite revision migration tests; all application APIs are local fakes."""
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


class RevisionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        ai, bd, bm = fixture()
        self.seed = build_mapping(ai, bd, bm)
        self.abs, self.bo = FakeABS(ai), FakeBO(bd, bm)
        self.clock = 1000
        self.config = {"abs_url": "http://abs", "bo_url": "http://bo", "abs_version": "2.37.1",
                       "mappings": [self.seed]}
        self.credentials = {"abs_token": "dummy", "bo_username": "dummy", "bo_password": "dummy"}
        self.gate()
        self.bridge = self.open_bridge()

    def gate(self):
        (self.root / "deployment.json").write_text(json.dumps({"ok": True, "checked_at": self.clock}))

    def open_bridge(self):
        with patch("sync.ABSClient", return_value=self.abs), patch("sync.BOClient", return_value=self.bo):
            bridge = sync.Bridge(self.root, self.config, self.credentials, clock=lambda: self.clock)
        self.addCleanup(bridge.db.close)
        return bridge

    def baseline(self, position=25):
        self.abs.set(position)
        self.bo.set(position)
        pair, _ = self.bridge.read_pair(self.seed)
        self.bridge.save(self.seed["abs_id"], {"baseline": pair})
        return copy.deepcopy(pair)

    def change_revision(self, letter="b"):
        self.bo.bm["revision"] = letter * 64

    def cycle(self, apply=True, settle=0):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.bridge.cycle(apply=apply, settle=settle)

    def prepare(self, apply=True):
        mapping = self.bridge.list_mappings()[0]
        return self.bridge.prepare_pair(mapping, self.bridge.load(mapping["abs_id"]), apply)

    def overrides(self):
        return self.bridge.db.execute("SELECT * FROM mapping_revision_overrides").fetchall()

    def history(self):
        return self.bridge.db.execute("SELECT revision FROM mapping_revision_history ORDER BY revision").fetchall()

    def snapshot(self):
        names = [r[0] for r in self.bridge.db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: self.bridge.db.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall()
                for name in names}

    def test_scan_revision_updates_mapping_without_remote_progress_write(self):
        self.baseline()
        self.change_revision()
        result = self.cycle()
        self.assertEqual(result["revisions_updated"], 1, result)
        self.assertEqual(result["unchanged"], 1, result)
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertEqual(self.bridge.list_mappings()[0]["bo_revision"], "b" * 64)
        self.assertEqual(self.config["mappings"][0]["bo_revision"], "a" * 64)
        self.assertEqual(self.bo.raw["manifestRevision"], "a" * 64)
        self.assertEqual(len(self.history()), 2)

    def test_atomic_migration_changes_only_baseline_fingerprints_and_candidate(self):
        baseline = self.baseline()
        state = {"baseline": baseline, "candidate": {"since": 900, "signature": "old"},
                 "last_error": "previous independent issue"}
        self.bridge.save(self.seed["abs_id"], state)
        self.bo.set(42)
        self.change_revision()
        mapping, migrated, pair, raw, changed = self.prepare()
        self.assertTrue(changed)
        expected = copy.deepcopy(state)
        del expected["candidate"]
        for side in ("abs", "bo"):
            expected["baseline"][side]["fingerprint"] = mapping["fingerprint"]
        self.assertEqual(migrated, expected)
        self.assertEqual(self.bridge.load(self.seed["abs_id"]), expected)
        self.assertEqual(pair["bo"]["position"], 42)
        self.assertEqual(migrated["baseline"]["bo"]["position"], 25)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_untouched_books_stay_without_progress_rows(self):
        self.change_revision()
        result = self.cycle()
        self.assertEqual(result["unchanged"], 1, result)
        self.assertIsNone(self.abs.raw)
        self.assertIsNone(self.bo.raw)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_dry_run_migrates_only_in_memory(self):
        self.baseline()
        self.change_revision()
        before = self.snapshot()
        result = self.cycle(apply=False)
        self.assertEqual(result["revisions_proposed"], 1, result)
        self.assertEqual(result["revisions_updated"], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_restart_preserves_override_and_accepts_known_stale_playback_revision(self):
        self.baseline()
        self.change_revision()
        self.assertEqual(self.cycle()["revisions_updated"], 1)
        before = self.snapshot()
        self.bridge.db.close()
        self.bridge = self.open_bridge()
        result = self.cycle()
        self.assertEqual(result["revisions_updated"], 0, result)
        self.assertEqual(result["unchanged"], 1, result)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_multiple_verified_revisions_accept_original_playback_revision(self):
        self.baseline()
        for letter in ("b", "c", "d"):
            self.change_revision(letter)
            result = self.cycle()
            self.assertEqual(result["revisions_updated"], 1, result)
            self.assertEqual(result["unchanged"], 1, result)
        self.assertEqual(self.bo.raw["manifestRevision"], "a" * 64)
        self.assertEqual(len(self.history()), 4)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_real_progress_write_uses_current_manifest_and_server_cas_revision(self):
        self.baseline()
        self.change_revision()
        self.cycle()
        cas = self.bo.raw["revision"]
        self.abs.set(42)
        with patch.object(self.bo, "write_position", wraps=self.bo.write_position) as write:
            result = self.cycle()
        self.assertEqual(result["synced"], 1, result)
        self.assertEqual(write.call_args.args[3], cas)
        self.assertEqual(write.call_args.args[4], "b" * 64)
        self.assertEqual(self.bo.raw["positionMs"], 42000)

    def test_revision_resets_old_settle_window_before_real_progress_write(self):
        self.baseline()
        self.abs.set(42)
        pair, raw = self.bridge.read_pair(self.seed)
        stored = self.bridge.load(self.seed["abs_id"])
        stored["candidate"] = {"signature": sync.digest(raw), "since": 900}
        self.bridge.save(self.seed["abs_id"], stored)
        self.change_revision()
        first = self.cycle(settle=30)
        self.assertEqual(first["waiting"], 1, first)
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.clock += 30
        self.gate()
        self.assertEqual(self.cycle(settle=30)["synced"], 1)

    def test_pending_operation_prevents_revision_migration_even_if_goal_reached(self):
        pair = self.baseline()
        self.bridge.write_record("pending", self.seed["abs_id"], {"goal": pair["abs"], "stage": "sending_position"})
        self.change_revision()
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "Offene"):
            self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_unknown_playback_revision_blocks_without_mapping_change(self):
        self.baseline()
        self.bo.raw["manifestRevision"] = "f" * 64
        self.change_revision()
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_historical_revision_with_wrong_identity_fingerprint_is_rejected(self):
        self.baseline()
        self.change_revision()
        self.cycle()
        self.bridge.db.execute('INSERT INTO mapping_revision_history VALUES (?,?,?)',
                               (self.seed['abs_id'], 'f' * 64, '0' * 64))
        self.bridge.db.commit()
        self.bo.raw['manifestRevision'] = 'f' * 64
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'anderen Dateizuordnung'):
            self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_historical_revision_requires_matching_asset_and_chapter_evidence_hash(self):
        self.baseline()
        self.change_revision()
        self.cycle()
        current = self.bridge.list_mappings()[0]
        legacy_fp = sync.mapping_fingerprint(dict(current, bo_revision='f' * 64))
        self.bridge.db.execute('INSERT INTO mapping_revision_history VALUES (?,?,?)',
                               (self.seed['abs_id'], 'f' * 64, legacy_fp))
        self.bridge.db.commit()
        self.bo.raw['manifestRevision'] = 'f' * 64
        # An otherwise matching legacy fingerprint alone proves neither BO
        # modification time nor chapter semantics.
        with self.assertRaisesRegex(RuntimeError, 'anderen Dateizuordnung'):
            self.prepare()
        self.bridge.db.execute('INSERT INTO mapping_revision_evidence VALUES (?,?,?)',
                               (self.seed['abs_id'], 'f' * 64, '0' * 64))
        self.bridge.db.commit()
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'anderen Dateizuordnung'):
            self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_same_manifest_revision_with_changed_asset_evidence_is_rejected(self):
        self.baseline()
        self.bo.bm["assets"][0]["etag"] = "e" * 64
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.prepare()
        with self.assertRaises(ValueError):
            self.bridge.read_pair(self.seed)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_actual_file_change_is_never_treated_as_revision_refresh(self):
        self.baseline()
        self.abs.ai["media"]["tracks"][0]["metadata"]["mtimeMs"] += 1
        self.change_revision()
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.snapshot(), before)

    def test_incompatible_baseline_fingerprint_blocks_migration(self):
        pair = self.baseline()
        pair["bo"]["fingerprint"] = "0" * 64
        self.bridge.save(self.seed["abs_id"], {"baseline": pair})
        self.change_revision()
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "Baseline"):
            self.prepare()
        self.assertEqual(self.snapshot(), before)

    def test_revision_override_does_not_rewrite_auto_mapping_seed(self):
        self.config["mappings"] = []
        encoded = sync.dump(self.seed)
        self.bridge.db.execute("INSERT INTO auto_mappings VALUES (?,?,?)", (self.seed["abs_id"], self.seed["bo_id"], encoded))
        self.bridge.db.commit()
        self.baseline()
        self.change_revision()
        self.assertEqual(self.cycle()["revisions_updated"], 1)
        self.assertEqual(self.bridge.db.execute("SELECT value FROM auto_mappings").fetchone()[0], encoded)
        self.assertEqual(self.bridge.list_mappings()[0]["bo_revision"], "b" * 64)

    def test_replacing_seed_after_override_fails_closed(self):
        self.baseline()
        self.change_revision()
        self.cycle()
        self.config["mappings"][0] = copy.deepcopy(self.bridge.list_mappings()[0])
        with self.assertRaisesRegex(RuntimeError, "urspruenglichen"):
            self.bridge.list_mappings()

    def test_journal_failure_rolls_back_override_history_and_baseline(self):
        self.baseline()
        self.change_revision()
        self.bridge.db.execute("""CREATE TRIGGER fail_revision_event BEFORE INSERT ON events
          WHEN NEW.kind='manifest_revision_updated' BEGIN SELECT RAISE(ABORT,'simulated interruption'); END""")
        self.bridge.db.commit()
        before = self.snapshot()
        with self.assertRaises(Exception):
            self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.abs.writes or self.bo.writes)


if __name__ == "__main__":
    unittest.main()
