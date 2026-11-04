import copy
import hashlib
import math
import unittest

from mapping import build_mapping, bo_to_canonical, canonical_to_bo


def fixture():
    paths = ["/audiobooks/Example/01.mp3", "/audiobooks/Example/02.mp3"]
    a = {"id": "abs-book", "path": "/audiobooks/Example", "media": {
        "metadata": {"title": "Example"}, "duration": 30.5,
        "tracks": [{"metadata": {"path": paths[0], "size": 100, "mtimeMs": 1000}, "duration": 10.4, "startOffset": 0},
                   {"metadata": {"path": paths[1], "size": 200, "mtimeMs": 2000}, "duration": 20.1, "startOffset": 10.4}]}}
    bd = {"id": 23, "libraryId": 7, "folderPath": "/audiobooks/Example", "readAloudSync": {"state": "unavailable"},
          "files": [{"id": 11, "absolutePath": paths[0], "sizeBytes": 100, "durationSeconds": 10, "format": "mp3"},
                    {"id": 12, "absolutePath": paths[1], "sizeBytes": 200, "durationSeconds": 20, "format": "mp3"}]}
    bm = {"schema": "bookorbit.audiobook-manifest", "schemaVersion": 2, "revision": "a" * 64,
          "book": {"id": 23}, "totalDurationMs": 30000, "chapters": [],
          "assets": [{"assetId": "aud_1", "fileId": 11, "sequence": 0, "durationMs": 10000, "sizeBytes": 100, "etag": hashlib.sha256(b"a").hexdigest()},
                     {"assetId": "aud_2", "fileId": 12, "sequence": 1, "durationMs": 20000, "sizeBytes": 200, "etag": hashlib.sha256(b"b").hexdigest()}]}
    return a, bd, bm


