"""Publish verified tracks on filesystems without renameat2(RENAME_NOREPLACE)."""
import hashlib
import json
import os
import re
from pathlib import PurePosixPath
import secrets
import stat

from series_files import components, open_dir
from series_listenarr import Defer

OWNER = '.books-audio-publish'


def identity(info):
    return info.st_dev, info.st_ino


def marker_at(fd, name, proof, *, allow_foreign=False):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_size or identity(info) != tuple(proof['marker']):
        if allow_foreign:
            return False
        raise Defer('Fremde Scanner-Sperre oder ausgetauschter Auftragsnachweis')
    return True


def create_ticket(queue, key, work, target, signature):
    # A separate private hardlink proves ownership even when mergerfs calculates
    # directory inodes from their path. The scanner marker must be an empty file.
    name = '.publish-' + secrets.token_hex(24)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=work)
    try:
        # The empty marker is read by media-server UIDs distinct from the importer.
        os.fchmod(fd, 0o644)
        proof = dict(anchor=name, marker=list(identity(os.fstat(fd))), target=target, signature=signature)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(work)
    try:
        with queue.connect() as db:
            db.execute('INSERT INTO publications(id,proof) VALUES(?,?)', (key, json.dumps(proof)))
    except Exception:
        if marker_at(work, name, proof):
            os.unlink(name, dir_fd=work)
            os.fsync(work)
        raise
    return proof, 0


def check_bound_dir(parent, name, fd):
    entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(entry.st_mode) or not os.path.samestat(entry, os.fstat(fd)):
        raise Defer('Folgenordner wurde während der Veröffentlichung ausgetauscht')


def confirm_claim(parent, name, fd, proof, *, guarded):
    # Path-hash filesystems can return the same directory inode for a replacement.
    # Verify our private regular-file hardlink again through the current name.
    check_bound_dir(parent, name, fd)
    with open_dir(parent, [name]) as current:
        if not marker_at(current, OWNER, proof) or marker_at(current, '.ignore', proof) != guarded:
            raise Defer('Auftragsnachweis am aktuellen Folgenpfad stimmt nicht mehr überein')


