import copy
import math
import unittest

from core import decide, from_global, to_global, validate_tracks


def state(position=0, status=None, **extra):
    return {"position": position,
            "status": status or ("in_progress" if position else "not_started"), **extra}


def baseline(position, status="in_progress"):
    return {"abs": state(position, status), "bo": state(position, status)}


class Decisions(unittest.TestCase):
    def action(self, a, b, old=None):
        return decide(a, b, old)["action"]

    def test_initial_empty_and_explicit_missing_are_untouched(self):
        self.assertEqual(self.action(state(), {"missing": True}), "baseline")

    def test_initial_progress_goes_to_untouched_side_in_either_direction(self):
        self.assertEqual(self.action(state(150), state()), "abs_to_bo")
        self.assertEqual(self.action(state(), state(150)), "bo_to_abs")
        self.assertEqual(self.action(state(0, "finished"), state()), "abs_to_bo")

    def test_initial_different_progress_does_not_pick_newest_or_farthest(self):
        self.assertEqual(self.action(state(100, updated=1), state(200, updated=1000)), "conflict")

    def test_position_tolerance_requires_equal_status(self):
        self.assertEqual(self.action(state(100), state(100.9)), "baseline")
        self.assertEqual(self.action(state(100), state(101.01)), "conflict")
        self.assertEqual(self.action(state(100), state(100, "finished")), "conflict")

    def test_unchanged_cycle_does_not_write(self):
        self.assertEqual(self.action(state(100), state(100), baseline(100)), "baseline")

    def test_verified_write_does_not_bounce_back(self):
        initial = baseline(100)
        self.assertEqual(self.action(state(125), state(100), initial), "abs_to_bo")
        verified = {"abs": state(125), "bo": state(125.4)}
        self.assertEqual(self.action(state(125), state(125.4), verified), "baseline")

    def test_intentional_rewind_is_transferred(self):
        self.assertEqual(self.action(state(10), state(100), baseline(100)), "abs_to_bo")
        self.assertEqual(self.action(state(100), state(10), baseline(100)), "bo_to_abs")

    def test_explicit_reset_is_not_the_same_as_disappeared_record(self):
        self.assertEqual(self.action(state(), state(100), baseline(100)), "abs_to_bo")
        answer = decide({"missing": True}, state(100), baseline(100))
        self.assertEqual(answer["action"], "conflict")
        self.assertEqual(answer["reason"], "abs_active_record_disappeared")

    def test_disappearance_does_not_hide_behind_both_sides_zero(self):
        self.assertEqual(self.action({"missing": True}, state(), baseline(100)), "conflict")

    def test_finish_and_reopen_are_status_changes(self):
        self.assertEqual(self.action(state(100, "finished"), state(100), baseline(100)), "abs_to_bo")
        self.assertEqual(self.action(state(100, "finished"), state(12), baseline(100, "finished")), "bo_to_abs")

    def test_two_changes_disagree_then_conflict(self):
        self.assertEqual(self.action(state(120), state(140), baseline(100)), "conflict")

    def test_two_changes_converge_then_accept_baseline(self):
        self.assertEqual(self.action(state(140), state(140.5), baseline(100)), "baseline")

    def test_changed_or_disappeared_fingerprint_blocks_even_equal_progress(self):
        old = {"abs": state(100, fingerprint="abs-a"), "bo": state(100, fingerprint="bo-a")}
        self.assertEqual(self.action(state(100, fingerprint="abs-a"), state(100, fingerprint="bo-b"), old), "conflict")
        self.assertEqual(self.action(state(100), state(100, fingerprint="bo-a"), old), "conflict")

    def test_distinct_service_fingerprints_are_not_compared_to_one_another(self):
        old = {"abs": state(100, fingerprint="abs-a"), "bo": state(100, fingerprint="bo-a")}
        self.assertEqual(self.action(old["abs"], old["bo"], old), "baseline")

    def test_bad_inputs_fail_closed_and_do_not_expose_input_values(self):
        invalid = [None, {}, state(math.nan), state(math.inf), state(-1),
                   state(True), state(1, "not_started"), state(1, "other"),
                   {"missing": "yes"}, {"missing": True, "position": 2},
                   state(5, fingerprint=""), {"position": 4, "status": []},
                   state(10**1000)]
        for bad in invalid:
            with self.subTest(bad=bad):
                self.assertEqual(self.action(bad, state()), "conflict")

    def test_invalid_baseline_is_not_initial_sync(self):
        self.assertEqual(self.action(state(100), state(), {}), "conflict")

    def test_inputs_are_not_mutated(self):
        a, b, old = state(55), state(100), baseline(100)
        before = copy.deepcopy((a, b, old))
        decide(a, b, old)
        self.assertEqual((a, b, old), before)


