"""Side-effect-free progress decisions and track-position conversion.

The caller owns authenticated reads, manifest matching, concurrency checks,
durable operation journalling, writes and verification. No timestamp wins a
conflict here. Commit a new baseline only after the caller verifies both sides.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

POSITION_TOLERANCE = 1.0  # seconds; avoid write loops from player rounding
TRACK_EPSILON = 1e-6  # floating-point arithmetic only, not format drift
STATUSES = frozenset({"not_started", "in_progress", "finished"})


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError(f"{name} is too large") from None
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def _state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("state must be an explicit mapping, not null")
    missing = value.get("missing", False)
    if not isinstance(missing, bool):
        raise ValueError("missing must be boolean")
    # Only an explicit missing flag may supply the default untouched state.
    position = _number(value.get("position", 0 if missing else None), "position")
    status = value.get("status", "not_started" if missing else None)
    if status not in STATUSES:
        raise ValueError("unknown status")
    if status == "not_started" and position != 0:
        raise ValueError("not_started has a nonzero position")
    if missing and (status != "not_started" or position != 0):
        raise ValueError("missing record has active progress")
    fingerprint = value.get("fingerprint")
    if fingerprint is not None and (not isinstance(fingerprint, str) or not fingerprint):
        raise ValueError("fingerprint must be a nonempty string when present")
    return {"position": position, "status": status, "missing": missing,
            "fingerprint": fingerprint}


def _equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return (left["status"] == right["status"] and
            abs(left["position"] - right["position"]) <= POSITION_TOLERANCE)


def _active(state: Mapping[str, Any]) -> bool:
    return state["status"] != "not_started"


def decide(abs_state: Mapping[str, Any], bo_state: Mapping[str, Any],
           baseline: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Return a direction, accepted baseline, or conflict without writing.

    ``missing: True`` means a successful API read proved there is no record;
    it must never stand for an API/network/authentication error. Missing a
    formerly active record is a conflict, not an instruction to reset progress.

    Baseline is ``{"abs": state, "bo": state}``. A stored fingerprint binds a
    side to its validated manifest; disappearance/change of it blocks writes.
    Fingerprints across services need not be identical.
    """
    try:
        a, b = _state(abs_state), _state(bo_state)
        previous = None
        if baseline is not None:
            if not isinstance(baseline, Mapping) or "abs" not in baseline or "bo" not in baseline:
                raise ValueError("baseline must contain abs and bo")
            previous = {"abs": _state(baseline["abs"]), "bo": _state(baseline["bo"])}
    except (ValueError, TypeError) as exc:
        return {"action": "conflict", "reason": f"invalid_state: {exc}"}

    if previous is not None:
        for side, current in (("abs", a), ("bo", b)):
            old = previous[side]
            if old["fingerprint"] is not None and old["fingerprint"] != current["fingerprint"]:
                return {"action": "conflict", "reason": f"{side}_manifest_changed"}
            if current["missing"] and _active(old):
                return {"action": "conflict", "reason": f"{side}_active_record_disappeared"}

    if _equal(a, b):
        return {"action": "baseline", "reason": "already_equal"}

    if previous is None:
        if _active(a) and not _active(b):
            return {"action": "abs_to_bo", "reason": "initial_abs_progress_only"}
        if _active(b) and not _active(a):
            return {"action": "bo_to_abs", "reason": "initial_bo_progress_only"}
        return {"action": "conflict", "reason": "initial_progress_disagrees"}

    changed_a = not _equal(a, previous["abs"])
    changed_b = not _equal(b, previous["bo"])
    if changed_a and changed_b:
        return {"action": "conflict", "reason": "both_sides_changed"}
    if changed_a:
        return {"action": "abs_to_bo", "reason": "abs_changed"}
    if changed_b:
        return {"action": "bo_to_abs", "reason": "bo_changed"}
    return {"action": "baseline", "reason": "unchanged"}


def validate_tracks(tracks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate the supplied player order. Never guess order by filename."""
    if not isinstance(tracks, Sequence) or isinstance(tracks, (str, bytes)) or not tracks:
        raise ValueError("tracks must be a nonempty sequence")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected_start = 0.0
    for track in tracks:
        if not isinstance(track, Mapping):
            raise ValueError("track must be a mapping")
        asset_id = track.get("id")
        if not isinstance(asset_id, str) or not asset_id:
            raise ValueError("track id must be a nonempty string")
        if asset_id in seen:
            raise ValueError("duplicate track id")
        seen.add(asset_id)
        duration = _number(track.get("duration"), "track duration")
        start = _number(track.get("start"), "track start")
        if duration == 0:
            raise ValueError("track duration must be positive")
        if abs(start - expected_start) > TRACK_EPSILON:
            raise ValueError("track starts are not contiguous in player order")
        # Canonical cumulative starts remove insignificant arithmetic drift.
        result.append({"id": asset_id, "duration": duration, "start": expected_start})
        expected_start += duration
        if not math.isfinite(expected_start):
            raise ValueError("total duration is not finite")
    return result


def to_global(tracks: Sequence[Mapping[str, Any]], asset_id: str, position: float) -> float:
    """Convert seconds inside a known asset to cumulative book seconds."""
    checked = validate_tracks(tracks)
    local = _number(position, "track position")
    for track in checked:
        if track["id"] == asset_id:
            if local > track["duration"] + TRACK_EPSILON:
                raise ValueError("position is beyond the track")
            return track["start"] + min(local, track["duration"])
    raise ValueError("unknown track id")


def from_global(tracks: Sequence[Mapping[str, Any]], position: float) -> tuple[str, float]:
    """Convert book seconds to (asset id, seconds within asset).

    An exact internal boundary belongs to the next track. The exact total
    duration belongs to the final track at its duration (not a nonexistent
    following track). Out-of-range values are errors, never broad clamping.
    """
    checked = validate_tracks(tracks)
    absolute = _number(position, "book position")
    last = checked[-1]
    total = last["start"] + last["duration"]
    if absolute > total + TRACK_EPSILON:
        raise ValueError("position is beyond the book")
    if absolute >= total:
        return last["id"], last["duration"]
    for track in checked:
        if absolute < track["start"] + track["duration"]:
            return track["id"], absolute - track["start"]
    raise ValueError("position cannot be resolved")
