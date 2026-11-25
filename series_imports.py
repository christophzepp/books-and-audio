"""Completed radio-drama imports. Keep the producer's registered media in place."""
import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath

from series_files import _rename_exclusive, components, open_dir
from series_listenarr import Defer, display_component, episode_label

ARR = {'readarr', 'bookshelf', 'chaptarr'}
PRODUCERS = ARR | {'shelfmark', 'readmeabook'}
AUDIO = {'.mp3', '.m4b', '.m4a', '.flac', '.ogg', '.opus', '.aac', '.wav', '.wma'}
SIDECARS = {'.jpg', '.jpeg', '.png', '.webp', '.nfo', '.xml', '.json', '.cue', '.txt', '.lrc', '.opf'}


def item_path(source, path):
    """Only explicit, provider-specific export roots enroll a completed import."""
    if source not in PRODUCERS or not isinstance(path, str):
        raise ValueError('Unbekannte Importquelle')
    for root in ('/audio-dramas', '/data/audio-dramas'):
        prefix = f'{root}/.series-import/{source}/'
        if path.startswith(prefix):
            parts = components(path[len(prefix):])
            if len(parts) < 4 or len(parts) > 10:
                raise ValueError('Die Importvorlage benötigt Autor/Serie/Folge/Datei')
            return parts
    return None


def from_event(source, event):
    if not isinstance(event, dict):
        raise ValueError('Ungültiger Import')
    if source in ARR:
        if event.get('eventType') == 'Test':
            return None
        if event.get('eventType') != 'Download':
            return None
        files = event.get('bookFiles')
        if not isinstance(files, list) or not all(isinstance(f, dict) for f in files):
            raise ValueError('Dateiliste fehlt')
        paths = [f.get('path') for f in files]
        revision = [(f.get('id'), f.get('size'), f.get('dateAdded')) for f in files]
    elif source == 'shelfmark':
        if event.get('phase') != 'post_transfer' or event.get('output', {}).get('mode') != 'folder':
            return None
        paths = event.get('paths', {}).get('final_paths')
        revision = event.get('task', {}).get('task_id')
    else:
        raise ValueError('Unbekannte Importquelle')
    if not isinstance(paths, list) or not 0 < len(paths) <= 4000 or not all(isinstance(p, str) for p in paths):
        raise ValueError('Dateiliste fehlt oder ist zu groß')
    audio = [p for p in paths if PurePosixPath(p).suffix.casefold() in AUDIO]
    if not audio:
        return None
    if source in ARR and os.environ.get('SERIES_' + source.upper() + '_ORIGIN'):
        from series_arr import source_path
        relative = [source_path(source, p) for p in audio]
        if not any(relative):
            return None
        if not all(relative):
            raise ValueError('Import enthält Audiodateien aus mehreren Wurzeln')
        book_id = event.get('book', {}).get('id')
        author_id = event.get('author', {}).get('id')
        if any(type(v) is not int or v <= 0 for v in (book_id, author_id)):
            raise ValueError('Native Buch- oder Autorenkennung fehlt')
        claimed = {}
        for f in files:
            if f['path'] not in audio:
                continue
            rel = source_path(source, f['path'])
            if rel in claimed or any(type(f.get(k)) is not int or f[k] <= 0 for k in ('id', 'size')):
                raise ValueError('Ungültige native Dateizuordnung')
            claimed[rel] = [f['id'], f['size']]
        return dict(source=source, kind='arr-event', book_id=book_id, author_id=author_id, claimed=claimed)
    entries = [item_path(source, p) for p in audio]
    if not any(entries):
        return None
    if not all(entries) or len({tuple(e[:3]) for e in entries}) != 1:
        raise ValueError('Ein Import muss genau einer Hörspielfolge gehören')
    return receipt(source, entries[0][:3], ['/'.join(e[3:]) for e in entries], revision)


