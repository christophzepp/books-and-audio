"""Strict, side-effect-free reconciliation of a BookOrbit metadata revision.

A normal BookOrbit scan can change file.updatedAt and therefore its manifest
revision without changing the underlying audio. A refresh is safe only when
all stored mapping identity fields AND independently preserved BO asset etags
are equal. Etags bind BO's public file ID, size and mtime; they are metadata
evidence, not full audio content hashes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import PurePosixPath

from core import TRACK_EPSILON, validate_tracks

_HEX64 = re.compile(r"[0-9a-f]{64}")
_FILE_KEYS = frozenset({"path", "size", "duration", "start", "mtimeMs", "asset_id"})
_ASSET_KEYS = frozenset({"asset_id", "file_id", "sequence", "duration_ms", "size", "etag"})
_CHAPTER_KEYS = frozenset({"title", "asset_id", "sequence", "start_ms", "end_ms", "asset_offset_ms"})
_IDENTITY_KEYS = ("abs_id", "bo_id", "bo_library_id", "folder", "files",
                  "abs_tracks", "bo_tracks", "abs_duration", "bo_duration", "order_differs")


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid numeric mapping identity")
    try:
        valid = math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("invalid numeric mapping identity")
    return value


def _integer(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid integer mapping identity")
    return _number(value, positive=positive)


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not value or (isinstance(value, int) and value < 0):
        raise ValueError("invalid mapping identifier")
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def mapping_fingerprint(mapping):
    """Recompute the deployed v1 fingerprint; never silently change its schema."""
    identity = {key: mapping[key] for key in ("abs_id", "bo_id", "bo_library_id", "files", "bo_revision", "bo_tracks")}
    return hashlib.sha256(_json(identity).encode()).hexdigest()


def _validate_mapping(mapping):
    if not isinstance(mapping, Mapping):
        raise ValueError("mapping is not an object")
    if any(key not in mapping for key in (*_IDENTITY_KEYS, "bo_revision", "fingerprint")):
        raise ValueError("mapping identity evidence is incomplete")
    for key in ("abs_id", "bo_id", "bo_library_id"):
        _id(mapping[key])
    for key in ("bo_revision", "fingerprint"):
        if not isinstance(mapping[key], str) or not _HEX64.fullmatch(mapping[key]):
            raise ValueError("invalid mapping revision or fingerprint")
    folder = mapping["folder"]
    if not isinstance(folder, str) or "\x00" in folder or "\\" in folder:
        raise ValueError("invalid mapping folder")
    folder_path = PurePosixPath(folder)
    # Scope authority belongs to build_mapping's deployment-owned aliases.
    # Reconciliation only compares already pinned identities and never opens a
    # path. It must accept non-default absolute roots without rewriting them.
    if folder != str(folder_path) or not folder_path.is_absolute() or folder == '/' or folder.startswith('//') or ".." in folder_path.parts:
        raise ValueError("invalid mapping folder")
    a = validate_tracks(mapping["abs_tracks"])
    b = validate_tracks(mapping["bo_tracks"])
    files = mapping["files"]
    if not isinstance(files, list) or len(files) != len(a) or len(b) != len(a):
        raise ValueError("mapping file inventory is incomplete")
    seen_paths = set()
    for file, track in zip(files, a):
        if not isinstance(file, Mapping) or set(file) != _FILE_KEYS:
            raise ValueError("mapping file identity schema changed")
        path = file["path"]
        if not isinstance(path, str) or "\\" in path or "\x00" in path:
            raise ValueError("invalid mapping file path")
        p = PurePosixPath(path)
        if path != str(p) or not p.is_relative_to(folder_path) or p == folder_path or ".." in p.parts or path in seen_paths:
            raise ValueError("invalid or duplicate mapping file path")
        seen_paths.add(path)
        _integer(file["size"], positive=True)
        _number(file["mtimeMs"])
        _number(file["duration"], positive=True)
        _number(file["start"])
        if (file["asset_id"] != track["id"]
                or file["duration"] != track["duration"]
                or abs(file["start"] - track["start"]) > TRACK_EPSILON):
            raise ValueError("mapping file identity contradicts ABS timeline")
    if {t["id"] for t in a} != {t["id"] for t in b}:
        raise ValueError("mapping asset identities differ")
    a_by_id = {t["id"]: t for t in a}
    if any(abs(t["duration"] - a_by_id[t["id"]]["duration"]) > 1 + TRACK_EPSILON for t in b):
        raise ValueError("mapping duration identities disagree")
    for key, tracks in (("abs_duration", a), ("bo_duration", b)):
        _number(mapping[key], positive=True)
        if abs(mapping[key] - tracks[-1]["start"] - tracks[-1]["duration"]) > TRACK_EPSILON:
            raise ValueError("mapping total duration contradicts timeline")
    if not isinstance(mapping["order_differs"], bool) or mapping["order_differs"] != ([t["id"] for t in a] != [t["id"] for t in b]):
        raise ValueError("mapping order metadata contradicts timeline")
    if mapping_fingerprint(mapping) != mapping["fingerprint"]:
        raise ValueError("stored mapping fingerprint is inconsistent")


def _validate_asset_evidence(mapping, evidence):
    if not isinstance(evidence, list) or len(evidence) != len(mapping["bo_tracks"]):
        raise ValueError("BookOrbit asset evidence is incomplete")
    files = {f["asset_id"]: f for f in mapping["files"]}
    seen_files = set()
    for index, (asset, track) in enumerate(zip(evidence, mapping["bo_tracks"])):
        if not isinstance(asset, Mapping) or set(asset) != _ASSET_KEYS:
            raise ValueError("BookOrbit asset evidence schema changed")
        if asset["asset_id"] != track["id"] or asset["sequence"] != index:
            raise ValueError("BookOrbit asset evidence changed identity or order")
        _integer(asset["sequence"])
        file_id = _id(asset["file_id"])
        if file_id in seen_files:
            raise ValueError("duplicate BookOrbit file evidence")
        seen_files.add(file_id)
        _integer(asset["size"], positive=True)
        _integer(asset["duration_ms"], positive=True)
        if asset["size"] != files[track["id"]]["size"] or abs(asset["duration_ms"] / 1000 - track["duration"]) > TRACK_EPSILON:
            raise ValueError("BookOrbit asset evidence contradicts mapped file")
        if not isinstance(asset["etag"], str) or not _HEX64.fullmatch(asset["etag"]):
            raise ValueError("BookOrbit modification-time evidence is missing")
    return copy.deepcopy(evidence)


def manifest_asset_evidence(mapping, manifest):
    """Extract only identity/mtime-bound assets from the matching raw manifest.

    A historical manifest must come from a pre-existing backup. Supplying the
    current manifest twice is not evidence about a previously unseen revision.
    """
    _validate_mapping(mapping)
    if (not isinstance(manifest, Mapping)
            or manifest.get("schema") != "bookorbit.audiobook-manifest"
            or manifest.get("schemaVersion") != 2
            or manifest.get("revision") != mapping["bo_revision"]):
        raise ValueError("manifest does not corroborate the mapped revision")
    if "book" in manifest and manifest["book"].get("id") != mapping["bo_id"]:
        raise ValueError("manifest evidence belongs to another book")
    try:
        assets = [{"asset_id": a["assetId"], "file_id": a["fileId"], "sequence": a["sequence"],
                   "duration_ms": a["durationMs"], "size": a["sizeBytes"], "etag": a["etag"]}
                  for a in manifest["assets"]]
    except (KeyError, TypeError):
        raise ValueError("manifest asset evidence is incomplete") from None
    return _validate_asset_evidence(mapping, assets)


def _validate_chapter_evidence(mapping, evidence):
    if not isinstance(evidence, list):
        raise ValueError("BookOrbit chapter evidence is unavailable")
    assets = {track["id"] for track in mapping["bo_tracks"]}
    for index, chapter in enumerate(evidence):
        if not isinstance(chapter, Mapping) or set(chapter) != _CHAPTER_KEYS:
            raise ValueError("BookOrbit chapter evidence schema changed")
        if (chapter["asset_id"] not in assets or chapter["sequence"] != index
                or not isinstance(chapter["title"], str)):
            raise ValueError("BookOrbit chapter identity or order is invalid")
        for key in ("sequence", "start_ms", "end_ms", "asset_offset_ms"):
            _integer(chapter[key])
        if chapter["end_ms"] < chapter["start_ms"]:
            raise ValueError("BookOrbit chapter time range is invalid")
    return copy.deepcopy(evidence)


def manifest_chapter_evidence(mapping, manifest):
    """Preserve chapter semantics, excluding revision-derived chapter IDs."""
    _validate_mapping(mapping)
    if (not isinstance(manifest, Mapping)
            or manifest.get("schema") != "bookorbit.audiobook-manifest"
            or manifest.get("schemaVersion") != 2
            or manifest.get("revision") != mapping["bo_revision"]):
        raise ValueError("manifest does not corroborate the mapped revision")
    try:
        chapters = [{"title": c["title"], "asset_id": c["assetId"], "sequence": c["sequence"],
                     "start_ms": c["startMs"], "end_ms": c["endMs"], "asset_offset_ms": c["assetOffsetMs"]}
                    for c in manifest["chapters"]]
    except (KeyError, TypeError):
        raise ValueError("manifest chapter evidence is incomplete") from None
    return _validate_chapter_evidence(mapping, chapters)


def _evidence(mapping, manifest, kind="assets"):
    field = "bo_assets" if kind == "assets" else "bo_chapters"
    extract = manifest_asset_evidence if kind == "assets" else manifest_chapter_evidence
    validate = _validate_asset_evidence if kind == "assets" else _validate_chapter_evidence
    stored = mapping.get(field)
    observed = extract(mapping, manifest) if manifest is not None else None
    if stored is not None:
        stored = validate(mapping, stored)
        if observed is not None and _json(stored) != _json(observed):
            raise ValueError("stored and manifest " + kind + " evidence disagree")
        return stored
    if observed is not None:
        return observed
    raise ValueError("historical BookOrbit " + kind + " evidence is unavailable")


def mapping_identity_fingerprint(mapping):
    """Bind accepted revision history to all audio and independent BO evidence.

    Unlike the backward-compatible deployed fingerprint, this intentionally
    includes BO etags and chapter semantics, but excludes mutable revision,
    revision-derived fingerprint, and display title. Missing evidence blocks.
    """
    _validate_mapping(mapping)
    identity = {key: mapping[key] for key in _IDENTITY_KEYS}
    identity["bo_assets"] = _evidence(mapping, None, "assets")
    identity["bo_chapters"] = _evidence(mapping, None, "chapters")
    return hashlib.sha256(_json(identity).encode()).hexdigest()


def reconcile_manifest_revision(old, new, *, old_manifest=None, new_manifest=None):
    """Validate a metadata-only BO revision refresh and return a review result.

    Does not touch config, baselines, operation records, progress or the server.
    The caller must block pending operations, persist the refreshed mapping
    and migrate only matching old baseline fingerprints in one transaction.
    Any unknown/stale baseline must remain blocked; never synthesize progress.
    """
    try:
        _validate_mapping(old); _validate_mapping(new)
        if any(_json(old[key]) != _json(new[key]) for key in _IDENTITY_KEYS):
            raise ValueError("audio mapping identity changed; revision-only refresh refused")
        old_assets = _evidence(old, old_manifest)
        new_assets = _evidence(new, new_manifest)
        if _json(old_assets) != _json(new_assets):
            raise ValueError("BookOrbit file identity or modification time changed")
        old_chapters = _evidence(old, old_manifest, "chapters")
        new_chapters = _evidence(new, new_manifest, "chapters")
        if _json(old_chapters) != _json(new_chapters):
            raise ValueError("BookOrbit chapter structure changed")
        refreshed = copy.deepcopy(dict(new))
        refreshed["bo_assets"] = new_assets
        refreshed["bo_chapters"] = new_chapters
        return {"changed": old["bo_revision"] != new["bo_revision"], "mapping": refreshed,
                "old_fingerprint": old["fingerprint"], "new_fingerprint": new["fingerprint"],
                "old_revision": old["bo_revision"], "new_revision": new["bo_revision"]}
    except (KeyError, TypeError, AttributeError, OverflowError):
        raise ValueError("revision evidence is malformed or incomplete") from None
