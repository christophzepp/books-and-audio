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


def relative(path, root):
    p = PurePosixPath(path)
    if not p.is_absolute() or '..' in p.parts:
        raise ValueError('Invalid ebook path')
    return p.relative_to(root)


def inspect_epub(path):
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
    return e.hash


def discover(bo, jf, spec, existing, previous, now):
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
            root = Path(spec['local_root']).resolve(strict=True)
            path = root / rel
            if path.resolve(strict=True) != path or not path.is_file():
                raise ValueError('Ebook symlink or non-file excluded')
            if path.stat().st_size != f['sizeBytes']:
                raise ValueError('Ebook size differs from BookOrbit')
            before = path.stat()
            sha = inspect_epub(path)
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError('Ebook changed during validation')
            m = dict(title=title, bo_id=d['id'], bo_library_id=spec['bo_library_id'], file_id=f['id'],
                     bo_path=f['absolutePath'], jf_path=jp, jf_id=matches[0]['Id'],
                     local_path=str(path), size=after.st_size, sha256=sha)
            key = str(f['id']); signature = hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()
            old = previous.get(key, {})
            seen = old.get('since', now) if old.get('signature') == signature else now
            obs[key] = dict(signature=signature, since=seen)
            report['candidates'] += 1
            if now - seen < 60:
                report['waiting'] += 1
            else:
                additions.append(m)
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            report['blocked'].append(dict(title=title, reason=str(exc) if isinstance(exc, ValueError) else type(exc).__name__))
    report['added'] = len(additions)
    return additions, obs, report


def refresh(bridge, apply):
    spec = bridge.config['jellyfin'].get('auto_ebooks')
    fixed = bridge.config['jellyfin'].get('ebooks', [])
    if not spec or spec.get('enabled') is not True:
        return fixed, None
    stored = bridge.load('_ebook_discovery')
    auto = stored.get('mappings', [])
    existing = fixed + auto
    if 0 <= bridge.clock() - stored.get('checked_at', 0) < 300:
        return existing, dict(enabled=True, scanned=False, added=0)
    try:
        bridge.guard(); bridge.jellyfin.guard()
        additions, observations, report = discover(bridge.bo, bridge.jellyfin, spec, existing,
                                                   stored.get('observations', {}), bridge.clock())
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
