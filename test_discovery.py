import copy
import json
import unittest

from discovery import discover
from mapping import build_mapping
from test_mapping import fixture

ABS_LIBRARY = "abs-library"


def book(aid="a", bid=1, folder="One", asset_suffix="one"):
    a, b, m = fixture()
    old = a["path"]
    new = "/audiobooks/" + folder
    a.update(id=aid, path=new, libraryId=ABS_LIBRARY, mediaType="book", isMissing=False, isInvalid=False)
    b.update(id=bid, folderPath=new, libraryId=7, status="present")
    m["book"]["id"] = bid
    for t in a["media"]["tracks"]:
        t["metadata"]["path"] = t["metadata"]["path"].replace(old, new)
    for f in b["files"]:
        f["absolutePath"] = f["absolutePath"].replace(old, new)
    for asset in m["assets"]:
        asset["assetId"] += "_" + asset_suffix
    return a, b, m


class FakeABS:
    def __init__(self, books):
        self.items = {b[0]["id"]: copy.deepcopy(b[0]) for b in books}
        self.cards = [copy.deepcopy(b[0]) for b in books]
        self.library_rows = [{"id": ABS_LIBRARY, "mediaType": "book"}]
        self.fail = None
        self.calls = []

    def libraries(self):
        return copy.deepcopy(self.library_rows)

    def inventory(self, libid):
        self.calls.append(("inventory", libid))
        if self.fail == "inventory":
            raise RuntimeError("secret=DO_NOT_OUTPUT")
        return copy.deepcopy(self.cards)

    def item(self, aid):
        self.calls.append(("item", aid))
        if self.fail == aid:
            raise RuntimeError("secret=DO_NOT_OUTPUT")
        return copy.deepcopy(self.items[aid])


class FakeBO:
    def __init__(self, books):
        self.details = {b[1]["id"]: copy.deepcopy(b[1]) for b in books}
        self.manifests = {b[1]["id"]: copy.deepcopy(b[2]) for b in books}
        self.cards = [{"id": b[1]["id"], "libraryId": b[1]["libraryId"], "status": "present"} for b in books]
        self.fail = None
        self.calls = []

    def query(self):
        self.calls.append(("query",))
        if self.fail == "query":
            raise RuntimeError("Authorization: private-token")
        return copy.deepcopy(self.cards)

    def detail(self, bid):
        self.calls.append(("detail", bid))
        if self.fail == ("detail", bid):
            raise RuntimeError("private-password")
        return copy.deepcopy(self.details[bid])

    def manifest(self, bid):
        self.calls.append(("manifest", bid))
        if self.fail == ("manifest", bid):
            raise RuntimeError("private-password")
        return copy.deepcopy(self.manifests[bid])