def clear_stage(work, key, files, dirs):
    from series_imports import manifest
    try:
        entry = os.stat(key, dir_fd=work, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(entry.st_mode):
        raise Defer('Vorbereiteter Folgenordner wurde ausgetauscht')
    with open_dir(work, [key]) as stage:
        existing = manifest(stage, directories=dirs)
        if any(files.get(name) != facts for name, facts in existing.items()):
            raise Defer('Vorbereitete Audiodateien wurden verändert')
        for name, facts in existing.items():
            parts = components(name)
            with open_dir(stage, parts[:-1]) as parent:
                current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != facts:
                    raise Defer('Vorbereitete Audiodatei wurde ausgetauscht')
                os.unlink(parts[-1], dir_fd=parent)
                os.fsync(parent)
        for rel in sorted(dirs, key=lambda path: path.count('/'), reverse=True):
            parts = components(rel)
            try:
                with open_dir(stage, parts[:-1]) as parent:
                    os.rmdir(parts[-1], dir_fd=parent)
                    os.fsync(parent)
            except FileNotFoundError:
                pass
        check_bound_dir(work, key, stage)
    os.rmdir(key, dir_fd=work)
    os.fsync(work)


def checked_anchor(proof):
    anchor = proof['anchor']
    if not isinstance(anchor, str) or not re.fullmatch(r'\.publish-[0-9a-f]{48}', anchor):
        raise Defer('Ungültige Eigentumskennung im Auftragsjournal')
    return anchor


def cleanup(queue, key, work, parent, name, proof):
    """The durable completion precedes cleanup. Only unlink our own hardlinks."""
    anchor = checked_anchor(proof)
    try:
        with open_dir(parent, [name]) as dest:
            check_bound_dir(parent, name, dest)
            if marker_at(dest, OWNER, proof, allow_foreign=True):
                os.unlink(OWNER, dir_fd=dest)
                os.fsync(dest)
    except FileNotFoundError:
        pass
    release_anchor(queue, key, work, anchor, proof)


def release_anchor(queue, key, work, anchor, proof):
    if marker_at(work, anchor, proof):
        os.unlink(anchor, dir_fd=work)
        os.fsync(work)
    queue.mark(key, 'done')


def finish_committed(root, queue, key, proof):
    target = proof['target']
    if not isinstance(target, list) or len(target) != 2 or components('/'.join(target)) != target:
        raise Defer('Ungültiges Ziel im Auftragsjournal')
    with open_dir(root, ['.series-work']) as work:
        try:
            with open_dir(root, target[:-1]) as parent:
                cleanup(queue, key, work, parent, target[-1], proof)
        except FileNotFoundError:
            # An operator may remove the entire already-completed series.
            release_anchor(queue, key, work, checked_anchor(proof), proof)


def portable_publish(queue, key, work, parent, original, target, files, current_source):
    from series_imports import manifest
    name = target[-1]
    signature = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    dirs = {str(p) for rel in files for p in PurePosixPath(rel).parents if p != PurePosixPath('.')}
    proof, phase = queue.publication(key) or create_ticket(queue, key, work, target, signature)
    if proof['signature'] != signature or proof['target'] != target:
        raise Defer('Quelle oder Ziel des vorgemerkten Imports hat sich geändert')
    anchor = checked_anchor(proof)
    if phase == 2:
        cleanup(queue, key, work, parent, name, proof)
        return False
    if not marker_at(work, anchor, proof):
        raise Defer('Privater Auftragsnachweis fehlt')
    created = False
    if phase == 0:
        try:
            os.mkdir(name, mode=0o755, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
    with open_dir(parent, [name]) as dest:
        if created:
            if os.listdir(dest):
                raise Defer('Neu reservierter Folgenordner wurde bereits belegt')
            os.link(anchor, '.ignore', src_dir_fd=work, dst_dir_fd=dest, follow_symlinks=False)
            os.fsync(dest)
            os.fsync(parent)
        guarded = marker_at(dest, '.ignore', proof)
        owned = marker_at(dest, OWNER, proof)
        if not guarded and not (phase == 1 and owned):
            raise Defer('Ziel gehört nicht nachweislich zum offenen Auftrag')
        visible = manifest(dest, ignore=tuple(proof['marker']), directories=dirs)
        if any(files.get(rel) != facts for rel, facts in visible.items()) or not guarded and visible != files:
            raise Defer('Fremde oder fehlende Dateien im reservierten Folgenordner')
        if guarded:
            if not owned:
                os.link(anchor, OWNER, src_dir_fd=work, dst_dir_fd=dest, follow_symlinks=False)
                os.fsync(dest)
            for rel in sorted(files.keys() - visible.keys()):
                parts = components(rel)
                with open_dir(original, parts[:-1]) as src, open_dir(dest, parts[:-1], create=True) as out:
                    os.link(parts[-1], parts[-1], src_dir_fd=src, dst_dir_fd=out, follow_symlinks=False)
            for rel in sorted(dirs, key=lambda value: value.count('/'), reverse=True):
                with open_dir(dest, components(rel)) as subdir:
                    os.fsync(subdir)
            os.fsync(dest)
            clear_stage(work, key, files, dirs)
            if manifest(dest, ignore=tuple(proof['marker']), directories=dirs) != files or current_source() != files:
                raise Defer('Dateien haben sich vor der Freigabe geändert')
            confirm_claim(parent, name, dest, proof, guarded=True)
            with queue.connect() as db:
                db.execute('UPDATE publications SET ready=1 WHERE id=?', (key,))
            os.unlink('.ignore', dir_fd=dest)
        os.fsync(dest)
        os.fsync(parent)
        confirm_claim(parent, name, dest, proof, guarded=False)
        # Remember public visibility before dropping the temporary ownership
        # witness. A crash here can be completed without a source/catalog scan.
        with queue.connect() as db:
            db.execute('UPDATE publications SET ready=2 WHERE id=?', (key,))
            db.execute("UPDATE imports SET state='done',reason=NULL WHERE id=?", (key,))
    cleanup(queue, key, work, parent, name, proof)
    return True
