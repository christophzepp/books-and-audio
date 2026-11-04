"""Validate identical audiobook files and translate their player timelines.

This does not hash or read media. The deployment must already have verified
that both /audiobooks mounts refer to the same host directory. Matching title
text, percentages or cumulative offsets alone is deliberately insufficient.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from folder_manager import active_folders, translate, clean_path

from core import TRACK_EPSILON, from_global, to_global, validate_tracks

DURATION_TOLERANCE = 1.0
AUDIO_FORMATS = frozenset({"mp3", "m4b", "m4a", "aac", "flac", "ogg", "opus", "wav", "wma", "aiff", "aif", "alac", "mp4"})
EBOOK_FORMATS = frozenset({"epub", "pdf", "mobi", "azw", "azw3", "azw4", "fb2", "djvu", "djv", "cbr", "cbz", "cb7", "cbt", "txt", "rtf", "doc", "docx", "html", "htm", "xhtml", "chm", "lit", "pdb"})
SIDECAR_FORMATS = frozenset({"nfo", "jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "ico", "svg", "opf", "xml", "json", "cue", "srt", "vtt"})
NATIVE_READALONG_FIELDS = frozenset({"bo_id", "overlay_file_id", "overlay_path", "overlay_size"})


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError(f"{name} is too large") from None
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


def _integer(value: Any, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be a {'positive' if positive else 'nonnegative'} integer")
    return value


def _id(value: Any, name: str) -> str | int:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or value == "" or (isinstance(value, int) and value <= 0):
        raise ValueError(f"{name} is invalid")
    return value


def audio_root(folder_config=None, endpoint="abs"):
    if not folder_config or 'folder_mappings' not in folder_config:
        return '/audiobooks'
    roots = [row['paths'].get(endpoint) for row in active_folders(folder_config) if row['kind'] == 'audiobooks']
    if len(roots) != 1 or not roots[0]:
        raise ValueError('Audiobook folder alias missing or ambiguous')
    return str(clean_path(roots[0]))


def audio_path(value, source, folder_config=None):
    if not folder_config or 'folder_mappings' not in folder_config:
        return value
    return translate(value, source, 'abs', 'audiobooks', folder_config)


def _path(value: Any, name: str, folder: PurePosixPath | None = None, root='/audiobooks') -> PurePosixPath:
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise ValueError(f"{name} is not a canonical POSIX path")
    path = PurePosixPath(value)
    if value != str(path) or not path.is_relative_to(PurePosixPath(root)) or path == PurePosixPath(root) or ".." in path.parts:
        raise ValueError(f"{name} must be below the allowed audiobook root without path traversal")
    if folder is not None and (path == folder or not path.is_relative_to(folder)):
        raise ValueError(f"{name} is outside the shared book folder")
    return path


def validate_native_readalong(pin: Any, bo_id: Any, folder: Any, *, folder_config=None) -> dict[str, Any]:
    """Validate an explicit pin; it never discovers or selects an EPUB itself."""
    if not isinstance(pin, dict) or set(pin) != NATIVE_READALONG_FIELDS:
        raise ValueError("native readalong pin has unknown or missing fields")
    if _integer(pin["bo_id"], "native BookOrbit id", positive=True) != bo_id:
        raise ValueError("native readalong pin belongs to another BookOrbit book")
    _integer(pin["overlay_file_id"], "native overlay file id", positive=True)
    _integer(pin["overlay_size"], "native overlay size", positive=True)
    root = audio_root(folder_config)
    checked_folder = _path(folder, "native book folder", root=root)
    path = _path(audio_path(pin["overlay_path"], "bo", folder_config), "native overlay path", checked_folder, root=root)
    if path.suffix.lower() != ".epub":
        raise ValueError("native overlay pin must be an EPUB")
    return dict(pin)


def _native_capability(bd: Mapping[str, Any], pin: Mapping[str, Any], audio_duration: float) -> None:
    capability = bd.get("readAloudSync")
    if (not isinstance(capability, Mapping) or capability.get("mode") != "auto"
            or capability.get("state") != "enabled" or capability.get("unavailableReason") is not None
            or _integer(capability.get("overlayFileId"), "selected overlay id", positive=True) != pin["overlay_file_id"]):
        raise ValueError("the pinned native readalong is not enabled")
    actual_audio = _number(capability.get("audioDurationSeconds"), "native audio duration", positive=True)
    overlay = _number(capability.get("overlayDurationSeconds"), "native overlay duration", positive=True)
    difference = _number(capability.get("durationDifferenceSeconds"), "native duration difference")
    ratio = _number(capability.get("durationDifferenceRatio"), "native duration difference ratio")
    if abs(actual_audio - audio_duration) > TRACK_EPSILON:
        raise ValueError("native audio duration differs from the validated manifest")
    computed = abs(actual_audio - overlay)
    if (abs(difference - computed) > TRACK_EPSILON
            or abs(ratio - computed / overlay) > TRACK_EPSILON):
        raise ValueError("native duration evidence is inconsistent")
    # BookOrbit 3.5.0 bases its 5% threshold on OVERLAY duration, capped at
    # 300 seconds. Equality is accepted; never substitute audio percentage.
    if computed > min(300.0, overlay * 0.05):
        raise ValueError("native overlay and audio durations are incompatible")


def build_mapping(a: Mapping[str, Any], bd: Mapping[str, Any], bm: Mapping[str, Any],
                  *, native_readalong: Mapping[str, Any] | None = None, folder_config=None) -> dict[str, Any]:
    """Return one validated, fingerprint-bound mapping or raise ValueError.

    BookOrbit sequence numbers must be exactly 0..N-1 in returned order.
    ABS player order remains the canonical cumulative timeline even when the
    BookOrbit player has a different order. Small per-file duration rounding
    differences are tolerated without accumulating them across tracks.
    """
    try:
        return _build_mapping(a, bd, bm, native_readalong, folder_config)
    except (KeyError, TypeError, AttributeError) as exc:
        # Do not echo malformed API payloads into operational logs.
        raise ValueError("missing or malformed mapping metadata") from None


def _build_mapping(a: Mapping[str, Any], bd: Mapping[str, Any], bm: Mapping[str, Any],
                   native_readalong: Mapping[str, Any] | None, folder_config=None) -> dict[str, Any]:
    if not all(isinstance(v, Mapping) for v in (a, bd, bm)):
        raise ValueError("mapping inputs must be objects")
    abs_id = _id(a["id"], "ABS id")
    bo_id = _id(bd["id"], "BookOrbit id")
    bo_library_id = _id(bd["libraryId"], "BookOrbit library id")
    root = audio_root(folder_config)
    folder = _path(a["path"], "ABS folder", root=root)
    if _path(audio_path(bd["folderPath"], "bo", folder_config), "BookOrbit folder", root=root) != folder:
        raise ValueError("the two book folders are different")
    pin = None if native_readalong is None else validate_native_readalong(native_readalong, bo_id, str(folder), folder_config=folder_config)
    if pin is None and (bd.get("readAloudSync") or {}).get("state") == "enabled":
        raise ValueError("BookOrbit read-aloud sync is enabled")
    if bm.get("schema") != "bookorbit.audiobook-manifest" or bm.get("schemaVersion") != 2:
        raise ValueError("unsupported BookOrbit manifest schema")
    revision = bm.get("revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise ValueError("invalid BookOrbit manifest revision")
    if "book" in bm and bm["book"].get("id") != bo_id:
        raise ValueError("manifest belongs to another book")
    abs_raw = a["media"]["tracks"]
    bo_files = bd["files"]
    assets = bm["assets"]
    if not all(isinstance(v, list) and v for v in (abs_raw, bo_files, assets)):
        raise ValueError("all file/track lists must be nonempty arrays")

    by_path: dict[str, Mapping[str, Any]] = {}
    by_file_id: dict[str | int, Mapping[str, Any]] = {}
    overlay_count = 0
    native_seen_ids, native_seen_paths = set(), set()
    for file in bo_files:
        path = _path(audio_path(file["absolutePath"], "bo", folder_config), "BookOrbit file path", folder, root=root)
        if pin is not None:
            file_id = _id(file["id"], "BookOrbit file id")
            if file_id in native_seen_ids or str(path) in native_seen_paths:
                raise ValueError("duplicate native BookOrbit file identity")
            native_seen_ids.add(file_id)
            native_seen_paths.add(str(path))
        extension = path.suffix[1:].lower()
        declared_format = file.get("format", extension)
        if not isinstance(declared_format, str):
            raise ValueError("invalid declared file format")
        declared_format = declared_format.lower().lstrip(".")
        # Metadata and covers are not player tracks. Never let a misleading
        # role/format hide an ebook or an extra unrepresented audio file.
        if extension in EBOOK_FORMATS or declared_format in EBOOK_FORMATS:
            if (pin is not None and extension == declared_format == "epub"
                    and _integer(file["id"], "overlay file id", positive=True) == pin["overlay_file_id"]
                    and file["absolutePath"] == pin["overlay_path"]
                    and _integer(file["sizeBytes"], "overlay file size", positive=True) == pin["overlay_size"]):
                overlay_count += 1
                continue
            raise ValueError("book contains ebook files")
        if extension in SIDECAR_FORMATS and declared_format not in AUDIO_FORMATS:
            continue
        if declared_format not in AUDIO_FORMATS or extension not in AUDIO_FORMATS:
            raise ValueError("book contains ebook or unsupported non-audio files")
        file_id = _id(file["id"], "BookOrbit file id")
        if str(path) in by_path or file_id in by_file_id:
            raise ValueError("duplicate BookOrbit file identity")
        _integer(file["sizeBytes"], "BookOrbit file size", positive=True)
        _number(file["durationSeconds"], "BookOrbit file duration", positive=True)
        by_path[str(path)] = file
        by_file_id[file_id] = file
    if pin is not None and overlay_count != 1:
        raise ValueError("the single pinned native overlay EPUB is missing")

    assets_by_file: dict[str | int, Mapping[str, Any]] = {}
    bo_tracks = []
    bo_cursor = 0.0
    asset_ids: set[str] = set()
    for index, asset in enumerate(assets):
        if _integer(asset["sequence"], "asset sequence") != index:
            raise ValueError("manifest sequence is non-contiguous or out of order")
        asset_id = asset["assetId"]
        if not isinstance(asset_id, str) or not asset_id or asset_id in asset_ids:
            raise ValueError("duplicate or invalid asset id")
        asset_ids.add(asset_id)
        file_id = _id(asset["fileId"], "asset file id")
        if file_id not in by_file_id or file_id in assets_by_file:
            raise ValueError("manifest file identities do not match book files")
        assets_by_file[file_id] = asset
        file = by_file_id[file_id]
        asset_size = _integer(asset["sizeBytes"], "asset size", positive=True)
        if asset_size != file["sizeBytes"]:
            raise ValueError("manifest/detail file sizes disagree")
        duration_ms = _integer(asset["durationMs"], "asset duration", positive=True)
        duration = duration_ms / 1000.0
        if abs(duration - float(file["durationSeconds"])) > TRACK_EPSILON:
            raise ValueError("manifest/detail durations disagree")
        bo_tracks.append({"id": asset_id, "start": bo_cursor, "duration": duration})
        bo_cursor += duration
    if set(assets_by_file) != set(by_file_id):
        raise ValueError("manifest omits book files")
    if abs(_integer(bm["totalDurationMs"], "manifest total", positive=True) / 1000.0 - bo_cursor) > TRACK_EPSILON:
        raise ValueError("manifest total does not match its tracks")
    bo_tracks = validate_tracks(bo_tracks)
    if pin is not None:
        _native_capability(bd, pin, bo_cursor)

    abs_tracks = []
    file_identity = []
    abs_paths: set[str] = set()
    for track in abs_raw:
        metadata = track["metadata"]
        path = str(_path(metadata["path"], "ABS track path", folder, root=root))
        if path in abs_paths:
            raise ValueError("duplicate ABS track path")
        abs_paths.add(path)
        if path not in by_path:
            raise ValueError("ABS track is absent from BookOrbit")
        file = by_path[path]
        asset = assets_by_file[file["id"]]
        size = _integer(metadata["size"], "ABS track size", positive=True)
        if size != file["sizeBytes"]:
            raise ValueError("same-path audio files have different sizes")
        duration = _number(track["duration"], "ABS duration", positive=True)
        start = _number(track["startOffset"], "ABS start offset")
        mtime = _number(metadata["mtimeMs"], "ABS modification time")
        if abs(duration - asset["durationMs"] / 1000.0) > DURATION_TOLERANCE + TRACK_EPSILON:
            raise ValueError("same-file duration difference exceeds one second")
        abs_tracks.append({"id": asset["assetId"], "start": start, "duration": duration})
        file_identity.append({"path": path, "size": size, "duration": duration,
                              "start": start, "mtimeMs": mtime, "asset_id": asset["assetId"]})
    if abs_paths != set(by_path):
        raise ValueError("ABS and BookOrbit contain different audio file sets")
    abs_tracks = validate_tracks(abs_tracks)
    abs_duration = abs_tracks[-1]["start"] + abs_tracks[-1]["duration"]
    if abs(_number(a["media"]["duration"], "ABS total", positive=True) - abs_duration) > 0.001:
        raise ValueError("ABS total does not match its tracks")
    identity = {"abs_id": abs_id, "bo_id": bo_id, "bo_library_id": bo_library_id, "files": file_identity,
                "bo_revision": revision,
                "bo_tracks": bo_tracks}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    title = a["media"].get("metadata", {}).get("title") or bd.get("title") or folder.name
    if not isinstance(title, str):
        raise ValueError("title must be text")
    result = {"abs_id": abs_id, "bo_id": bo_id, "bo_library_id": bo_library_id, "title": title, "folder": str(folder),
            "abs_tracks": abs_tracks, "bo_tracks": bo_tracks,
            "abs_duration": abs_duration, "bo_duration": bo_cursor,
            "bo_revision": revision, "fingerprint": fingerprint,
            "order_differs": [x["id"] for x in abs_tracks] != [x["id"] for x in bo_tracks],
            "files": file_identity}
    # Keep the deployed fingerprint schema stable for existing baselines.
    # Supplemental independent BO mtime/chapter evidence enables only tightly
    # checked metadata-revision reconciliation, never file reassignment.
    from revision import manifest_asset_evidence, manifest_chapter_evidence
    result["bo_assets"] = manifest_asset_evidence(result, bm)
    result["bo_chapters"] = manifest_chapter_evidence(result, bm)
    return result


def _mapped_tracks(mapping: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    a, b = validate_tracks(mapping["abs_tracks"]), validate_tracks(mapping["bo_tracks"])
    a_by_id = {t["id"]: t for t in a}
    if set(a_by_id) != {t["id"] for t in b}:
        raise ValueError("mapping track identities differ")
    if any(abs(t["duration"] - a_by_id[t["id"]]["duration"]) > DURATION_TOLERANCE + TRACK_EPSILON for t in b):
        raise ValueError("mapping track durations are incompatible")
    return a, b


def bo_to_canonical(mapping: Mapping[str, Any], asset_id: str, local_ms: int) -> float:
    """Return ABS cumulative seconds using the same file, not BO offsets."""
    a, b = _mapped_tracks(mapping)
    local = _integer(local_ms, "BookOrbit local position") / 1000.0
    bo_track = next((t for t in b if t["id"] == asset_id), None)
    if bo_track is None:
        raise ValueError("unknown BookOrbit asset id")
    if local > bo_track["duration"] + TRACK_EPSILON:
        raise ValueError("BookOrbit position exceeds its source track")
    abs_track = next(t for t in a if t["id"] == asset_id)
    if local > abs_track["duration"] + DURATION_TOLERANCE + TRACK_EPSILON:
        raise ValueError("BookOrbit position exceeds the matching ABS track")
    return to_global(a, asset_id, min(local, abs_track["duration"]))


def canonical_to_bo(mapping: Mapping[str, Any], seconds: float) -> tuple[str, int]:
    """Return BookOrbit asset/local milliseconds from the ABS timeline."""
    a, b = _mapped_tracks(mapping)
    asset_id, local = from_global(a, seconds)
    bo_track = next(t for t in b if t["id"] == asset_id)
    if local > bo_track["duration"] + DURATION_TOLERANCE + TRACK_EPSILON:
        raise ValueError("ABS position exceeds the matching BookOrbit track")
    local = min(local, bo_track["duration"])
    return asset_id, min(round(local * 1000), round(bo_track["duration"] * 1000))
