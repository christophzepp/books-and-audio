import copy
import hashlib
import unittest

from mapping import build_mapping
from revision import manifest_asset_evidence, manifest_chapter_evidence, mapping_fingerprint, mapping_identity_fingerprint, reconcile_manifest_revision
from test_mapping import fixture


def versions():
    a, b, manifest = fixture()
    for asset in manifest["assets"]:
        asset["etag"] = hashlib.sha256(asset["assetId"].encode()).hexdigest()
    old = build_mapping(a, b, manifest)
    changed_manifest = copy.deepcopy(manifest)
    changed_manifest["revision"] = "b" * 64
    new = build_mapping(a, b, changed_manifest)
    return old, new, manifest, changed_manifest


class RevisionReconciliation(unittest.TestCase):
    def test_pure_updated_at_revision_change_with_historical_assets_is_accepted(self):
        old, new, before, after = versions()
        result = reconcile_manifest_revision(old, new, old_manifest=before, new_manifest=after)
        self.assertTrue(result["changed"])
        self.assertEqual(result["old_fingerprint"], old["fingerprint"])
        self.assertEqual(result["new_fingerprint"], new["fingerprint"])
        self.assertEqual(result["mapping"]["bo_assets"], manifest_asset_evidence(new, after))

    def test_independently_recomputes_legacy_fingerprints(self):
        old, new, before, after = versions()
        self.assertEqual(mapping_fingerprint(old), old["fingerprint"])
        self.assertEqual(mapping_fingerprint(new), new["fingerprint"])
        old["fingerprint"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "fingerprint is inconsistent"):
            reconcile_manifest_revision(old, new, old_manifest=before, new_manifest=after)

    def test_missing_historical_bo_mtime_evidence_cannot_be_reconstructed_from_abs(self):
        old, new, before, after = versions()
        del old["bo_assets"]
        with self.assertRaisesRegex(ValueError, "historical BookOrbit"):
            reconcile_manifest_revision(old, new, new_manifest=after)

    def test_current_manifest_cannot_be_used_as_old_evidence(self):
        old, new, before, after = versions()
        with self.assertRaisesRegex(ValueError, "mapped revision"):
            reconcile_manifest_revision(old, new, old_manifest=after, new_manifest=after)

    def test_saved_asset_evidence_allows_future_revision_changes(self):
        old, new, before, after = versions()
        old["bo_assets"] = manifest_asset_evidence(old, before)
        new["bo_assets"] = manifest_asset_evidence(new, after)
        result = reconcile_manifest_revision(old, new)
        self.assertTrue(result["changed"])

    def test_same_size_replacement_with_stale_abs_metadata_is_rejected(self):
        old, new, before, after = versions()
        del new["bo_assets"]
        after["assets"][0]["etag"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "modification time changed"):
            reconcile_manifest_revision(old, new, old_manifest=before, new_manifest=after)

    def test_bo_file_row_rebinding_is_rejected_even_when_asset_size_and_time_match(self):
        old, new, before, after = versions()
        del new["bo_assets"]
        after["assets"][0]["fileId"] = 999
        with self.assertRaisesRegex(ValueError, "file identity"):
            reconcile_manifest_revision(old, new, old_manifest=before, new_manifest=after)

    def test_abs_mtime_and_path_changes_are_not_silently_accepted(self):
        for field, value in (("mtimeMs", 4444), ("path", "/audiobooks/Example/replaced.mp3")):
            old, new, before, after = versions()
            new["files"][0][field] = value
            new["fingerprint"] = mapping_fingerprint(new)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "identity changed"):
                reconcile_manifest_revision(old, new, old_manifest=before, new_manifest=after)

    def test_book_ids_library_and_folder_changes_are_rejected(self):
        for field, value in (("abs_id", "new-abs"), ("bo_id", 99), ("bo_library_id", 44)):
            old, new, before, after = versions()
            new[field] = value
            new["fingerprint"] = mapping_fingerprint(new)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "identity changed"):
                reconcile_manifest_revision(old, new, old_manifest=before, new_manifest=after)

    def test_track_order_change_is_rejected(self):
        old, new, before, after = versions()
        new["bo_tracks"].reverse()
        new["bo_tracks"][0]["start"] = 0
        new["bo_tracks"][1]["start"] = new["bo_tracks"][0]["duration"]
        new["order_differs"] = True
        new["fingerprint"] = mapping_fingerprint(new)
        with self.assertRaisesRegex(ValueError, "identity changed"):
            reconcile_manifest_revision(old, new, old_manifest=before, new_manifest=after)

    def test_title_change_is_non_identity_metadata(self):
        old, new, before, after = versions()
        new["title"] = "New title"
        result = reconcile_manifest_revision(old, new, old_manifest=before, new_manifest=after)
        self.assertEqual(result["mapping"]["title"], "New title")

    def test_chapter_ids_changing_with_revision_do_not_affect_audio_identity(self):
        old, new, before, after = versions()
        chapter = {"title": "One", "assetId": "aud_1", "sequence": 0,
                   "startMs": 0, "endMs": 5000, "assetOffsetMs": 0}
        before["chapters"] = [dict(chapter, id="old")]
        after["chapters"] = [dict(chapter, id="new")]
        del old["bo_chapters"]; del new["bo_chapters"]
        self.assertTrue(reconcile_manifest_revision(old, new, old_manifest=before, new_manifest=after)["changed"])

    def test_changed_chapter_timing_is_rejected_despite_same_audio_assets(self):
        old, new, before, after = versions()
        chapter = {"title": "One", "assetId": "aud_1", "sequence": 0,
                   "startMs": 0, "endMs": 5000, "assetOffsetMs": 0}
        before["chapters"] = [dict(chapter, id="old")]
        after["chapters"] = [dict(chapter, id="new", endMs=6000)]
        del old["bo_chapters"]; del new["bo_chapters"]
        with self.assertRaisesRegex(ValueError, "chapter structure changed"):
            reconcile_manifest_revision(old, new, old_manifest=before, new_manifest=after)

    def test_missing_historical_chapter_structure_is_not_assumed_empty(self):
        old, new, before, after = versions()
        del old["bo_chapters"]
        with self.assertRaisesRegex(ValueError, "historical BookOrbit chapters"):
            reconcile_manifest_revision(old, new)

    def test_legacy_mapping_can_be_enriched_from_original_backup_without_changing_fingerprint(self):
        old, new, before, after = versions()
        old.pop("bo_assets"); old.pop("bo_chapters")
        old_fp = old["fingerprint"]
        old["bo_assets"] = manifest_asset_evidence(old, before)
        old["bo_chapters"] = manifest_chapter_evidence(old, before)
        self.assertEqual(mapping_fingerprint(old), old_fp)
        self.assertTrue(reconcile_manifest_revision(old, new)["changed"])

    def test_incomplete_asset_evidence_and_wrong_revision_are_rejected(self):
        for mutation in ("missing-etag", "missing-asset", "wrong-revision", "wrong-size"):
            old, new, before, after = versions()
            if mutation == "missing-etag":
                del before["assets"][0]["etag"]
            elif mutation == "missing-asset":
                before["assets"].pop()
            elif mutation == "wrong-revision":
                before["revision"] = "f" * 64
            else:
                before["assets"][0]["sizeBytes"] += 1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                reconcile_manifest_revision(old, new, old_manifest=before, new_manifest=after)

    def test_inconsistent_saved_and_raw_evidence_is_rejected(self):
        old, new, before, after = versions()
        old["bo_assets"] = manifest_asset_evidence(old, before)
        before["assets"][0]["etag"] = "d" * 64
        with self.assertRaisesRegex(ValueError, "disagree"):
            reconcile_manifest_revision(old, new, old_manifest=before, new_manifest=after)

    def test_returned_mapping_and_inputs_are_independent(self):
        old, new, before, after = versions()
        inputs = copy.deepcopy((old, new, before, after))
        result = reconcile_manifest_revision(old, new, old_manifest=before, new_manifest=after)
        self.assertEqual((old, new, before, after), inputs)
        result["mapping"]["files"][0]["size"] = 999
        self.assertEqual(new["files"][0]["size"], 100)

    def test_unchanged_revision_reports_no_change(self):
        old, _, before, _ = versions()
        result = reconcile_manifest_revision(old, old, old_manifest=before, new_manifest=before)
        self.assertFalse(result["changed"])

    def test_history_identity_hash_survives_revision_and_title_change_only(self):
        old, new, _, _ = versions()
        new["title"] = "Updated display title"
        self.assertNotEqual(old["fingerprint"], new["fingerprint"])
        self.assertEqual(mapping_identity_fingerprint(old), mapping_identity_fingerprint(new))

    def test_history_identity_hash_binds_bo_mtime_and_chapter_evidence(self):
        old, new, _, _ = versions()
        initial = mapping_identity_fingerprint(old)
        new["bo_assets"][0]["etag"] = "c" * 64
        self.assertNotEqual(mapping_identity_fingerprint(new), initial)
        old["bo_chapters"] = [{"title": "Chapter", "asset_id": "aud_1", "sequence": 0,
                                "start_ms": 0, "end_ms": 5000, "asset_offset_ms": 0}]
        self.assertNotEqual(mapping_identity_fingerprint(old), initial)

    def test_history_identity_hash_never_invents_missing_evidence(self):
        old, _, _, _ = versions()
        del old["bo_assets"]
        with self.assertRaisesRegex(ValueError, "historical BookOrbit"):
            mapping_identity_fingerprint(old)


if __name__ == "__main__":
    unittest.main()