class MappingValidation(unittest.TestCase):
    def test_matching_files_return_canonical_and_player_timelines(self):
        m = build_mapping(*fixture())
        self.assertEqual(m["abs_id"], "abs-book")
        self.assertEqual(m["bo_id"], 23)
        self.assertEqual(m["bo_library_id"], 7)
        self.assertEqual(m["abs_tracks"][1], {"id": "aud_2", "start": 10.4, "duration": 20.1})
        self.assertEqual(m["bo_tracks"][1], {"id": "aud_2", "start": 10.0, "duration": 20.0})
        self.assertFalse(m["order_differs"])

    def test_reversed_bo_order_uses_file_identity(self):
        a, bd, bm = fixture()
        bm["assets"].reverse()
        for index, asset in enumerate(bm["assets"]):
            asset["sequence"] = index
        m = build_mapping(a, bd, bm)
        self.assertTrue(m["order_differs"])
        self.assertEqual(canonical_to_bo(m, 15.4), ("aud_2", 5000))
        self.assertAlmostEqual(bo_to_canonical(m, "aud_2", 5000), 15.4)
        self.assertEqual(canonical_to_bo(m, 5), ("aud_1", 5000))
        self.assertEqual(bo_to_canonical(m, "aud_1", 5000), 5)

    def test_manifest_revision_file_mtime_size_order_and_duration_change_fingerprint(self):
        base = build_mapping(*fixture())["fingerprint"]
        a, bd, bm = fixture()
        bm["revision"] = "b" * 64
        self.assertNotEqual(build_mapping(a, bd, bm)["fingerprint"], base)
        a, bd, bm = fixture()
        a["media"]["tracks"][0]["metadata"]["mtimeMs"] += 1
        self.assertNotEqual(build_mapping(a, bd, bm)["fingerprint"], base)
        a, bd, bm = fixture()
        a["media"]["tracks"][0]["metadata"]["size"] += 1
        bd["files"][0]["sizeBytes"] += 1
        bm["assets"][0]["sizeBytes"] += 1
        self.assertNotEqual(build_mapping(a, bd, bm)["fingerprint"], base)
        a, bd, bm = fixture()
        a["media"]["tracks"][1]["duration"] += 0.1
        a["media"]["duration"] += 0.1
        self.assertNotEqual(build_mapping(a, bd, bm)["fingerprint"], base)

    def test_reading_state_and_title_do_not_change_fingerprint(self):
        a, bd, bm = fixture()
        base = build_mapping(a, bd, bm)["fingerprint"]
        a["userMediaProgress"] = {"currentTime": 5}
        a["media"]["metadata"]["title"] = "Edited title"
        bd["readStatus"] = {"status": "read"}
        self.assertEqual(build_mapping(a, bd, bm)["fingerprint"], base)

    def test_library_id_is_required_and_bound_to_fingerprint(self):
        a, bd, bm = fixture()
        base = build_mapping(a, bd, bm)["fingerprint"]
        bd["libraryId"] = 8
        self.assertNotEqual(build_mapping(a, bd, bm)["fingerprint"], base)
        del bd["libraryId"]
        with self.assertRaises(ValueError):
            build_mapping(a, bd, bm)

    def test_rejects_wrong_folder_and_noncanonical_paths(self):
        for value in ("/audiobooks/Other", "/audiobooks/../Example", "/audiobooks//Example", "relative/Example"):
            a, bd, bm = fixture()
            bd["folderPath"] = value
            with self.subTest(path=value), self.assertRaises(ValueError):
                build_mapping(a, bd, bm)

    def test_rejects_same_name_but_different_file_size(self):
        a, bd, bm = fixture()
        a["media"]["tracks"][0]["metadata"]["size"] = 101
        with self.assertRaises(ValueError):
            build_mapping(a, bd, bm)

    def test_rejects_excessive_duration_difference(self):
        a, bd, bm = fixture()
        a["media"]["tracks"][1]["duration"] = 21.01
        a["media"]["duration"] = 31.41
        with self.assertRaises(ValueError):
            build_mapping(a, bd, bm)

    def test_rejects_missing_or_duplicate_tracks(self):
        a, bd, bm = fixture()
        a["media"]["tracks"].pop()
        with self.assertRaises(ValueError):
            build_mapping(a, bd, bm)
        a, bd, bm = fixture()
        a["media"]["tracks"].append(copy.deepcopy(a["media"]["tracks"][0]))
        with self.assertRaises(ValueError):
            build_mapping(a, bd, bm)

    def test_rejects_extra_ebook(self):
        a, bd, bm = fixture()
        bd["files"].append({"id": 13, "absolutePath": "/audiobooks/Example/book.epub", "sizeBytes": 500, "durationSeconds": 0, "format": "epub"})
        with self.assertRaises(ValueError):
            build_mapping(a, bd, bm)

    def test_metadata_and_cover_files_are_not_audio_tracks(self):
        a, bd, bm = fixture()
        original = build_mapping(a, bd, bm)
        bd["files"].extend([
            {"id": 13, "absolutePath": "/audiobooks/Example/book.nfo", "format": "nfo", "role": "metadata"},
            {"id": 14, "absolutePath": "/audiobooks/Example/cover.jpg", "format": "jpg", "role": "cover"},
            {"id": 15, "absolutePath": "/audiobooks/Example/folder.png", "format": "png"},
        ])
        self.assertEqual(build_mapping(a, bd, bm), original)

    def test_metadata_role_cannot_hide_extra_audio_or_ebook(self):
        for suffix, file_format in (("mp3", "mp3"), ("epub", "nfo"), ("nfo", "epub")):
            a, bd, bm = fixture()
            bd["files"].append({"id": 13, "absolutePath": "/audiobooks/Example/extra." + suffix,
                                "sizeBytes": 100, "durationSeconds": 3, "format": file_format, "role": "metadata"})
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                build_mapping(a, bd, bm)

    def test_rejects_enabled_read_aloud(self):
        a, bd, bm = fixture()
        bd["readAloudSync"]["state"] = "enabled"
        with self.assertRaises(ValueError):
            build_mapping(a, bd, bm)

    def test_rejects_outside_folder_file_paths(self):
        a, bd, bm = fixture()
        a["media"]["tracks"][0]["metadata"]["path"] = "/audiobooks/Elsewhere/01.mp3"
        with self.assertRaises(ValueError):
            build_mapping(a, bd, bm)

    def test_rejects_bad_schema_revision_and_mismatched_book(self):
        for field, value in (("schemaVersion", 3), ("revision", "not-a-revision"), ("book", {"id": 24})):
            a, bd, bm = fixture()
            bm[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_mapping(a, bd, bm)

    def test_rejects_bad_manifest_sequence_identity_or_total(self):
        for change in ("sequence", "duplicate-id", "duplicate-file", "total", "detail-duration", "detail-size"):
            a, bd, bm = fixture()
            if change == "sequence":
                bm["assets"][1]["sequence"] = 2
            elif change == "duplicate-id":
                bm["assets"][1]["assetId"] = "aud_1"
            elif change == "duplicate-file":
                bm["assets"][1]["fileId"] = 11
            elif change == "total":
                bm["totalDurationMs"] = 30001
            elif change == "detail-duration":
                bd["files"][0]["durationSeconds"] = 11
            else:
                bd["files"][0]["sizeBytes"] = 101
            with self.subTest(change=change), self.assertRaises(ValueError):
                build_mapping(a, bd, bm)

    def test_rejects_noncontiguous_abs_offsets_and_inconsistent_total(self):
        a, bd, bm = fixture()
        a["media"]["tracks"][1]["startOffset"] = 10.5
        with self.assertRaises(ValueError):
            build_mapping(a, bd, bm)
        a, bd, bm = fixture()
        a["media"]["duration"] = 31
        with self.assertRaises(ValueError):
            build_mapping(a, bd, bm)

    def test_rejects_nan_missing_and_zero_durations(self):
        for bad in (None, 0, math.nan, math.inf, -1, True):
            a, bd, bm = fixture()
            a["media"]["tracks"][0]["duration"] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                build_mapping(a, bd, bm)

    def test_does_not_mutate_inputs(self):
        inputs = fixture()
        before = copy.deepcopy(inputs)
        build_mapping(*inputs)
        self.assertEqual(inputs, before)


class PositionTranslation(unittest.TestCase):
    def test_boundary_uses_next_abs_track_even_if_bo_order_differs(self):
        a, bd, bm = fixture()
        bm["assets"].reverse()
        for i, asset in enumerate(bm["assets"]):
            asset["sequence"] = i
        m = build_mapping(a, bd, bm)
        self.assertEqual(canonical_to_bo(m, 10.4), ("aud_2", 0))
        self.assertEqual(canonical_to_bo(m, 30.5), ("aud_2", 20000))

    def test_rounding_clamps_only_small_same_file_difference(self):
        m = build_mapping(*fixture())
        self.assertEqual(canonical_to_bo(m, 10.3), ("aud_1", 10000))
        self.assertEqual(bo_to_canonical(m, "aud_1", 10000), 10)
        self.assertEqual(canonical_to_bo(m, 30.5), ("aud_2", 20000))

    def test_rounding_in_other_direction_is_bounded(self):
        a, bd, bm = fixture()
        bd["files"][0]["durationSeconds"] = 11
        bm["assets"][0]["durationMs"] = 11000
        bm["totalDurationMs"] = 31000
        m = build_mapping(a, bd, bm)
        self.assertEqual(bo_to_canonical(m, "aud_1", 11000), 10.4)
        with self.assertRaises(ValueError):
            bo_to_canonical(m, "aud_1", 11001)

    def test_many_positions_round_trip_within_one_second(self):
        m = build_mapping(*fixture())
        for tick in range(3051):
            seconds = tick / 100
            asset_id, ms = canonical_to_bo(m, seconds)
            self.assertLessEqual(abs(bo_to_canonical(m, asset_id, ms) - seconds), 1.0)

    def test_does_not_accept_beyond_source_duration_or_unknown_asset(self):
        m = build_mapping(*fixture())
        for asset_id, ms in (("aud_1", 10001), ("unknown", 0), ("aud_1", -1), ("aud_1", True), ("aud_1", 1.5)):
            with self.subTest(asset=asset_id, ms=ms), self.assertRaises(ValueError):
                bo_to_canonical(m, asset_id, ms)
        for position in (-1, 30.6, math.inf, math.nan):
            with self.subTest(position=position), self.assertRaises(ValueError):
                canonical_to_bo(m, position)

    def test_corrupted_mapping_is_rejected(self):
        m = build_mapping(*fixture())
        m["bo_tracks"][1]["duration"] = 25
        with self.assertRaises(ValueError):
            canonical_to_bo(m, 15)


if __name__ == "__main__":
    unittest.main()
