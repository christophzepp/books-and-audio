"""Engine integration for discovery persistence and safe progress hand-off.

The discovery algorithm is substituted at its validated-result boundary; SQLite,
the transaction engine, mapping construction, and progress decisions are real.
No network calls or production files are used.
"""
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


def scan(*, mappings=(), candidates=None, issues=(), ok=True):
    return {
        "scan_ok": ok,
        "new_mappings": copy.deepcopy(list(mappings)),
        "candidates": copy.deepcopy(candidates or {}),
        "issues": copy.deepcopy(list(issues)),
        "summary": {"scan_ok": ok, "new_mappings": len(mappings)},
    }


class AutoDiscoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        ai, bd, bm = fixture()
        self.mapping = build_mapping(ai, bd, bm)
        self.mapping["bo_library_id"] = 1
        self.abs, self.bo = FakeABS(ai), FakeBO(bd, bm)
        self.clock = 1000
        self.config = {
            "abs_url": "http://abs", "bo_url": "http://bo", "abs_version": "2.37.1",
            "mappings": [],
            "auto_discovery": {
                "enabled": True, "abs_library_id": "abs-library", "bo_library_ids": [1],
                "interval_seconds": 300, "stable_seconds": 300,
            },
        }
        self.credentials = {"abs_token": "dummy", "bo_username": "dummy", "bo_password": "dummy"}
        self.refresh_gate()
        self.bridge = self.open_bridge()
        self.discover_patch = patch("sync.discover", return_value=scan())
        self.discover = self.discover_patch.start()
        self.addCleanup(self.discover_patch.stop)

    def open_bridge(self):
        with patch("sync.ABSClient", return_value=self.abs), patch("sync.BOClient", return_value=self.bo):
            bridge = sync.Bridge(self.root, self.config, self.credentials, clock=lambda: self.clock)
        self.addCleanup(bridge.db.close)
        return bridge

    def refresh_gate(self):
        (self.root / "deployment.json").write_text(json.dumps({"ok": True, "checked_at": self.clock}))

    def advance(self, seconds):
        self.clock += seconds
        self.refresh_gate()

    def refresh(self, apply=True):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.bridge.refresh_mappings(apply)

    def cycle(self, apply=True, settle=0):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.bridge.cycle(apply=apply, settle=settle)

    def snapshot_db(self):
        names = [r[0] for r in self.bridge.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
        return {name: self.bridge.db.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall()
                for name in names}

    def auto_rows(self):
        return self.bridge.db.execute("SELECT id,bo_id,value FROM auto_mappings ORDER BY id").fetchall()

    def test_disabled_discovery_never_scans(self):
        self.config["auto_discovery"]["enabled"] = False
        self.config["mappings"] = [self.mapping]
        mappings, _ = self.refresh()
        self.assertEqual(mappings, [self.mapping])
        self.discover.assert_not_called()
        self.assertFalse(self.auto_rows())

    def test_candidates_persist_and_interval_prevents_extra_scan(self):
        candidates = {"new-book": {"fingerprint": self.mapping["fingerprint"], "since": self.clock}}
        self.discover.return_value = scan(candidates=candidates)
        mappings, _ = self.refresh()
        self.assertFalse(mappings)
        self.assertEqual(self.bridge.load("_discovery")["candidates"], candidates)
        self.advance(299)
        self.refresh()
        self.assertEqual(self.discover.call_count, 1)
        self.assertFalse(self.auto_rows())

    def test_promotion_persists_once_after_second_scan(self):
        candidates = {"new-book": {"fingerprint": self.mapping["fingerprint"], "since": self.clock}}
        self.discover.side_effect = [scan(candidates=candidates), scan(mappings=[self.mapping]), scan()]
        self.refresh()
        self.advance(300)
        mappings, _ = self.refresh()
        self.assertEqual(mappings, [self.mapping])
        self.assertEqual(len(self.auto_rows()), 1)
        self.assertEqual(self.bridge.load("_discovery")["candidates"], {})
        self.advance(300)
        again, _ = self.refresh()
        self.assertEqual(again, [self.mapping])
        self.assertEqual(len(self.auto_rows()), 1)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_candidates_and_promoted_mapping_survive_restart(self):
        candidates = {"new-book": {"fingerprint": self.mapping["fingerprint"], "since": self.clock}}
        self.discover.return_value = scan(candidates=candidates)
        self.refresh()
        self.bridge.db.close()
        self.bridge = self.open_bridge()
        self.assertEqual(self.bridge.load("_discovery")["candidates"], candidates)
        self.advance(300)
        self.discover.return_value = scan(mappings=[self.mapping])
        self.refresh()
        self.bridge.db.close()
        self.bridge = self.open_bridge()
        self.assertEqual(self.bridge.list_mappings(), [self.mapping])
        self.assertEqual(len(self.auto_rows()), 1)
        calls = self.discover.call_count
        self.refresh()
        self.assertEqual(self.discover.call_count, calls)

    def test_dry_run_proposes_mapping_without_any_database_change(self):
        self.discover.return_value = scan(mappings=[self.mapping], candidates={"next": {"since": self.clock}})
        before = self.snapshot_db()
        mappings, _ = self.refresh(apply=False)
        self.assertEqual(mappings, [self.mapping])
        self.assertEqual(self.snapshot_db(), before)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_dry_run_cycle_does_not_write_progress_or_deliver_notifications(self):
        self.abs.set(25)
        self.bridge.alert("old-notification", "test pending notification")
        self.discover.return_value = scan(mappings=[self.mapping])
        before = self.snapshot_db()
        with patch.object(self.bridge, "deliver_notifications") as deliver:
            result = self.cycle(apply=False)
        self.assertEqual(len(result["proposed"]), 1)
        self.assertEqual(self.snapshot_db(), before)
        self.assertFalse(self.abs.writes or self.bo.writes)
        deliver.assert_not_called()

    def test_new_untouched_book_creates_only_local_baseline(self):
        self.discover.return_value = scan(mappings=[self.mapping])
        result = self.cycle()
        self.assertEqual(result["unchanged"], 1, result)
        self.assertEqual(len(self.auto_rows()), 1)
        self.assertIn("baseline", self.bridge.load(self.mapping["abs_id"]))
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertIsNone(self.abs.raw)
        self.assertIsNone(self.bo.raw)

    def test_new_abs_progress_passes_normal_settle_then_syncs_once(self):
        self.abs.set(25)
        self.discover.return_value = scan(mappings=[self.mapping])
        first = self.cycle(settle=30)
        self.assertEqual(first["waiting"], 1, first)
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.advance(30)
        second = self.cycle(settle=30)
        self.assertEqual(second["synced"], 1, second)
        self.assertEqual(self.bo.raw["positionMs"], 25000)
        self.assertEqual(self.bo.bd["readStatus"]["status"], "reading")
        writes = copy.deepcopy(self.bo.writes)
        self.bridge.db.close()
        self.bridge = self.open_bridge()
        self.assertEqual(self.cycle()["unchanged"], 1)
        self.assertEqual(self.bo.writes, writes)

    def test_new_bo_progress_passes_normal_settle_then_syncs(self):
        self.bo.set(25)
        self.discover.return_value = scan(mappings=[self.mapping])
        self.assertEqual(self.cycle(settle=30)["waiting"], 1)
        self.assertFalse(self.abs.writes)
        self.advance(30)
        result = self.cycle(settle=30)
        self.assertEqual(result["synced"], 1, result)
        self.assertEqual(self.abs.raw["currentTime"], 25)
        self.assertFalse(self.abs.raw["isFinished"])

    def test_conflicting_progress_on_new_pair_is_not_overwritten(self):
        self.abs.set(25)
        self.bo.set(42)
        self.discover.return_value = scan(mappings=[self.mapping])
        result = self.cycle()
        self.assertEqual(len(result["blocked"]), 1, result)
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertNotIn("baseline", self.bridge.load(self.mapping["abs_id"]))
        self.assertEqual(self.bridge.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)

    def test_refresh_preserves_old_baseline_candidate_and_pending_operation(self):
        old = copy.deepcopy(self.mapping)
        old["abs_id"], old["bo_id"] = "old-book", 88
        self.config["mappings"] = [old]
        state = {"baseline": {"abs": {"position": 10}, "bo": {"position": 10}},
                 "candidate": {"signature": "existing", "since": 990}}
        self.bridge.save("old-book", state)
        self.bridge.write_record("old-operation", "old-book", {"stage": "sending_position"})
        before_op = self.bridge.db.execute("SELECT * FROM operations").fetchall()
        self.discover.return_value = scan(mappings=[self.mapping])
        mappings, _ = self.refresh()
        self.assertEqual({m["abs_id"] for m in mappings}, {"old-book", self.mapping["abs_id"]})
        self.assertEqual(self.bridge.load("old-book"), state)
        self.assertEqual(self.bridge.db.execute("SELECT * FROM operations").fetchall(), before_op)

    def test_no_discovery_result_removes_an_existing_mapping(self):
        self.discover.return_value = scan(mappings=[self.mapping])
        self.refresh()
        self.advance(300)
        self.discover.return_value = scan()
        mappings, _ = self.refresh()
        self.assertEqual(mappings, [self.mapping])
        self.assertEqual(len(self.auto_rows()), 1)

    def test_duplicate_bookorbit_id_cannot_partially_commit_batch(self):
        other = copy.deepcopy(self.mapping)
        other["abs_id"] = "different-abs-id"
        self.discover.return_value = scan(mappings=[self.mapping, other])
        before = self.snapshot_db()
        try:
            self.refresh()
        except Exception:
            pass  # Either a returned refusal or an exception is fail-closed.
        self.assertFalse(self.auto_rows())
        self.assertEqual(self.snapshot_db(), before)


if __name__ == "__main__":
    unittest.main()