def receipt(source, parts, files, revision):
    if source not in PRODUCERS or len(parts) != 3 or components('/'.join(parts)) != parts:
        raise ValueError('Ungültiges Übergabeverzeichnis')
    author, series, episode = parts
    # A sequence from the configured native naming template, never inferred from an author's name.
    m = re.fullmatch(r'(?:Folge\s+)?(\d{1,4}(?:[,.]\d{1,4})?)\s+-\s+(.+)', episode, re.IGNORECASE)
    if not m or display_component(series) != series:
        raise ValueError('Die Importvorlage benötigt Serie und „Nummer - Titel“')
    title = display_component(m[2])
    target = f'{series}/{episode_label({"seriesNumber": m[1]})} - {title}'
    if len(target.split('/')[1].encode()) > 255:
        raise ValueError('Zielname ist zu lang')
    components(target)
    if files is not None:
        files = sorted(set(files))
        if not files or any(not components(name) or PurePosixPath(name).suffix.casefold() not in AUDIO for name in files):
            raise ValueError('Ungültige Audiodatei')
    return {'source': source, 'folder': '/'.join(parts), 'target': target, 'files': files, 'revision': revision}


class ImportQueue:
    def __init__(self, state):
        self.file = Path(state) / 'imports.sqlite'
        self.file.touch(mode=0o600, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY, receipt TEXT NOT NULL, state TEXT NOT NULL, reason TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS publications (id TEXT PRIMARY KEY, proof TEXT NOT NULL, ready INTEGER NOT NULL DEFAULT 0)')

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.file, timeout=10)) as db, db:
            yield db

    def known(self, source, folder, revision):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM imports WHERE json_extract(receipt,'$.source')=? AND json_extract(receipt,'$.folder')=? AND json_extract(receipt,'$.revision') IS json_extract(?,'$') LIMIT 1",
                                   (source, folder, json.dumps(revision))).fetchone())

    def enqueue(self, job):
        raw = json.dumps(job, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        key = hashlib.sha256(raw.encode()).hexdigest()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO imports VALUES (?,?,'queued',NULL)", (key, raw))
        return key

    def queued(self):
        with self.connect() as db:
            return [(key, json.loads(raw)) for key, raw in db.execute("SELECT id,receipt FROM imports WHERE state='queued' OR (state='done' AND id IN (SELECT id FROM publications WHERE ready=2)) ORDER BY rowid LIMIT 50")]

    def mark(self, key, status, reason=None):
        with self.connect() as db:
            db.execute('UPDATE imports SET state=?,reason=? WHERE id=?', (status, reason, key))
            if status == 'done':
                db.execute('DELETE FROM publications WHERE id=?', (key,))

    def publication(self, key):
        with self.connect() as db:
            row = db.execute('SELECT proof,ready FROM publications WHERE id=?', (key,)).fetchone()
        return (json.loads(row[0]), int(row[1])) if row else None

    def conflicts(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM imports WHERE state='conflict'").fetchone()[0]


def manifest(fd, prefix='', result=None, *, ignore=None, directories=None):
    if result is None:
        result = {}
    with os.scandir(fd) as entries:
        for entry in entries:
            name = prefix + entry.name
            if name in ('.ignore', '.books-audio-publish') and ignore is not None:
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_size or (info.st_dev, info.st_ino) != ignore:
                    raise Defer('Scanner-Sperre gehört nicht zu diesem Auftrag')
                continue
            components(name)
            info = entry.stat(follow_symlinks=False)
            if len(result) >= 4000 or len(name.split('/')) > 8:
                raise Defer('Der Folgenordner ist zu groß')
            if stat.S_ISDIR(info.st_mode):
                if directories is not None and name not in directories:
                    raise Defer('Folgenziel enthält einen fremden Unterordner')
                with open_dir(fd, [entry.name]) as child:
                    manifest(child, name + '/', result, ignore=ignore, directories=directories)
            elif stat.S_ISREG(info.st_mode) and PurePosixPath(name).suffix.casefold() in AUDIO | SIDECARS:
                if info.st_size <= 0:
                    raise Defer('Import enthält eine leere Datei')
                result[name] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            else:
                raise Defer('Import enthält unbestätigte Dateien oder Links')
    return result


def exclude_from_libraries(root_fd):
    # Both Jellyfin and ABS exclude an entire directory with an empty .ignore.
    # Create only missing markers, never truncate a user's existing ignore rules.
    for part in ('.series-import', '.series-work'):
        with open_dir(root_fd, [part], create=True) as parent:
            try:
                fd = os.open('.ignore', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
            except FileExistsError:
                info = os.stat('.ignore', dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_size:
                    raise Defer('Vorhandene Scanner-Ausschlussregel erfordert Prüfung')
            else:
                os.close(fd)


def publish(root_fd, key, job, archive=None):
    # Each source owns its import folder. Only hard links are placed in the library.
    if not re.fullmatch('[a-f0-9]{64}', key):
        raise ValueError('Ungültiger Importbeleg')
    source = job['source']
    parts = components(job['folder'])
    if receipt(source, parts, job['files'], job['revision']) != job:
        raise ValueError('Importbeleg passt nicht zum Ziel')
    target = components(job['target'])
    exclude_from_libraries(root_fd)
    with open_dir(root_fd, ['.series-import', source, *parts]) as original:
        files = manifest(original)
        audio = {name for name in files if PurePosixPath(name).suffix.casefold() in AUDIO}
        if not audio or job['files'] is not None and audio != set(job['files']):
            raise Defer('Erfasste Audiodateien und Abschlussmeldung weichen voneinander ab')
        return publish_files(root_fd, key, original, target, files, whole_folder=True, archive=archive)


def selected_manifest(original, names):
    files = {}
    for name in names:
        rel = components(name)
        with open_dir(original, rel[:-1]) as parent:
            info = os.stat(rel[-1], dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise Defer('Registrierte Datei wurde ausgetauscht')
            files[name] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return files


def publish_arr(root_fd, key, event, resolved, archive=None):
    if event.get('source') not in ARR or not re.fullmatch('[a-f0-9]{64}', key):
        raise ValueError('Ungültiger nativer Arr-Beleg')
    folder, destination, sizes = resolved
    exclude_from_libraries(root_fd)
    src = components(folder) if folder else []
    with open_dir(root_fd, ['.series-import', event['source'], *src]) as original:
        files = selected_manifest(original, sizes)
        if not files or any(files[name][2] != size for name, size in sizes.items()):
            raise Defer('Dateigröße entspricht nicht dem Katalog')
        # A flat author directory can contain files belonging to other books.
        # Only publish registered tracks; do not enumerate unowned neighbours.
        return publish_files(root_fd, key, original, components(destination), files, whole_folder=False, archive=archive)


def publish_files(root_fd, key, original, target, files, *, whole_folder, archive=None):
    from series_publication import portable_publish
    def current_source():
        return manifest(original) if whole_folder else selected_manifest(original, files)

    with open_dir(root_fd, target[:-1], create=True) as parent:
        if archive is not None and archive.publication(key):
            with open_dir(root_fd, ['.series-work']) as work:
                return portable_publish(archive, key, work, parent, original, target, files, current_source)
        try:
            with open_dir(parent, target[-1:]) as existing:
                if manifest(existing) != files:
                    raise Defer('Folgenziel ist bereits anders belegt')
            return False
        except FileNotFoundError:
            pass
        with open_dir(root_fd, ['.series-work'], create=True) as work:
            with open_dir(work, [key], create=True) as stage:
                existing = manifest(stage)
                if any(files.get(name) != facts for name, facts in existing.items()):
                    raise Defer('Angefangene Veröffentlichung erfordert Prüfung')
                for name in files.keys() - existing.keys():
                    rel = components(name)
                    with open_dir(original, rel[:-1]) as src, open_dir(stage, rel[:-1], create=True) as dst:
                        os.link(rel[-1], rel[-1], src_dir_fd=src, dst_dir_fd=dst, follow_symlinks=False)
                current = current_source()
                if manifest(stage) != files or current != files:
                    raise Defer('Import hat sich vor der Veröffentlichung geändert')
            try:
                _rename_exclusive(work, key, parent, target[-1])
            except OSError as error:
                if archive is None or error.errno not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
                    raise
                return portable_publish(archive, key, work, parent, original, target, files, current_source)
        return True