class Discovery(unittest.TestCase):
    def setUp(self):
        self.books = [book()]
        self.abs = FakeABS(self.books)
        self.bo = FakeBO(self.books)

    def run_scan(self, previous=None, now=1000, existing=None, allowed=(7,)):
        return discover(self.abs, self.bo, existing or [], ABS_LIBRARY, allowed,
                        previous or {}, now, stable_seconds=300)

    def test_first_scan_waits_second_stable_scan_adds(self):
        first = self.run_scan()
        self.assertTrue(first["scan_ok"])
        self.assertEqual(first["new_mappings"], [])
        self.assertEqual(first["summary"]["waiting"], 1)
        early = self.run_scan(first["candidates"], now=1299)
        self.assertEqual(early["new_mappings"], [])
        ready = self.run_scan(early["candidates"], now=1300)
        self.assertEqual(len(ready["new_mappings"]), 1)

    def test_repeated_same_time_is_not_a_stability_window(self):
        first = self.run_scan()
        second = self.run_scan(first["candidates"], now=1000)
        self.assertEqual(second["new_mappings"], [])

    def test_fingerprint_change_restarts_waiting(self):
        first = self.run_scan()
        self.bo.manifests[1]["revision"] = "b" * 64
        changed = self.run_scan(first["candidates"], now=1400)
        self.assertEqual(changed["new_mappings"], [])
        self.assertEqual(changed["summary"]["reset_candidates"], 1)
        self.assertEqual(len(self.run_scan(changed["candidates"], now=1700)["new_mappings"]), 1)

    def test_removed_candidate_loses_stability(self):
        first = self.run_scan()
        cards = self.bo.cards
        self.bo.cards = []
        missing = self.run_scan(first["candidates"], now=1400)
        self.assertEqual(missing["candidates"], {})
        self.assertEqual(missing["issues"], [])
        self.bo.cards = cards
        returned = self.run_scan(missing["candidates"], now=1800)
        self.assertEqual(returned["new_mappings"], [])

    def test_duplicate_abs_or_bo_ids_abort_complete_scan(self):
        for side in ("abs", "bo"):
            self.setUp()
            target = getattr(self, side)
            target.cards.append(copy.deepcopy(target.cards[0]))
            result = self.run_scan()
            self.assertFalse(result["scan_ok"])
            self.assertEqual(result["new_mappings"], [])
            self.assertEqual(result["candidates"], {})

    def test_duplicate_folders_never_select_first_match(self):
        extra = book("b", 2, "One", "two")
        self.abs = FakeABS(self.books + [extra])
        self.bo = FakeBO(self.books + [extra])
        result = self.run_scan()
        self.assertTrue(result["scan_ok"])
        self.assertEqual(result["candidates"], {})
        self.assertEqual(result["issues"][0]["reason"], "duplicate_folder_identity")
        self.assertEqual(result["issues"][0]["title"], "One")

    def test_existing_pairs_are_unchanged_and_reserved_details_are_read(self):
        old = [build_mapping(*self.books[0])]
        before = copy.deepcopy(old)
        result = self.run_scan(existing=old)
        self.assertEqual(result["new_mappings"], [])
        self.assertEqual(result["issues"], [])
        self.assertEqual(old, before)
        self.assertIn(("detail", 1), self.bo.calls)
        self.assertNotIn(("manifest", 1), self.bo.calls)

    def test_reserved_bo_id_cannot_be_rebound_to_new_abs_id(self):
        old = [build_mapping(*self.books[0])]
        replacement = book("replacement", 1)
        self.abs = FakeABS([replacement])
        result = self.run_scan(existing=old)
        self.assertEqual(result["candidates"], {})
        self.assertEqual(result["issues"][0]["reason"], "existing_mapping_id_reserved")

    def test_recreated_both_ids_do_not_reclaim_existing_media(self):
        old = [build_mapping(*self.books[0])]
        replacement = book("replacement", 9, "One", "new")
        self.abs = FakeABS([replacement]); self.bo = FakeBO([replacement])
        result = self.run_scan(existing=old)
        self.assertEqual(result["candidates"], {})
        self.assertEqual(result["issues"][0]["reason"], "existing_mapping_folder_reserved")

    def test_legacy_mapping_without_persistent_folder_fails_closed(self):
        old = [build_mapping(*self.books[0])]
        del old[0]["folder"]
        result = self.run_scan(existing=old)
        self.assertFalse(result["scan_ok"])
        self.assertEqual(result["issues"][0]["reason"], "missing_or_duplicate_existing_folder")

    def test_nested_candidate_does_not_harvest_reserved_book_folder(self):
        old = [build_mapping(*self.books[0])]
        nested = book("new", 8, "One/Disc2", "new")
        self.abs = FakeABS([nested]); self.bo = FakeBO([nested])
        result = self.run_scan(existing=old)
        self.assertEqual(result["candidates"], {})
        self.assertEqual(result["issues"][0]["reason"], "existing_mapping_folder_reserved")

    def test_card_provides_availability_when_detail_omits_status(self):
        del self.bo.details[1]["status"]
        self.assertEqual(len(self.run_scan()["candidates"]), 1)

    def test_unknown_or_missing_card_availability_blocks_candidate(self):
        for status in ("missing", "unknown", None):
            self.bo.cards[0]["status"] = status
            result = self.run_scan()
            self.assertEqual(result["candidates"], {})
            self.assertEqual(result["issues"][0]["reason"], "bo_item_unavailable_or_unknown_state")

    def test_duplicate_folder_includes_existing_pairs(self):
        old = [build_mapping(*self.books[0])]
        new = book("new", 2, "One", "new")
        self.bo = FakeBO(self.books + [new])
        result = self.run_scan(existing=old)
        self.assertEqual(result["issues"][0]["reason"], "duplicate_folder_identity")
        self.assertEqual(result["candidates"], {})

    def test_duplicate_asset_across_two_new_books_rejects_both_not_first(self):
        second = book("b", 2, "Two", "one")
        self.abs = FakeABS(self.books + [second]); self.bo = FakeBO(self.books + [second])
        result = self.run_scan()
        self.assertTrue(result["scan_ok"])
        self.assertEqual(result["candidates"], {})
        self.assertEqual(result["summary"]["waiting"], 0)
        self.assertEqual(len(result["issues"]), 2)
        self.assertTrue(all(issue["reason"] == "duplicate_candidate_media_identity" for issue in result["issues"]))

    def test_two_new_nested_folders_with_disjoint_files_are_both_blocked(self):
        nested = book("nested", 2, "One/Other Book", "nested")
        separate = book("separate", 3, "Separate", "separate")
        self.abs = FakeABS(self.books + [nested, separate])
        self.bo = FakeBO(self.books + [nested, separate])
        first = self.run_scan()
        self.assertEqual(len(first["candidates"]), 1)
        self.assertEqual(len(first["issues"]), 2)
        self.assertTrue(all(issue["reason"] == "overlapping_candidate_folders" for issue in first["issues"]))
        ready = self.run_scan(first["candidates"], now=1400)
        self.assertEqual([m["abs_id"] for m in ready["new_mappings"]], ["separate"])

    def test_blocked_abs_issue_has_readable_title_instead_of_key_only(self):
        self.abs.cards[0]["isMissing"] = True
        result = self.run_scan()
        self.assertEqual(result["issues"][0]["title"], "Example")

    def test_bo_issue_uses_folder_basename_if_title_is_missing(self):
        self.bo.cards[0]["status"] = "missing"
        result = self.run_scan()
        self.assertEqual(result["issues"][0]["title"], "One")

    def test_late_api_failure_discards_earlier_ready_candidate(self):
        second = book("b", 2, "Two", "two")
        self.abs = FakeABS(self.books + [second]); self.bo = FakeBO(self.books + [second])
        first = self.run_scan()
        self.bo.fail = ("manifest", 2)
        failed = self.run_scan(first["candidates"], now=1400)
        self.assertFalse(failed["scan_ok"])
        self.assertEqual(failed["new_mappings"], [])
        self.assertEqual(failed["candidates"], {})
        self.assertNotIn("private-password", json.dumps(failed))

    def test_inventory_failure_is_sanitized_and_not_empty_inventory_success(self):
        self.abs.fail = "inventory"
        result = self.run_scan()
        self.assertFalse(result["scan_ok"])
        self.assertNotIn("DO_NOT_OUTPUT", json.dumps(result))

    def test_abs_scope_escape_aborts_scan(self):
        self.abs.cards[0]["libraryId"] = "another-library"
        result = self.run_scan()
        self.assertFalse(result["scan_ok"])

    def test_bo_out_of_scope_is_ignored_without_reading_detail(self):
        self.bo.cards[0]["libraryId"] = 8
        result = self.run_scan()
        self.assertTrue(result["scan_ok"])
        self.assertEqual(result["candidates"], {})
        self.assertNotIn(("detail", 1), self.bo.calls)

    def test_bo_library_change_mid_scan_aborts(self):
        self.bo.details[1]["libraryId"] = 8
        result = self.run_scan()
        self.assertFalse(result["scan_ok"])

    def test_abs_expanded_path_or_availability_change_is_blocked(self):
        self.abs.items["a"]["isMissing"] = True
        result = self.run_scan()
        self.assertEqual(result["candidates"], {})
        self.assertEqual(result["issues"][0]["reason"], "abs_item_unavailable_during_scan")

    def test_unknown_read_status_is_not_automatically_coerced(self):
        self.bo.details[1]["readStatus"] = {"status": "abandoned"}
        result = self.run_scan()
        self.assertEqual(result["candidates"], {})
        self.assertEqual(result["issues"][0]["reason"], "bo_reading_status_requires_review")

    def test_incomplete_manifest_or_ebook_does_not_become_candidate(self):
        self.bo.manifests[1]["assets"].pop()
        result = self.run_scan()
        self.assertTrue(result["scan_ok"])
        self.assertEqual(result["candidates"], {})
        self.assertEqual(result["issues"][0]["reason"], "audio_files_or_manifest_not_compatible")
        self.setUp()
        self.bo.details[1]["files"].append({"id": 20, "format": "epub", "absolutePath": "/audiobooks/One/book.epub"})
        result = self.run_scan()
        self.assertEqual(result["candidates"], {})

    def test_future_or_malformed_previous_timer_does_not_accelerate_activation(self):
        first = self.run_scan()
        saved = first["candidates"]
        key = next(iter(saved))
        saved[key]["last_seen"] = 99999
        reset = self.run_scan(saved, now=1400)
        self.assertEqual(reset["new_mappings"], [])
        self.assertEqual(reset["candidates"][key]["first_seen"], 1400)

    def test_unmatched_pending_is_normal_not_alert(self):
        self.bo.cards = []
        result = self.run_scan()
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["summary"]["waiting"], 1)

    def test_podcast_library_cannot_be_pinned(self):
        self.abs.library_rows[0]["mediaType"] = "podcast"
        self.assertFalse(self.run_scan()["scan_ok"])

    def test_no_progress_api_calls_or_input_mutation(self):
        state = self.run_scan()["candidates"]
        saved = copy.deepcopy(state)
        self.run_scan(state, now=1400)
        self.assertEqual(state, saved)
        self.assertTrue(all(call[0] in ("inventory", "item") for call in self.abs.calls))
        self.assertTrue(all(call[0] in ("query", "detail", "manifest") for call in self.bo.calls))


if __name__ == "__main__":
    unittest.main()