class Tracks(unittest.TestCase):
    def setUp(self):
        self.tracks = [{"id": "a", "duration": 10.5, "start": 0},
                       {"id": "b", "duration": 20, "start": 10.5},
                       {"id": "c", "duration": 30, "start": 30.5}]

    def test_start_boundaries_and_end(self):
        self.assertEqual(from_global(self.tracks, 0), ("a", 0))
        self.assertEqual(from_global(self.tracks, 10.5), ("b", 0))
        self.assertEqual(from_global(self.tracks, 30.5), ("c", 0))
        self.assertEqual(from_global(self.tracks, 60.5), ("c", 30))

    def test_track_local_conversion_uses_actual_order(self):
        self.assertEqual(to_global(self.tracks, "b", 5), 15.5)
        self.assertEqual(to_global(self.tracks, "a", 10.5), 10.5)
        self.assertEqual(to_global(self.tracks, "c", 30), 60.5)

    def test_round_trip_many_positions(self):
        for tick in range(6051):
            position = tick / 100
            asset, local = from_global(self.tracks, position)
            self.assertAlmostEqual(to_global(self.tracks, asset, local), position)

    def test_invalid_positions_and_unknown_asset_are_rejected(self):
        for bad in (-1, math.inf, math.nan, True, "1", None, 60.6):
            with self.subTest(position=bad), self.assertRaises(ValueError):
                from_global(self.tracks, bad)
        for bad in (-1, math.inf, math.nan, True, 20.1):
            with self.subTest(local=bad), self.assertRaises(ValueError):
                to_global(self.tracks, "b", bad)
        with self.assertRaises(ValueError):
            to_global(self.tracks, "missing", 0)

    def test_unknown_durations_duplicates_gaps_and_wrong_order_are_rejected(self):
        invalid = [[], [{"id": "a", "duration": 0, "start": 0}],
                   [{"id": "a", "duration": math.nan, "start": 0}],
                   [{"id": "a", "duration": 1, "start": 1}],
                   [self.tracks[1], self.tracks[0]],
                   [self.tracks[0], {"id": "a", "duration": 5, "start": 10.5}],
                   [self.tracks[0], {"id": "b", "duration": 5, "start": 10.6}],
                   [{"id": "a", "duration": 5, "start": 0}, {"id": "b", "duration": 5, "start": 4}],
                   [{"id": "a", "duration": True, "start": 0}],
                   [{"id": "", "duration": 1, "start": 0}]]
        for tracks in invalid:
            with self.subTest(tracks=tracks), self.assertRaises(ValueError):
                validate_tracks(tracks)

    def test_only_tiny_arithmetic_overrun_is_tolerated(self):
        self.assertEqual(from_global(self.tracks, 60.50000001), ("c", 30))
        self.assertEqual(to_global(self.tracks, "a", 10.50000001), 10.5)
        with self.assertRaises(ValueError):
            from_global(self.tracks, 60.501)

    def test_single_asset_book(self):
        tracks = [{"id": "m4b", "start": 0, "duration": 50000.25}]
        self.assertEqual(from_global(tracks, 14784.7), ("m4b", 14784.7))
        self.assertEqual(to_global(tracks, "m4b", 50000.25), 50000.25)


if __name__ == "__main__":
    unittest.main()
