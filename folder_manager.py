"""Media folder aliases, managed by the sole control worker.

This module never changes media or Docker mounts. The deployment supplies the
worker/server mount allowlist; browser input can only select an existing folder
inside it. An active alias change also needs a coordinator compatibility proof.
"""
from __future__ import annotations

import contextlib
import copy
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
import uuid
from io_metrics import increment
from media_observation import ObservationStore


class FolderManagerError(ValueError):
    """Stable, public error code (no callback or filesystem exception text)."""


KINDS = {'audiobooks': 'Hörbücher', 'ebooks': 'E-Books', 'podcasts': 'Podcasts'}
ENDPOINTS = {'server': 'Server', 'worker': 'Sync-Dienst', 'abs': 'Audiobookshelf',
             'bo': 'BookOrbit', 'jf': 'Jellyfin', 'bb': 'BookBridge'}
ID = re.compile(r'[a-z][a-z0-9-]{0,79}\Z')
LIMIT = 1024 * 1024


def fail(code):
    raise FolderManagerError(code)


def clean_path(value):
    if (not isinstance(value, str) or not value or len(value) > 4096 or '\\' in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        fail('invalid_folder_path')
    path = PurePosixPath(value)
    if (not path.is_absolute() or str(path) != value or '..' in path.parts
            or value == '/' or value.startswith('//')):
        fail('invalid_folder_path')
    return path


def _under(path, root):
    try:
        return clean_path(path).relative_to(clean_path(root))
    except ValueError:
        return None


def _name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 120 or not value.strip() or any(ord(c) < 32 for c in value):
        fail('invalid_folder_name')
    return value


def _paths(value):
    if not isinstance(value, dict) or set(value) - set(ENDPOINTS) or not value.get('worker'):
        fail('invalid_folder_paths')
    return {k: str(clean_path(v)) for k, v in value.items() if v is not None and v != ''}


def active_folders(folders_or_config):
    """Accept effective config or rows; configuration without aliases is legacy."""
    rows = (folders_or_config.get('folder_mappings', [])
            if isinstance(folders_or_config, dict) else folders_or_config)
    if not isinstance(rows, list):
        fail('invalid_folder_configuration')
    return [row for row in rows if row.get('active', True)]


def translate(path, source, target, kind, folders_or_config):
    """Translate an exact rooted path, refusing absent or ambiguous aliases.

    Source and target are endpoint IDs from ENDPOINTS. A root itself is allowed;
    media callers separately require a file below it. No filesystem is accessed.
    """
    clean_path(path)
    if source not in ENDPOINTS or target not in ENDPOINTS or kind not in KINDS:
        fail('invalid_folder_endpoint')
    found = []
    for row in active_folders(folders_or_config):
        if row.get('kind') != kind:
            continue
        paths = _paths(row.get('paths'))
        if source not in paths or target not in paths:
            continue
        rel = _under(path, paths[source])
        if rel is not None:
            found.append(str(clean_path(paths[target]) / rel))
    if len(found) != 1:
        fail('ambiguous_folder_mapping' if found else 'folder_mapping_missing')
    return found[0]



def validate_service_paths(row, deployment_mounts):
    """Prove app aliases share the worker's host directory using trusted mounts.

    deployment_mounts is a complete host-produced list of {endpoint, container,
    server}, never GUI input. Nested mounts inside a selected root are refused:
    such a root would not describe one shared directory for future additions.
    """
    paths = _paths(row['paths'])
    if not paths.get('server') or not isinstance(deployment_mounts, list):
        fail('folder_service_mounts_unverified')
    for endpoint, selected in paths.items():
        if endpoint == 'server':
            continue
        candidates = []
        for mount in deployment_mounts:
            if not isinstance(mount, dict) or set(mount) != {'endpoint', 'container', 'server'}:
                fail('invalid_folder_service_mounts')
            if mount['endpoint'] not in ENDPOINTS or mount['endpoint'] == 'server':
                fail('invalid_folder_service_mounts')
            container, server = clean_path(mount['container']), clean_path(mount['server'])
            if mount['endpoint'] != endpoint:
                continue
            relative = _under(selected, str(container))
            if relative is not None:
                candidates.append((container, server / relative))
            if _under(str(container), selected) is not None and str(container) != selected:
                fail('folder_nested_service_mount')
        if not candidates:
            fail('folder_service_mounts_unverified')
        deepest = max(len(container.parts) for container, _ in candidates)
        resolved = [server for container, server in candidates if len(container.parts) == deepest]
        if len(resolved) != 1 or str(resolved[0]) != paths['server']:
            fail('folder_service_mount_mismatch')
    return True


def default_rows(config):
    if config.get('folder_mappings') is not None:
        return copy.deepcopy(config['folder_mappings'])
    ebook = config.get('jellyfin', {}).get('auto_ebooks') or {}
    rows = [dict(id='audiobooks-default', kind='audiobooks', name='Hörbücher', active=True,
                 paths=dict(worker='/audiobooks', abs='/audiobooks', bo='/audiobooks',
                            jf='/data/audiobooks', bb='/audiobooks')),
            dict(id='ebooks-default', kind='ebooks', name='E-Books', active=True,
                 paths=dict(worker=ebook.get('local_root', '/ebooks'),
                            bo=ebook.get('bo_root', '/books'),
                            jf=ebook.get('jf_root', '/data/ebooks'), bb='/books'))]
    podcasts = config.get('podcasts')
    if isinstance(podcasts, dict) and all(podcasts.get(k) for k in ('worker_root', 'abs_root', 'jellyfin_root')):
        rows.append(dict(id='podcasts-default', kind='podcasts', name='Podcasts', active=True,
                         paths=dict(worker=podcasts['worker_root'], abs=podcasts['abs_root'], jf=podcasts['jellyfin_root'])))
    return rows


class FolderManager:
    """Persist aliases; activate only within trusted mounts and with review.

    access_roots is deployment-owned [{worker: '/ebooks', server: '/mnt/...'}].
    Server may be omitted when unavailable. Never derive it from browser values.
    mount_checker is only a test seam; production uses os.path.ismount.
    compatibility_callback(old_config, new_config) must return exactly True.
    It should check copied coordinator state + application paths without writing
    progress. Root aliases alone cannot prove that apps see the same files.
    """
    def __init__(self, root, base_config, access_roots=None, mount_checker=None,
                 generation_reader=None, clock=None):
        self.root = Path(root) / 'control'
        self.path = self.root / 'folders.json'
        self.base_config = copy.deepcopy(base_config)
        self.mount_checker = mount_checker or os.path.ismount
        self.generation_reader = generation_reader
        self._runtime_mounts = {}
        self.observations = ObservationStore(Path(root))
        self.clock = clock or time.monotonic
        self._checks = {}
        self._generation = None
        self._context = None
        self._state_signature = None
        self._epoch_nonces = {}
        self._error_epochs = {kind: self._load_epoch(kind) for kind in KINDS}
        self._invalidation_epoch = self._load_epoch('all')
        roots = access_roots if access_roots is not None else base_config.get('folder_access_roots')
        if roots is None:
            candidates = ('/audiobooks', '/ebooks', '/podcasts')
            if mount_checker is not None:
                roots = [dict(worker=p) for p in candidates if self.mount_checker(p)]
            else:
                virtual = self._mount_generation(candidates)
                targets = {m[2] for m in virtual['stable'][1]} if virtual else set()
                roots = [dict(worker=p) for p in candidates if p in targets]
        if not isinstance(roots, list) or len(roots) > 100:
            fail('invalid_folder_access_roots')
        self.access_roots = []
        for row in roots:
            if not isinstance(row, dict) or set(row) - {'worker', 'server'} or not row.get('worker'):
                fail('invalid_folder_access_roots')
            clean = {k: str(clean_path(v)) for k, v in row.items() if v is not None}
            # Broad mounts would turn an alias form into arbitrary file access.
            if clean['worker'] in {'/app', '/app-sync', '/config', '/state', '/admin-state', '/etc', '/proc', '/sys', '/dev', '/run', '/tmp', '/var', '/opt', '/mnt', '/media'}:
                fail('unsafe_folder_access_root')
            if any(_under(clean['worker'], x['worker']) is not None or _under(x['worker'], clean['worker']) is not None for x in self.access_roots):
                fail('overlapping_folder_access_roots')
            self.access_roots.append(clean)
        initial = dict(schema=1, revision=0, rows=default_rows(base_config))
        for row in initial['rows']:
            row.setdefault('name', KINDS.get(row.get('kind'), 'Ordner'))
            row.setdefault('active', True)
            row['paths'] = self._with_server(row['paths'])
        self._validate_state(initial)
        self.initial = initial

    @staticmethod
    def _mount_generation(paths=None):
        """Kernel-only, scoped mount identity stable across worker recreation.

        Container-local mount IDs are runtime change detectors, not persistent
        identities. Overlay/config mounts unrelated to the selected media roots
        cannot invalidate their history. A host reboot changes boot_id.
        """
        try:
            raw = Path('/proc/self/mountinfo').read_text()
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            if not boot or len(raw) > 16 * 1024 * 1024:
                return None
            selected = [clean_path(p) for p in paths or []]
            if not selected:
                return None
            mounts = []
            def unescape(value):
                return re.sub(r'\\(040|011|012|134)', lambda m: chr(int(m[1], 8)), value)
            for line in raw.splitlines():
                left, separator, right = line.partition(' - ')
                a, z = left.split(), right.split()
                if not separator or len(a) < 6 or len(z) < 3:
                    return None
                target = PurePosixPath(unescape(a[4]))
                if not target.is_absolute():
                    return None
                stable = [a[2], unescape(a[3]), str(target), sorted(a[5].split(',')),
                          z[0], unescape(z[1]), sorted(z[2].split(','))]
                mounts.append((target, stable, a[0]))
            covered = {}
            for path in selected:
                parents = [m for m in mounts if path.is_relative_to(m[0])]
                if not parents:
                    return None
                deepest = max(len(m[0].parts) for m in parents)
                roots = [m for m in parents if len(m[0].parts) == deepest]
                if len(roots) != 1:
                    return None
                for mount in roots + [m for m in mounts if m[0] != path and m[0].is_relative_to(path)]:
                    covered[str(mount[0])] = mount
            ordered = [covered[k] for k in sorted(covered)]
            return dict(stable=[boot, [m[1] for m in ordered]],
                        runtime=[m[2] for m in ordered])
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _signature(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                        allow_nan=False).encode()).hexdigest()

    @staticmethod
    def _row_identity(row):
        return {key: copy.deepcopy(row[key]) for key in ('id', 'kind', 'paths')}

    def _load_epoch(self, key):
        """Keep recovered history stable across restarts, failing closed on loss.

        The random incarnation prevents a missing/corrupt epoch from rolling
        back to an old generation-zero proof elsewhere in the cache. Only this
        NVMe history is touched; no media path is consulted.
        """
        saved = self.observations.get('folder-epoch', key, {'schema': 1})
        if (isinstance(saved, dict) and set(saved) == {'epoch', 'nonce'}
                and type(saved['epoch']) is int and saved['epoch'] >= 0
                and isinstance(saved['nonce'], str)
                and re.fullmatch(r'[0-9a-f]{32}', saved['nonce'])):
            self._epoch_nonces[key] = saved['nonce']
            return saved['epoch']
        self._epoch_nonces[key] = uuid.uuid4().hex
        self.observations.put('folder-epoch', key, {'schema': 1},
                              dict(epoch=0, nonce=self._epoch_nonces[key]))
        return 0

    def _bump_epoch(self, key):
        if key == 'all':
            self._invalidation_epoch += 1
            value = self._invalidation_epoch
        else:
            self._error_epochs[key] += 1
            value = self._error_epochs[key]
        self.observations.put('folder-epoch', key, {'schema': 1},
                              dict(epoch=value, nonce=self._epoch_nonces[key]))

    def _mount_scope(self, kind, rows):
        try:
            if self.generation_reader is not None:
                value = self.generation_reader()
            else:
                value = self._mount_generation([row['paths']['worker'] for row in rows])
        except Exception:
            value = None
        if isinstance(value, str) and value:
            stable, runtime = value, value
        elif (isinstance(value, dict) and set(value) == {'stable', 'runtime'}
                and value.get('stable') and value.get('runtime')):
            stable, runtime = value['stable'], value['runtime']
        else:
            return None
        runtime = self._signature(runtime)
        previous = self._runtime_mounts.get(kind)
        if previous is not None and previous != runtime:
            self._bump_epoch(kind)
        self._runtime_mounts[kind] = runtime
        return stable

    def _local_binding(self, row, mount):
        roots = [root for root in self.access_roots if _under(row['paths']['worker'], root['worker']) is not None]
        return dict(row=self._row_identity(row), roots=roots, mount=mount,
                    epoch=self._error_epochs[row['kind']], epoch_nonce=self._epoch_nonces[row['kind']],
                    invalidation=self._invalidation_epoch, invalidation_nonce=self._epoch_nonces['all'])

    def _record_check(self, row, binding=None):
        key = self._signature(row)
        try:
            proof = self.check(row)
            check = dict(ok=True, code=proof['code'])
        except FolderManagerError as exc:
            check = dict(ok=False, code=str(exc))
            self._bump_epoch(row['kind'])
            if binding is not None:
                binding = dict(binding, epoch=self._error_epochs[row['kind']])
            self.observations.forget('folders', row['id'])
        self._checks[key] = dict(check=check, at=self.clock(), binding=binding,
                                identity=proof.get('identity') if check['ok'] else None)
        if check['ok'] and binding is not None:
            self.observations.put('folders', row['id'], binding,
                                  dict(check=check, identity=proof['identity']))
        return check

    def invalidate(self):
        """Forget physical evidence after an explicit global invalidation."""
        self._checks.clear()
        self._generation = None
        self._bump_epoch('all')

    @staticmethod
    def _structural_context(context, rows):
        if not isinstance(context, dict):
            return context
        result = {k: copy.deepcopy(v) for k, v in context.items()
                  if not (k.endswith('_ok') or k.endswith('_errors'))
                  and k not in {'ok', 'checked_at', 'deployment_unavailable'}}
        endpoints = {endpoint: path for row in rows for endpoint, path in row['paths'].items()
                     if endpoint != 'server'}
        for key in ('folder_mounts', 'podcast_mounts'):
            mounts = result.get(key)
            if isinstance(mounts, dict):
                scoped = {}
                for endpoint, value in mounts.items():
                    if endpoint not in endpoints:
                        continue
                    if isinstance(value, list):
                        value = [item for item in value if isinstance(item, dict)
                                 and isinstance(item.get('destination'), str)
                                 and (_under(endpoints[endpoint], item['destination']) is not None
                                      or _under(item['destination'], endpoints[endpoint]) is not None)]
                    scoped[endpoint] = value
                result[key] = scoped
        return result

    def observation_generation(self, kind=None, *, context=None):
        """Stable read-only history binding; it never grants a fresh file proof."""
        if kind is not None and kind not in KINDS:
            fail('invalid_folder_kind')
        state, _ = self._read()
        kinds = [kind] if kind is not None else sorted(KINDS)
        values = []
        for selected in kinds:
            rows = [row for row in state['rows'] if row['active'] and row['kind'] == selected]
            if not rows:
                values.append([selected, []])
                continue
            mount = self._mount_scope(selected, rows)
            if mount is None:
                fail('folder_generation_unavailable')
            roots = [root for root in self.access_roots if any(
                _under(row['paths']['worker'], root['worker']) is not None for row in rows)]
            values.append([selected, mount, [self._row_identity(row) for row in rows], roots,
                           self._structural_context(self._context if context is None else context, rows),
                           [self._epoch_nonces['all'], self._invalidation_epoch],
                           [self._epoch_nonces[selected], self._error_epochs[selected]]])
        return self._signature(values)

    def generation(self, kind=None, *, context=None):
        return self.observation_generation(kind, context=context)

    def refresh(self, *, force=False, kind=None, context=None):
        """Reuse durable local history; only changed rows or explicit work walk media.

        interval=0 means no time-triggered positive checks. A configured positive
        interval is an opt-in integrity audit. Fresh deployment/operation guards
        are independent; this historical observation is never such a receipt.
        """
        if kind is not None and kind not in KINDS:
            fail('invalid_folder_kind')
        state, _ = self._read()
        if context is not None:
            self._context = copy.deepcopy(context)
        self._state_signature = self._signature(state)
        from io_policy import settings
        interval = settings(self.base_config)['folder_validation_interval_seconds']
        now = self.clock()
        scopes = {}
        for selected in KINDS:
            selected_rows = [row for row in state['rows'] if row['active'] and row['kind'] == selected]
            if selected_rows and (kind is None or kind == selected):
                scopes[selected] = self._mount_scope(selected, selected_rows)
        for row in state['rows']:
            if not row['active'] or kind is not None and row['kind'] != kind:
                continue
            key = self._signature(row)
            mount = scopes[row['kind']]
            binding = self._local_binding(row, mount) if mount is not None else None
            previous = self._checks.get(key)
            if previous is not None and previous.get('binding') != binding:
                previous = None
            if not force and interval == 0 and binding is not None and previous is None:
                saved = self.observations.get('folders', row['id'], binding)
                if (isinstance(saved, dict) and set(saved) == {'check', 'identity'}
                        and saved['check'] == {'ok': True, 'code': 'folder_checked'}
                        and isinstance(saved['identity'], dict)
                        and set(saved['identity']) == {'device', 'inode'}
                        and all(type(v) is int and v >= 0 for v in saved['identity'].values())):
                    previous = dict(check=saved['check'], identity=saved['identity'], at=now, binding=binding)
                    self._checks[key] = previous
                    increment(self, 'folder_durable_hits')
            positive = previous and previous['check']['ok']
            age = now - previous['at'] if previous else None
            usable = (previous is not None and binding is not None and age is not None and age >= 0
                      and (positive and (interval == 0 or age < interval)
                           or not positive and age < 60))
            if force or not usable:
                self._record_check(row, binding)
            else:
                increment(self, 'folder_snapshot_hits')
        current_keys = {self._signature(row) for row in state['rows']}
        self._checks = {key: value for key, value in self._checks.items() if key in current_keys}
        return state

    def require(self, kind, *, context=None):
        """Recheck current media paths immediately before real file work."""
        state = self.refresh(force=True, kind=kind, context=context)
        rows = [r for r in state['rows'] if r['active'] and r['kind'] == kind]
        if not rows:
            fail('folder_mapping_missing')
        for row in rows:
            check = self._checks[self._signature(row)]['check']
            if check['ok'] is not True:
                fail(check['code'])
        return True

    def _with_server(self, paths):
        result = _paths(paths)
        matches = [(root, _under(result['worker'], root['worker'])) for root in self.access_roots]
        matches = [(root, rel) for root, rel in matches if rel is not None]
        if len(matches) == 1 and matches[0][0].get('server'):
            root, rel = matches[0]
            server = str(clean_path(root['server']) / rel)
            if result.get('server') not in (None, server):
                fail('server_folder_mismatch')
            result['server'] = server
        elif result.get('server'):
            fail('server_folder_unverified')
        return result

    def _validate_state(self, state):
        if (not isinstance(state, dict) or set(state) != {'schema', 'revision', 'rows'}
                or state['schema'] != 1 or type(state['revision']) is not int or state['revision'] < 0
                or not isinstance(state['rows'], list) or len(state['rows']) > 100):
            fail('invalid_folder_store')
        ids, active = set(), set()
        for row in state['rows']:
            if (not isinstance(row, dict) or set(row) != {'id', 'kind', 'name', 'active', 'paths'}
                    or not isinstance(row['id'], str) or not ID.fullmatch(row['id']) or row['id'] in ids
                    or row['kind'] not in KINDS or type(row['active']) is not bool):
                fail('invalid_folder_store')
            ids.add(row['id']); _name(row['name']); _paths(row['paths'])
            if row['active']:
                if row['kind'] in active:
                    fail('duplicate_active_folder')
                active.add(row['kind'])

    def _directory(self, create=False):
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022 or info.st_uid != os.geteuid():
            fail('unsafe_folder_directory')

    @contextlib.contextmanager
    def _locked(self):
        self._directory(True)
        fd = os.open(self.root / '.folders.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
                fail('unsafe_folder_lock')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self):
        if not self.path.exists() and not self.path.is_symlink():
            if (self.root / '.folders.seeded').exists():
                fail('folder_store_unavailable')
            return copy.deepcopy(self.initial), None
        try:
            self._directory()
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid() or not 0 < info.st_size <= LIMIT:
                    fail('unsafe_folder_store')
                raw = handle.read(LIMIT + 1)
            state = json.loads(raw)
            self._validate_state(state)
            return state, raw
        except Exception:
            raise FolderManagerError('folder_store_unavailable') from None

    def _write(self, state, previous=None):
        raw = json.dumps(state, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
        if len(raw) > LIMIT:
            fail('folder_store_limit')
        if previous is not None:
            backup = self.root / ('folders.rev-%020d.json' % (state['revision'] - 1))
            try:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                if backup.is_symlink() or backup.read_bytes() != previous:
                    fail('folder_backup_conflict')
            else:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(previous); handle.flush(); os.fsync(handle.fileno())
        temporary = self.root / ('.folders-' + uuid.uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def seed(self):
        with self._locked():
            state, raw = self._read()
            if raw is None:
                self._write(state)
            fd = os.open(self.root / '.folders.seeded', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
                    fail('unsafe_folder_store')
                os.fsync(fd)
            finally: os.close(fd)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(directory)
            finally: os.close(directory)
        self.refresh()
        return self.public()

    def check(self, row):
        increment(self, 'folder_checks')
        paths = self._with_server(row['paths'])
        matches = [root for root in self.access_roots if _under(paths['worker'], root['worker']) is not None]
        if len(matches) != 1:
            fail('folder_not_in_allowed_mount')
        root = matches[0]
        if self.mount_checker(root['worker']) is not True:
            fail('folder_mount_unavailable')
        try:
            directory = Path(paths['worker'])
            # One descriptor walk proves every ancestor is a real directory.
            # resolve/is_dir/is_symlink would repeat the same media metadata I/O
            # and still require this race-resistant O_NOFOLLOW walk afterwards.
            fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for component in directory.parts[1:]:
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd); fd = child
                info = os.fstat(fd)
                if not stat.S_ISDIR(info.st_mode):
                    fail('folder_symlink_or_not_directory')
                identity = dict(device=info.st_dev, inode=info.st_ino)
            finally: os.close(fd)
        except FolderManagerError:
            raise
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise FolderManagerError('folder_symlink_or_not_directory') from None
            raise FolderManagerError('folder_unavailable') from None
        return dict(ok=True, code='folder_checked', paths=paths, media_changed=False,
                    identity=identity)

    def public(self):
        state, _ = self._read()
        checks = self._checks
        rows = []
        for row in state['rows']:
            result = copy.deepcopy(row)
            saved = checks.get(self._signature(row))
            result['check'] = copy.deepcopy(saved['check']) if saved else dict(
                ok=False, code='folder_check_pending')
            rows.append(result)
        return dict(revision=state['revision'], rows=rows, kinds=copy.deepcopy(KINDS),
                    endpoints=copy.deepcopy(ENDPOINTS), access_roots=copy.deepcopy(self.access_roots))

    def _effective(self, state, config):
        result = copy.deepcopy(config)
        rows = [copy.deepcopy(row) for row in state['rows'] if row['active']]
        result['folder_mappings'] = rows
        ebook = next((row for row in rows if row['kind'] == 'ebooks'), None)
        spec = result.get('jellyfin', {}).get('auto_ebooks')
        if isinstance(spec, dict):
            if ebook and all(k in ebook['paths'] for k in ('worker', 'bo', 'jf')):
                spec.update(local_root=ebook['paths']['worker'], bo_root=ebook['paths']['bo'], jf_root=ebook['paths']['jf'])
            else:
                spec['enabled'] = False
        podcast = next((row for row in rows if row['kind'] == 'podcasts'), None)
        if podcast or result.get('podcasts') is not None:
            settings = result.setdefault('podcasts', {})
            settings.pop('folder_error', None)
            for key in ('worker_root', 'abs_root', 'jellyfin_root'): settings.pop(key, None)
            if podcast and all(podcast['paths'].get(k) for k in ('worker', 'abs', 'jf')):
                settings.update(worker_root=podcast['paths']['worker'], abs_root=podcast['paths']['abs'],
                                jellyfin_root=podcast['paths']['jf'])
            elif settings.get('enabled'):
                settings['folder_error'] = 'podcast_folder_missing'
        return result

    def effective(self, config=None, *, context=None):
        state = self.refresh(context=context)
        podcast_error = None
        for row in state['rows']:
            if row['active']:
                saved = self._checks.get(self._signature(row))
                check = saved['check'] if saved else dict(ok=False, code='folder_check_pending')
                if not check['ok']:
                    if row['kind'] == 'podcasts': podcast_error = check['code']
                    else: fail(check['code'])
        result = self._effective(state, self.base_config if config is None else config)
        if podcast_error:
            spec = result.setdefault('podcasts', {})
            spec['folder_error'] = podcast_error
            for key in ('worker_root', 'abs_root', 'jellyfin_root'): spec.pop(key, None)
        return result

    def mutate(self, action, payload, compatibility_callback=None):
        revision = None
        try:
            with self._locked():
                old, raw = self._read(); revision = old['revision']
                if raw is None:
                    fail('folders_not_initialized')
                if not isinstance(payload, dict) or type(payload.get('expected_revision')) is not int or payload['expected_revision'] != revision:
                    fail('stale_folder_revision')
                if action not in {'create', 'update', 'test', 'activate', 'deactivate', 'delete'}:
                    fail('invalid_folder_action')
                allowed = {'expected_revision', 'kind', 'name', 'paths'} if action == 'create' else {'expected_revision', 'id'}
                if action == 'update': allowed |= {'name', 'paths'}
                if set(payload) - allowed: fail('invalid_folder_field')
                state = copy.deepcopy(old)
                if action == 'create':
                    kind = payload.get('kind')
                    if kind not in KINDS or len(state['rows']) >= 100: fail('invalid_folder_kind')
                    row = dict(id=kind + '-' + uuid.uuid4().hex, kind=kind, name=_name(payload.get('name')),
                               paths=self._with_server(payload.get('paths')), active=False)
                    state['rows'].append(row)
                else:
                    row = next((r for r in state['rows'] if r['id'] == payload.get('id')), None)
                    if row is None: fail('folder_not_found')
                if action == 'update':
                    if 'name' in payload: row['name'] = _name(payload['name'])
                    if 'paths' in payload:
                        if not isinstance(payload['paths'], dict): fail('invalid_folder_paths')
                        changed = dict(row['paths']); changed.pop('server', None)
                        changed.update(payload['paths']); row['paths'] = self._with_server(changed)
                if action in {'test', 'activate'} or action == 'update' and row['active']:
                    mount = self._mount_scope(row['kind'], [row])
                    binding = self._local_binding(row, mount) if mount is not None else None
                    try:
                        proof = self.check(row)
                    except FolderManagerError as exc:
                        self._bump_epoch(row['kind'])
                        if binding is not None:
                            binding = dict(binding, epoch=self._error_epochs[row['kind']])
                        self.observations.forget('folders', row['id'])
                        self._checks[self._signature(row)] = dict(
                            check=dict(ok=False, code=str(exc)), at=self.clock(), binding=binding)
                        raise
                    self._checks[self._signature(row)] = dict(
                        check=dict(ok=True, code=proof['code']), at=self.clock(),
                        binding=binding, identity=proof['identity'])
                    if binding is not None:
                        self.observations.put('folders', row['id'], binding,
                            dict(check=dict(ok=True, code=proof['code']), identity=proof['identity']))
                    if action == 'test':
                        return dict(ok=True, code='folder_checked', revision=revision, folder_id=row['id'], check=proof)
                    if action == 'activate':
                        for candidate in state['rows']:
                            if candidate['kind'] == row['kind']: candidate['active'] = candidate['id'] == row['id']
                    try:
                        compatible = callable(compatibility_callback) and compatibility_callback(
                            self._effective(old, self.base_config), self._effective(state, self.base_config)) is True
                    except Exception: compatible = False
                    if not compatible: fail('incompatible_folder_mapping')
                elif action in {'deactivate', 'delete'}:
                    if action == 'delete': state['rows'].remove(row)
                    else: row['active'] = False
                    if row['active'] or action == 'deactivate':
                        try:
                            compatible = callable(compatibility_callback) and compatibility_callback(
                                self._effective(old, self.base_config), self._effective(state, self.base_config)) is True
                        except Exception: compatible = False
                        if not compatible: fail('incompatible_folder_mapping')
                self._validate_state(state)
                state['revision'] += 1
                self._write(state, raw)
                # Row keys isolate changes; unrelated media histories stay valid.
                self._state_signature = None
                return dict(ok=True, code='folder_saved', revision=state['revision'], folder_id=row['id'])
        except FolderManagerError as exc:
            return dict(ok=False, code=str(exc), revision=revision)
        except Exception:
            return dict(ok=False, code='folder_change_failed', revision=revision)
