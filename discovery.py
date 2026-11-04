"""Read-only discovery of new, unambiguous same-file audiobook pairs.

Clients must implement the complete, validated pagination in ABS.inventory()
and BO.query(). This module never writes progress, edits existing mappings,
or chooses among duplicate folders. The caller persists the returned state
atomically and separately decides when to activate new mappings.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping
from pathlib import PurePosixPath

from mapping import build_mapping, audio_path, audio_root
import native_discovery

SUPPORTED_READING = frozenset({None, "unread", "want_to_read", "reading", "rereading", "read"})
AVAILABLE_BOOK_STATES = frozenset({"present"})


class _ScanFailure(Exception):
    def __init__(self, reason):
        self.reason = reason


def _key(kind, *values):
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return kind + ":" + hashlib.sha256(encoded).hexdigest()[:24]


def _valid_abs_id(value):
    return isinstance(value, str) and bool(value) and len(value) <= 200


def _valid_bo_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _finite(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        return False


def _folder(value, folder_config=None, source="abs"):
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        return None
    try:
        value = audio_path(value, source, folder_config)
        root = PurePosixPath(audio_root(folder_config))
    except ValueError:
        return None
    path = PurePosixPath(value)
    if value != str(path) or not path.is_relative_to(root) or path == root or ".." in path.parts:
        return None
    return value


def _title(data):
    value = data.get("title")
    if value is None:
        media = data.get("media")
        metadata = media.get("metadata") if isinstance(media, Mapping) else None
        if not isinstance(metadata, Mapping):
            metadata = data.get("metadata")
        value = metadata.get("title") if isinstance(metadata, Mapping) else None
    if value is None:
        folder = _folder(data.get("folderPath") or data.get("path") or data.get("folder"))
        value = PurePosixPath(folder).name if folder else None
    if not isinstance(value, str):
        return None
    return "".join(c for c in value if c.isprintable())[:160]


def _issue(key, reason, data=None):
    result = {"key": key, "reason": reason}
    if isinstance(data, Mapping):
        title = _title(data)
        if title:
            result["title"] = title
    return result


def _rows(value, side):
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise _ScanFailure(side + "_inventory_schema")
    seen = set()
    valid = _valid_abs_id if side == "abs" else _valid_bo_id
    for row in value:
        item_id = row.get("id")
        if not valid(item_id) or item_id in seen:
            raise _ScanFailure(side + "_duplicate_or_invalid_id")
        seen.add(item_id)
    return value


def discover(abs_client, bo_client, existing_mappings, abs_library_id,
             bo_library_ids, candidate_state, now, stable_seconds=300, native_readalong_enabled=False,
             native_witnesses=None, folder_config=None):
    """Return ``scan_ok, new_mappings, candidates, issues, summary``.

    ``candidate_state`` is the previous result's ``candidates`` dictionary.
    A candidate must pass two complete scans with an identical fingerprint,
    IDs, folder and library scope, separated by at least ``stable_seconds``.
    Every complete scan rebuilds this dictionary: absent, blocked and changed
    candidates lose their old stability window. Any API/pagination failure
    discards all additions and all candidate timers, even if it occurred after
    another candidate was already ready. Existing mappings are never modified.

    ABS card fields: id, libraryId, path, mediaType, isMissing, isInvalid.
    BO cards need id/libraryId only; details are read for all allowed books,
    including reserved IDs, to detect duplicate folders reliably.
    """
    native_proofs = {}
    witnesses = dict(native_witnesses or {})
    summary = {"abs_items": 0, "bo_items_in_scope": 0, "existing_mappings": 0,
               "matched_candidates": 0, "new_mappings": 0, "waiting": 0,
               "blocked": 0, "reset_candidates": 0}
    if not _finite(now) or not _finite(stable_seconds, positive=True):
        return _failed(summary, "invalid_clock_or_stability_window")
    if not _valid_abs_id(abs_library_id):
        return _failed(summary, "invalid_abs_library_scope")
    if not isinstance(bo_library_ids, (list, tuple, set, frozenset)) or not bo_library_ids or any(not _valid_bo_id(v) for v in bo_library_ids):
        return _failed(summary, "invalid_bo_library_scope")
    allowed_bo = set(bo_library_ids)
    if not isinstance(existing_mappings, list) or not isinstance(candidate_state, Mapping):
        return _failed(summary, "invalid_local_discovery_state")
    summary["existing_mappings"] = len(existing_mappings)
    reserved_abs, reserved_bo, reserved_paths, reserved_assets = set(), set(), set(), set()
    reserved_folders = set()
    try:
        for mapping in existing_mappings:
            aid, bid = mapping["abs_id"], mapping["bo_id"]
            if (not _valid_abs_id(aid) or not _valid_bo_id(bid)
                    or aid in reserved_abs or bid in reserved_bo):
                raise _ScanFailure("duplicate_or_invalid_existing_mapping")
            folder = _folder(mapping.get("folder"), folder_config)
            if folder is None or folder in reserved_folders:
                raise _ScanFailure("missing_or_duplicate_existing_folder")
            reserved_abs.add(aid); reserved_bo.add(bid)
            reserved_folders.add(folder)
            for file in mapping.get("files", []):
                path = file.get("path")
                if not isinstance(path, str):
                    raise _ScanFailure("invalid_existing_file_identity")
                reserved_paths.add(path)
            for track in mapping.get("abs_tracks", []):
                asset = track.get("id")
                if not isinstance(asset, str):
                    raise _ScanFailure("invalid_existing_asset_identity")
                reserved_assets.add(asset)
    except (KeyError, TypeError, AttributeError):
        return _failed(summary, "invalid_existing_mapping")
    except _ScanFailure as exc:
        return _failed(summary, exc.reason)

    issues, candidates, additions = [], {}, []
    validated_candidates, waiting_candidate_keys = {}, set()
    phase = "libraries"
    try:
        libraries = _rows(abs_client.libraries(), "abs")
        selected = [lib for lib in libraries if lib["id"] == abs_library_id]
        if len(selected) != 1 or selected[0].get("mediaType") != "book":
            raise _ScanFailure("pinned_abs_audiobook_library_unavailable")
        phase = "abs_inventory"
        abs_rows = _rows(abs_client.inventory(abs_library_id), "abs")
        abs_by_id = {row["id"]: row for row in abs_rows}
        summary["abs_items"] = len(abs_rows)
        if any(row.get("libraryId") != abs_library_id for row in abs_rows):
            raise _ScanFailure("abs_inventory_escaped_library_scope")
        phase = "bo_inventory"
        bo_rows = _rows(bo_client.query(), "bo")
        # All IDs were validated before filtering; duplicate pages cannot be
        # hidden merely because one duplicate advertises another library.
        if any(not _valid_bo_id(row.get("libraryId")) for row in bo_rows):
            raise _ScanFailure("bo_inventory_missing_library_scope")
        bo_rows = [row for row in bo_rows if row["libraryId"] in allowed_bo]
        summary["bo_items_in_scope"] = len(bo_rows)
        a_folders, b_folders = defaultdict(list), defaultdict(list)
        a_invalid, b_invalid, bo_details = {}, {}, {}
        for row in abs_rows:
            aid = row["id"]; folder = _folder(row.get("path"), folder_config)
            if folder is None:
                a_invalid[aid] = "abs_folder_outside_audio_scope"
                continue
            a_folders[folder].append(row)
            if row.get("mediaType") != "book":
                a_invalid[aid] = "abs_item_is_not_a_book"
            elif not isinstance(row.get("isMissing"), bool) or not isinstance(row.get("isInvalid"), bool):
                a_invalid[aid] = "abs_availability_unknown"
            elif row["isMissing"] or row["isInvalid"]:
                a_invalid[aid] = "abs_item_missing_or_invalid"
        for card in bo_rows:
            bid = card["id"]
            phase = "bo_detail"
            detail = bo_client.detail(bid)
            if not isinstance(detail, Mapping) or detail.get("id") != bid:
                raise _ScanFailure("bo_detail_identity_mismatch")
            if detail.get("libraryId") != card["libraryId"]:
                raise _ScanFailure("bo_library_changed_during_scan")
            bo_details[bid] = detail
            folder = _folder(detail.get("folderPath"), folder_config, "bo")
            if folder is None:
                b_invalid[bid] = "bo_folder_outside_audio_scope"
                continue
            b_folders[folder].append(card)
            if (card.get("status") not in AVAILABLE_BOOK_STATES
                    or ("status" in detail and detail["status"] not in AVAILABLE_BOOK_STATES)):
                b_invalid[bid] = "bo_item_unavailable_or_unknown_state"
            rs = detail.get("readStatus")
            if rs is not None and (not isinstance(rs, Mapping) or rs.get("status") not in SUPPORTED_READING):
                b_invalid[bid] = "bo_reading_status_requires_review"

        # Mark invalid records even if they have no counterpart. Existing pairs
        # are left to the ordinary sync guard; discovery never replaces them.
        for aid, reason in a_invalid.items():
            if aid not in reserved_abs:
                issues.append(_issue(_key("abs", aid), reason, abs_by_id[aid]))
        for bid, reason in b_invalid.items():
            if bid not in reserved_bo:
                issues.append(_issue(_key("bo", bid), reason, bo_details[bid]))

        for folder in sorted(set(a_folders) | set(b_folders)):
            aa, bb = a_folders.get(folder, []), b_folders.get(folder, [])
            if len(aa) > 1 or len(bb) > 1:
                issues.append(_issue(_key("folder", folder), "duplicate_folder_identity", {"folder": folder}))
                continue
            if not aa or not bb:
                unmatched = aa or bb
                reserved = reserved_abs if aa else reserved_bo
                summary["waiting"] += sum(row["id"] not in reserved for row in unmatched)
                continue
            ar, card = aa[0], bb[0]
            aid, bid = ar["id"], card["id"]
            if aid in reserved_abs or bid in reserved_bo:
                if not any(m["abs_id"] == aid and m["bo_id"] == bid for m in existing_mappings):
                    issues.append(_issue(_key("pair", aid, bid), "existing_mapping_id_reserved", ar))
                continue
            path = PurePosixPath(folder)
            if any(path.is_relative_to(saved) or PurePosixPath(saved).is_relative_to(path)
                   for saved in reserved_folders):
                issues.append(_issue(_key("pair", aid, bid), "existing_mapping_folder_reserved", ar))
                continue
            if aid in a_invalid or bid in b_invalid:
                continue
            summary["matched_candidates"] += 1
            detail = bo_details[bid]
            phase = "abs_detail"
            expanded = abs_client.item(aid)
            if not isinstance(expanded, Mapping) or expanded.get("id") != aid:
                raise _ScanFailure("abs_detail_identity_mismatch")
            if expanded.get("libraryId") != abs_library_id:
                raise _ScanFailure("abs_detail_escaped_library_scope")
            if expanded.get("path") != folder:
                issues.append(_issue(_key("pair", aid, bid), "abs_folder_changed_during_scan", expanded))
                continue
            if (expanded.get("mediaType") != "book"
                    or expanded.get("isMissing") is not False
                    or expanded.get("isInvalid") is not False):
                issues.append(_issue(_key("pair", aid, bid), "abs_item_unavailable_during_scan", expanded))
                continue
            phase = "bo_manifest"
            manifest = bo_client.manifest(bid)
            native_proof = None
            try:
                if native_readalong_enabled and (detail.get('readAloudSync') or {}).get('state') == 'enabled':
                    prior = witnesses.get(_key('candidate', aid, bid))
                    if prior is None:
                        prior = (candidate_state.get(_key('candidate', aid, bid)) or {}).get('native_proof')
                    native_proof, mapping = native_discovery.collect_new(bo_client, expanded, detail, manifest, trusted=prior, folder_config=folder_config)
                    witnesses[_key('candidate', aid, bid)] = native_proof
                else:
                    mapping = build_mapping(expanded, detail, manifest, folder_config=folder_config)
            except ValueError:
                issues.append(_issue(_key("pair", aid, bid), "audio_files_or_manifest_not_compatible", expanded))
                continue
            if (any(f["path"] in reserved_paths for f in mapping["files"])
                    or any(t["id"] in reserved_assets for t in mapping["abs_tracks"])):
                issues.append(_issue(_key("pair", aid, bid), "existing_mapping_media_reserved", expanded))
                continue
            key = _key("candidate", aid, bid)
            previous = candidate_state.get(key)
            same = (isinstance(previous, Mapping)
                    and previous.get("fingerprint") == mapping["fingerprint"]
                    and previous.get("native_signature") == (native_discovery.signature(native_proof) if native_proof else None)
                    and previous.get("abs_id") == aid and previous.get("bo_id") == bid
                    and previous.get("folder") == folder
                    and previous.get("abs_library_id") == abs_library_id
                    and previous.get("bo_library_id") == detail["libraryId"]
                    and _finite(previous.get("first_seen")) and _finite(previous.get("last_seen"))
                    and previous["first_seen"] <= previous["last_seen"] <= now)
            if previous is not None and not same:
                summary["reset_candidates"] += 1
            first_seen = previous["first_seen"] if same else now
            candidates[key] = {"abs_id": aid, "bo_id": bid, "folder": folder,
                               "abs_library_id": abs_library_id, "bo_library_id": detail["libraryId"],
                               "fingerprint": mapping["fingerprint"],
                               "first_seen": first_seen, "last_seen": now}
            if native_proof is not None:
                candidates[key]['native_signature'] = native_discovery.signature(native_proof)
                candidates[key]['native_proof'] = native_proof
                native_proofs[aid] = native_proof
            validated_candidates[key] = mapping
            if same and now - first_seen >= stable_seconds:
                additions.append(mapping)
            else:
                summary["waiting"] += 1
                waiting_candidate_keys.add(key)
    except _ScanFailure as exc:
        return _failed(summary, exc.reason, witnesses)
    except Exception:
        # Do not use exception text: third-party libraries may embed credentials
        # or response data even though our normal clients sanitize their errors.
        return _failed(summary, phase + "_api_or_schema_failure", witnesses)

    # Never accept the first of two newly discovered books sharing an asset
    # identity. Detect this across the entire batch, including waiting pairs.
    media_owners = defaultdict(set)
    for key, mapping in validated_candidates.items():
        for track in mapping["abs_tracks"]:
            media_owners[("asset", track["id"])].add(key)
        for file in mapping["files"]:
            media_owners[("path", file["path"])].add(key)
    duplicate_candidates = set().union(*(owners for owners in media_owners.values() if len(owners) > 1))
    overlapping_folders = set()
    candidate_pairs = list(validated_candidates.items())
    for index, (first_key, first) in enumerate(candidate_pairs):
        first_folder = PurePosixPath(first["folder"])
        for second_key, second in candidate_pairs[index + 1:]:
            second_folder = PurePosixPath(second["folder"])
            if first_folder.is_relative_to(second_folder) or second_folder.is_relative_to(first_folder):
                overlapping_folders.update((first_key, second_key))
    duplicate_candidates.update(overlapping_folders)
    if duplicate_candidates:
        for key in sorted(duplicate_candidates):
            candidates.pop(key, None)
            reason = "overlapping_candidate_folders" if key in overlapping_folders else "duplicate_candidate_media_identity"
            issues.append(_issue(key, reason, validated_candidates[key]))
        additions = [mapping for mapping in additions
                     if _key("candidate", mapping["abs_id"], mapping["bo_id"]) not in duplicate_candidates]
        summary["waiting"] -= len(waiting_candidate_keys & duplicate_candidates)
    summary["new_mappings"] = len(additions)
    summary["blocked"] = len(issues)
    issues.sort(key=lambda item: (item["key"], item["reason"]))
    return {"scan_ok": True, "new_mappings": additions, "candidates": candidates,
            "native_witnesses": witnesses,
            "native_readalongs": {m['abs_id']: native_proofs[m['abs_id']] for m in additions if m['abs_id'] in native_proofs},
            "issues": issues, "summary": summary}


def _failed(summary, reason, witnesses=None):
    summary = dict(summary)
    summary.update(new_mappings=0, blocked=1)
    return {"scan_ok": False, "new_mappings": [], "candidates": {},
            **({"native_witnesses": witnesses} if witnesses is not None else {}),
            "issues": [{"key": "scan:" + reason, "reason": reason}], "summary": summary}
