"""Register stable, unambiguous EPUBs from the verified shared ebook mount.

Discovery never alters an existing mapping or writes application progress.
Read-aloud packages and mixed audio/text books are deliberately excluded.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile
from xml.dom import minidom

from epub_locations import EpubLocations
from io_policy import settings as io_settings
from io_metrics import increment


def relative(path, root):
    p = PurePosixPath(path)
    if not p.is_absolute() or '..' in p.parts:
        raise ValueError('Invalid ebook path')
    return p.relative_to(root)


def inspect_epub(path, *, return_index=False):
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('EPUB exceeds automatic mapping limit')
    with zipfile.ZipFile(path) as z:
        if sum(x.file_size for x in z.infolist()) > 128 * 1024 * 1024:
            raise ValueError('EPUB exceeds automatic unpacked limit')
        for name in z.namelist():
            if name.lower().endswith(('.smil', '.mp3', '.m4a', '.m4b', '.ogg', '.opus', '.wav')):
                raise ValueError('Read-along or embedded audio excluded')
            if name.lower().endswith('.opf'):
                raw = z.read(name)
                if len(raw) > 1024 * 1024 or b'<!ENTITY' in raw:
                    raise ValueError('Unsupported EPUB package')
                doc = minidom.parseString(raw)
                if any(e.hasAttribute('media-overlay') or e.getAttribute('media-type').startswith('audio/')
                       for e in doc.getElementsByTagName('*')):
                    raise ValueError('Read-along package excluded')
    e = EpubLocations(path)
    # Verify the conversion at every generated position before admitting a book.
    for i in range(e.total + 1):
        point, _ = e.index_to_bo(i)
        if e.bo_to_index(point) != i or e.ticks_to_index(e.index_to_ticks(i)) != i:
            raise ValueError('EPUB position conversion is not reversible')
    return e if return_index else e.hash


def file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def discover(bo, jf, spec, existing, previous, now, *, stable_seconds=60, metrics=None, index_cache=None,
             media_guard=None):
    """Return new mappings, next observations, and a safe diagnostic summary."""
    obs, additions = {}, []
    report = dict(enabled=True, candidates=0, waiting=0, added=0, blocked=[])
    cards = bo.query()
    items = jf.items(parent=spec['jf_library_id'], types='Book')
    bypath = {}
    for item in items:
        bypath.setdefault(item.get('Path'), []).append(item)
    existing_files = {m['file_id'] for m in existing}
    existing_books = {m['bo_id'] for m in existing}
    existing_jf = {m['jf_id'] for m in existing}
    candidates = []
    guarded = False
    for card in cards:
        if card.get('libraryId') != spec['bo_library_id'] or card['id'] in existing_books:
            continue
        d = bo.detail(card['id'])
        if d.get('id') != card['id'] or d.get('libraryId') != spec['bo_library_id']:
            raise ValueError('BookOrbit ebook inventory changed')
        files = d.get('files')
        if not isinstance(files, list):
            raise ValueError('Incomplete BookOrbit ebook files')
        media = [f for f in files if f.get('format', '').lower() not in ('nfo', 'jpg', 'jpeg', 'png', 'webp')]
        if len(media) != 1 or media[0].get('format', '').lower() != 'epub':
            continue
        f = media[0]
        if f['id'] in existing_files:
            continue
        candidates.append((d, f))
    # Duplicate file identities/paths must never be admitted through separate books.
    for d, f in candidates:
        title = d.get('title') or 'E-Book ' + str(d['id'])
        try:
            rel = relative(f['absolutePath'], spec['bo_root'])
            if not rel.parts or rel.suffix.lower() != '.epub' or any('readalong' in p.lower() or 'readaloud' in p.lower() for p in rel.parts):
                continue
            if sum(x['id'] == f['id'] or x['absolutePath'] == f['absolutePath'] for _, x in candidates) != 1:
                raise ValueError('Ambiguous BookOrbit ebook identity')
            jp = str(PurePosixPath(spec['jf_root']) / rel)
            matches = bypath.get(jp, [])
            if not matches:
                report['waiting'] += 1
                continue
            if len(matches) != 1 or matches[0]['Id'] in existing_jf:
                raise ValueError('Ambiguous Jellyfin ebook identity')
            if media_guard is not None and not guarded:
                media_guard()
                guarded = True
            root = Path(spec['local_root']).resolve(strict=True)
            path = root / rel
            if path.resolve(strict=True) != path or not path.is_file():
                raise ValueError('Ebook symlink or non-file excluded')
            if path.stat().st_size != f['sizeBytes']:
                raise ValueError('Ebook size differs from BookOrbit')
            before = path.stat()
            candidate = dict(title=title, bo_id=d['id'], bo_library_id=spec['bo_library_id'], file_id=f['id'],
                     bo_path=f['absolutePath'], jf_path=jp, jf_id=matches[0]['Id'],
                     local_path=str(path), size=before.st_size)
            # Observe file/API metadata first. Merely finding a new EPUB must
            # not hash and parse a file that is still being copied.
            signature = hashlib.sha256(json.dumps(dict(schema=2, candidate=candidate,
                file_identity=file_identity(before)), sort_keys=True).encode()).hexdigest()
            key = str(f['id'])
            old = previous.get(key, {})
            seen = old.get('since', now) if old.get('schema') == 2 and old.get('signature') == signature else now
            if type(seen) not in (int, float) or not 0 <= now - seen:
                seen = now
            obs[key] = dict(schema=2, signature=signature, since=seen)
            report['candidates'] += 1
            if now - seen < stable_seconds:
                report['waiting'] += 1
                if metrics is not None: increment(metrics, 'ebook_discovery_waits')
                continue
            if metrics is not None: increment(metrics, 'ebook_discovery_content_checks')
            index = inspect_epub(path, return_index=True)
            after = path.stat()
            if file_identity(before) != file_identity(after):
                obs.pop(key, None)
                raise ValueError('Ebook changed during validation')
            if index_cache is not None: index_cache.seed(index)
            additions.append(dict(candidate, sha256=index.hash))
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            report['blocked'].append(dict(title=title, reason=str(exc) if isinstance(exc, ValueError) else type(exc).__name__))
    report['added'] = len(additions)
    return additions, obs, report


def refresh(bridge, apply):
    policy = io_settings(bridge.config)
    spec = bridge.config['jellyfin'].get('auto_ebooks')
    fixed = bridge.config['jellyfin'].get('ebooks', [])
    if not spec or spec.get('enabled') is not True:
        return fixed, None
    stored = bridge.load('_ebook_discovery')
    auto = stored.get('mappings', [])
    existing = fixed + auto
    if 0 <= bridge.clock() - stored.get('checked_at', 0) < policy['ebook_discovery_interval_seconds']:
        return existing, dict(enabled=True, scanned=False, added=0)
    try:
        bridge.guard(); bridge.jellyfin.guard()
        from jellyfin_ebook import EbookCoordinator
        additions, observations, report = discover(bridge.bo, bridge.jellyfin, spec, existing,
            stored.get('observations', {}), bridge.clock(),
            stable_seconds=policy['ebook_candidate_stable_seconds'], metrics=bridge,
            index_cache=EbookCoordinator(bridge).index_cache(bridge.config),
            media_guard=(lambda: bridge.media_guard('ebooks')) if getattr(bridge, 'media_guard', None) else None)
        if apply:
            bridge.save('_ebook_discovery', dict(mappings=auto + additions, observations=observations,
                                                 checked_at=bridge.clock()))
            for m in additions:
                bridge.event('_jellyfin_ebook:' + str(m['file_id']), 'ebook_mapped', dict(title=m['title']))
        error = json.dumps(report['blocked'], ensure_ascii=False, sort_keys=True)
        if apply:
            if report['blocked'] and bridge.load('_ebook_discovery_error').get('error') != error:
                bridge.alert('_ebook_discovery', 'E-Book-Zuordnung zurueckgestellt: ' + error)
            bridge.save('_ebook_discovery_error', dict(error=error))
        return existing + additions, report
    except Exception as exc:
        # Keep existing mappings, but discard observations after incomplete scans.
        if apply:
            bridge.save('_ebook_discovery', dict(mappings=auto, observations={}))
            reason = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            if bridge.load('_ebook_discovery_error').get('error') != reason:
                bridge.alert('_ebook_discovery', 'E-Book-Zuordnung zurueckgestellt: ' + reason)
            bridge.save('_ebook_discovery_error', dict(error=reason))
        return existing, dict(enabled=True, added=0, blocked=[dict(title='Automatische E-Book-Zuordnung',
                                 reason=str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__)])
