"""Transaction-level tests: real mapping, normalization, decisions and SQLite.

Only HTTP clients are substituted. Fake mutations reproduce relevant ABS
2.37.1 status side effects, rather than assuming PATCHes are simple assignment.
"""
import copy
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sync
from mapping import build_mapping, canonical_to_bo


STAMP = 1793340000000


def fixture():
    path = "/audiobooks/Example/01.mp3"
    ai = {"id": "abs-book", "path": "/audiobooks/Example", "media": {
        "metadata": {"title": "Example"}, "duration": 100,
        "tracks": [{"metadata": {"path": path, "size": 1000, "mtimeMs": STAMP},
                    "duration": 100, "startOffset": 0}]}}
    bd = {"id": 23, "libraryId": 1, "folderPath": "/audiobooks/Example", "readStatus": None,
          "readAloudSync": {"state": "unavailable"}, "files": [
              {"id": 11, "absolutePath": path, "sizeBytes": 1000,
               "durationSeconds": 100, "format": "mp3"}]}
    bm = {"schema": "bookorbit.audiobook-manifest", "schemaVersion": 2,
          "revision": "a" * 64, "book": {"id": 23}, "totalDurationMs": 100000, "chapters": [],
          "assets": [{"assetId": "aud_1", "fileId": 11, "sequence": 0,
                      "durationMs": 100000, "sizeBytes": 1000,
                      "etag": hashlib.sha256(b"aud_1:1000:1793340000000").hexdigest()}]}
    return ai, bd, bm


class FakeABS:
    def __init__(self, ai):
        self.ai = ai
        self.raw = None
        self.writes = []
        self.after_write = None

    def version(self):
        return "2.37.1"

    def item(self, item_id):
        return copy.deepcopy(self.ai)

    def progress(self, item_id):
        return copy.deepcopy(self.raw)

    def set(self, position, finished=False, finished_at=None):
        self.raw = {"id": "mp", "libraryItemId": "abs-book", "currentTime": position,
                    "duration": 100, "isFinished": finished, "startedAt": STAMP,
                    "lastUpdate": STAMP, "finishedAt": finished_at}

    def write_position(self, item_id, position, duration):
        self.writes.append(("position", position))
        if self.raw is None:
            self.set(position)
        else:
            if self.raw["isFinished"] and self.raw["currentTime"] != position:
                self.raw["isFinished"] = False
                self.raw["finishedAt"] = None
            self.raw["currentTime"] = position
        self.raw["duration"] = duration
        self.raw["lastUpdate"] += 1
        if self.after_write:
            self.after_write("position")

    def write_finished(self, item_id, finished, finished_at=None):
        self.writes.append(("finished", finished, finished_at))
        if self.raw is None:
            self.set(0)
        if self.raw["isFinished"] and not finished:
            self.raw["currentTime"] = 0
            self.raw["finishedAt"] = None
        if finished and not self.raw["isFinished"]:
            self.raw["finishedAt"] = finished_at or STAMP + 999
        self.raw["isFinished"] = finished
        self.raw["lastUpdate"] += 1
        if self.after_write:
            self.after_write("finished")


class FakeBO:
    def __init__(self, bd, bm):
        self.bd, self.bm = bd, bm
        self.raw = None
        self.writes = []
        self.after_write = None

    def detail(self, book_id):
        return copy.deepcopy(self.bd)

    def manifest(self, book_id):
        return copy.deepcopy(self.bm)

    def playback(self, book_id):
        return copy.deepcopy(self.raw)

    def library(self, library_id):
        return {"markAsFinishedPercentComplete": 98}

    def set(self, position, status="reading", finished_at=None):
        self.raw = {"assetId": "aud_1", "positionMs": round(position * 1000),
                    "manifestRevision": self.bm["revision"], "revision": 1}
        self.bd["readStatus"] = {"status": status, "startedAt": "2026-10-30",
                                 "finishedAt": finished_at}

    def write_position(self, book_id, asset, local, revision, manifest, oid, captured):
        self.writes.append(("position", local))
        if (0 if self.raw is None else self.raw["revision"]) != revision:
            raise RuntimeError("revision conflict")
        self.raw = {"assetId": asset, "positionMs": local, "manifestRevision": manifest,
                    "revision": revision + 1}
        if self.bd["readStatus"] is None:
            self.bd["readStatus"] = {"status": "reading", "startedAt": "2026-10-30",
                                     "finishedAt": None}
        if self.after_write:
            self.after_write("position")

    def write_status(self, book_id, status, started_at=None, finished_at=None):
        self.writes.append(("status", status))
        self.bd["readStatus"] = {"status": status, "startedAt": started_at,
                                 "finishedAt": finished_at}
        if self.after_write:
            self.after_write("status")


class SyncIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        ai, bd, bm = fixture()
        self.mapping = build_mapping(ai, bd, bm)
        self.mapping["bo_library_id"] = 1
        self.abs, self.bo = FakeABS(ai), FakeBO(bd, bm)
        self.clock = 1000
        self.root.joinpath("deployment.json").write_text(json.dumps({"ok": True, "checked_at": self.clock}))
        config = {"abs_url": "http://abs", "bo_url": "http://bo", "abs_version": "2.37.1",
                  "mappings": [self.mapping]}
        with patch("sync.ABSClient", return_value=self.abs), patch("sync.BOClient", return_value=self.bo):
            self.bridge = sync.Bridge(self.root, config,
                                      {"abs_token": "dummy", "bo_username": "dummy", "bo_password": "dummy"},
                                      clock=lambda: self.clock)
        self.addCleanup(self.bridge.db.close)

    def cycle(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.bridge.cycle(apply=True, settle=0, **kwargs)

    def apply(self, direction, baseline=None):
        pair, raw = self.bridge.read_pair(self.mapping)
        with contextlib.redirect_stdout(io.StringIO()):
            return self.bridge.apply(self.mapping, pair, raw, direction, baseline)

    def pending(self):
        return self.bridge.db.execute("SELECT value FROM operations WHERE status='pending'").fetchall()

    def baseline(self):
        pair, _ = self.bridge.read_pair(self.mapping)
        self.bridge.save(self.mapping["abs_id"], {"baseline": pair})
        return pair

    def test_untouched_libraries_create_only_baseline(self):
        result = self.cycle()
        self.assertEqual(result["unchanged"], 1)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_initial_abs_zero_position_finished_imports_status(self):
        self.abs.set(0, True, STAMP)
        result = self.cycle()
        self.assertEqual(result["synced"], 1, result)
        self.assertEqual(self.bo.bd["readStatus"]["status"], "read")
        self.assertEqual(self.bo.writes, [("position", 0), ("status", "read")])

    def test_initial_bo_zero_position_finished_imports_position_then_status(self):
        self.bo.set(0, "read", "2026-10-30")
        result = self.cycle()
        self.assertEqual(result["synced"], 1, result)
        self.assertEqual([entry[0] for entry in self.abs.writes], ["position", "finished"])
        self.assertTrue(self.abs.raw["isFinished"])

    def test_finish_before_end_updates_abs_position_then_explicit_status(self):
        self.abs.set(5)
        self.bo.set(5)
        self.baseline()
        self.bo.set(20, "read", "2026-10-30")
        result = self.cycle()
        self.assertEqual(result["synced"], 1, result)
        self.assertEqual(self.abs.raw["currentTime"], 20)
        self.assertTrue(self.abs.raw["isFinished"])
        self.assertEqual([x[0] for x in self.abs.writes], ["position", "finished"])

    def test_reopen_abs_uses_two_steps_and_preserves_desired_position(self):
        self.abs.set(100, True, STAMP)
        self.bo.set(100, "read", "2026-10-30")
        self.baseline()
        self.bo.set(25, "rereading")
        result = self.cycle()
        self.assertEqual(result["synced"], 1, result)
        self.assertEqual(self.abs.writes[0], ("finished", False, None))
        self.assertEqual(self.abs.writes[1], ("position", 25))
        self.assertFalse(self.abs.raw["isFinished"])
        self.assertEqual(self.abs.raw["currentTime"], 25)

    def test_unchanged_followup_does_not_write_again(self):
        self.abs.set(25)
        self.assertEqual(self.cycle()["synced"], 1)
        writes = copy.deepcopy(self.bo.writes)
        self.assertEqual(self.cycle()["unchanged"], 1)
        self.assertEqual(self.bo.writes, writes)

    def test_settle_window_defers_then_applies_stable_state(self):
        self.abs.set(25)
        with contextlib.redirect_stdout(io.StringIO()):
            first = self.bridge.cycle(apply=True, settle=30)
            self.clock += 29
            second = self.bridge.cycle(apply=True, settle=30)
            self.clock += 1
            third = self.bridge.cycle(apply=True, settle=30)
        self.assertEqual((first["waiting"], second["waiting"], third["synced"]), (1, 1, 1))

    def test_changed_target_before_apply_is_not_overwritten(self):
        self.abs.set(25)
        pair, raw = self.bridge.read_pair(self.mapping)
        self.bo.set(18)
        with self.assertRaises(RuntimeError):
            self.bridge.apply(self.mapping, pair, raw, "abs_to_bo", None)
        self.assertFalse(self.bo.writes)

    def test_target_change_between_steps_is_not_accepted_as_new_expectation(self):
        self.abs.set(25)
        original = self.bridge.read_pair
        calls = 0

        def interleaved(mapping):
            nonlocal calls
            calls += 1
            # apply() fresh/pre/post checks have passed; user changes status
            # immediately before the unguarded intermediate read.
            if calls == 4:
                self.bo.bd["readStatus"] = {"status": "read", "startedAt": "2026-10-30",
                                            "finishedAt": "2026-10-30"}
            return original(mapping)

        pair, raw = original(self.mapping)
        with patch.object(self.bridge, "read_pair", side_effect=interleaved):
            with self.assertRaises(RuntimeError):
                self.bridge.apply(self.mapping, pair, raw, "abs_to_bo", None)
        self.assertEqual(self.bo.bd["readStatus"]["status"], "read")
        self.assertFalse(any(x[0] == "status" for x in self.bo.writes))

    def test_unexpected_target_postwrite_does_not_trigger_followup_write(self):
        self.abs.set(25)

        def external_finish(label):
            if label == "position":
                self.bo.bd["readStatus"] = {"status": "read", "startedAt": "2026-10-30",
                                            "finishedAt": "2026-10-30"}

        self.bo.after_write = external_finish
        with self.assertRaises(RuntimeError):
            self.apply("abs_to_bo")
        self.assertEqual(self.bo.bd["readStatus"]["status"], "read")
        self.assertFalse(any(x[0] == "status" for x in self.bo.writes))

    def test_external_abs_completion_after_position_is_not_reset_to_zero(self):
        self.bo.set(25)

        def external_finish(label):
            if label == "position":
                self.abs.raw["isFinished"] = True
                self.abs.raw["finishedAt"] = STAMP
                self.abs.raw["lastUpdate"] += 1

        self.abs.after_write = external_finish
        with self.assertRaises(RuntimeError):
            self.apply("bo_to_abs")
        self.assertTrue(self.abs.raw["isFinished"])
        self.assertEqual(self.abs.raw["currentTime"], 25)
        self.assertFalse(any(x[0] == "finished" for x in self.abs.writes))

    def test_partial_abs_reopen_crash_is_pending_and_never_replayed_blindly(self):
        self.abs.set(100, True, STAMP)
        self.bo.set(100, "read", "2026-10-30")
        self.baseline()
        self.bo.set(25, "rereading")

        def timeout(label):
            raise RuntimeError("simulated timeout after remote commit")

        self.abs.after_write = timeout
        first = self.cycle()
        self.assertTrue(first["blocked"])
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.abs.raw["currentTime"], 0)
        writes = copy.deepcopy(self.abs.writes)
        self.abs.after_write = None
        second = self.cycle()
        self.assertTrue(second["blocked"])
        self.assertEqual(self.abs.writes, writes)

    def test_pending_fully_reached_goal_reconciles_without_repeating(self):
        self.abs.set(5)
        self.bo.set(5)
        self.baseline()
        self.bo.set(20, "read", "2026-10-30")

        def timeout_after_final(label):
            if label == "finished":
                raise RuntimeError("simulated timeout after remote commit")

        self.abs.after_write = timeout_after_final
        first = self.cycle()
        self.assertTrue(first["blocked"])
        self.assertTrue(self.abs.raw["isFinished"])
        self.assertEqual(len(self.pending()), 1)
        writes = copy.deepcopy(self.abs.writes)
        self.abs.after_write = None
        second = self.cycle()
        self.assertFalse(second["blocked"], second)
        self.assertEqual(self.abs.writes, writes)
        self.assertEqual(self.pending(), [])

    def test_completed_unknown_date_does_not_invent_a_source_timestamp(self):
        self.bo.set(25, "read", None)
        result = self.cycle()
        # Current policy either explicitly blocks missing historical dates, or
        # sends None and accepts ABS's import-time date. It must not invent one.
        if result["synced"]:
            completion = [w for w in self.abs.writes if w[0] == "finished"]
            self.assertEqual(completion[-1][2], None)
        else:
            self.assertTrue(result["blocked"])
            self.assertFalse(self.abs.writes)
            self.assertFalse(self.pending(), "A precondition failure must not create an uncertain write")

    def test_disappeared_bo_playback_is_blocked_despite_remaining_status(self):
        self.abs.set(25)
        self.bo.set(25)
        self.baseline()
        self.bo.raw = None
        result = self.cycle()
        self.assertTrue(result["blocked"])
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_finished_position_change_with_unknown_source_date_preserves_known_date_or_blocks(self):
        self.abs.set(100, True, STAMP)
        self.bo.set(100, "read", "2026-10-30")
        self.baseline()
        self.bo.set(25, "read", None)
        result = self.cycle()
        if result["synced"]:
            self.assertEqual(self.abs.raw["finishedAt"], STAMP,
                             "Reapplying completion after a seek must not fabricate today's date")
        else:
            self.assertTrue(result["blocked"])
            self.assertFalse(self.abs.writes)
            self.assertFalse(self.pending())

    def test_bo_status_without_position_does_not_reset_abs_position(self):
        self.abs.set(25)
        self.bo.bd["readStatus"] = {"status": "read", "finishedAt": "2026-10-30"}
        result = self.cycle()
        self.assertTrue(result["blocked"])
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_abs_near_end_unfinished_blocks_bo_autocompletion(self):
        self.abs.set(99, False)
        result = self.cycle()
        self.assertTrue(result["blocked"])
        self.assertFalse(self.bo.writes)

    def test_stale_deployment_guard_blocks_all_remote_writes(self):
        self.abs.set(25)
        self.clock += 151
        with self.assertRaises(RuntimeError):
            self.cycle()
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_both_sides_changed_since_baseline_is_conflict(self):
        self.abs.set(5)
        self.bo.set(5)
        self.baseline()
        self.abs.set(15)
        self.bo.set(25)
        result = self.cycle()
        self.assertTrue(result["blocked"])
        self.assertFalse(self.abs.writes or self.bo.writes)


if __name__ == "__main__":
    unittest.main()
