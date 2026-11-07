"""Deployment gate, run by cron. Importing this module performs no I/O.

BookBridge may retain one reviewed mapping during paused preparation or after
handoff to a pinned native readalong with its progress writers disabled. The
default still permits no BookBridge mappings. An explicit detached phase checks
ABS/BookOrbit and all native EPUB pins without querying BookBridge at all.
Native-mode configuration checks
return SQL-derived booleans only; credential values never leave the database.
"""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from pathlib import PurePosixPath


ROW_FIELDS = frozenset({'abs_id', 'audio_source', 'audio_source_id',
                        'audio_provider_book_id', 'ebook_source',
                        'ebook_source_id', 'user_id', 'sync_mode'})
POLICY_FIELDS = frozenset({'phase', 'image', 'paused_abs_id', 'mapping'})
NATIVE_POLICY_FIELDS = frozenset({'phase', 'image', 'abs_id', 'mapping'})
WRITER_CHECK_FIELDS = frozenset({'global_abs_disabled', 'global_bookorbit_disabled',
                               'personal_abs_disabled', 'personal_bookorbit_disabled',
                               'global_bookorbit_kosync_absent', 'personal_bookorbit_kosync_absent'})
SYNC_MODES = frozenset({'audiobook', 'audiobook_only', 'ebook_only'})
IMAGE_PATTERN = re.compile(r'sha256:[0-9a-f]{64}')
RECEIPT_MAX_AGE = 150


class DeploymentError(RuntimeError):
    """Fixed, safe diagnostic text; never raw process output or config values."""


def _positive_id(value):
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*', value):
        return value
    raise DeploymentError('BookBridge identity has invalid type or format')


def normalize_bookbridge_row(row):
    """Normalize only decimal int/string IDs; compare all other values exactly."""
    if not isinstance(row, dict) or set(row) != ROW_FIELDS:
        raise DeploymentError('BookBridge mapping schema changed')
    result = dict(row)
    for field in ('audio_source_id', 'audio_provider_book_id', 'ebook_source_id', 'user_id'):
        result[field] = _positive_id(row[field])
    if (row['audio_source'] != 'BookOrbit' or row['ebook_source'] != 'BookOrbit'
            or not isinstance(row['abs_id'], str)
            or row['abs_id'] != 'bookorbit:' + result['audio_source_id']
            or result['audio_provider_book_id'] != result['audio_source_id']
            or not isinstance(row['sync_mode'], str) or row['sync_mode'] not in SYNC_MODES):
        raise DeploymentError('BookBridge mapping identity or mode is not supported')
    return result


def preparation_policy(config):
    """Validate the opt-in preparation exception and its corresponding pause."""
    if not isinstance(config, dict):
        raise DeploymentError('Invalid deployment configuration')
    if 'bookbridge_policy' not in config:
        return None
    policy = config['bookbridge_policy']
    if not isinstance(policy, dict) or set(policy) != POLICY_FIELDS:
        raise DeploymentError('Invalid BookBridge policy schema')
    if policy['phase'] != 'preparation':
        raise DeploymentError('Unsupported BookBridge policy phase')
    if not isinstance(policy['image'], str) or not IMAGE_PATTERN.fullmatch(policy['image']):
        raise DeploymentError('BookBridge image is not pinned')
    expected = normalize_bookbridge_row(policy['mapping'])
    abs_id = policy['paused_abs_id']
    pauses = config.get('paused_mappings')
    if (not isinstance(abs_id, str) or not abs_id or abs_id != abs_id.strip()
            or not abs_id.isprintable() or not isinstance(pauses, dict)):
        raise DeploymentError('BookBridge preparation requires a valid ABS pause')
    reason = pauses.get(abs_id)
    if (not isinstance(reason, str) or not reason.strip() or len(reason) > 500
            or not reason.isprintable()):
        raise DeploymentError('BookBridge preparation requires the corresponding ABS pause')
    mappings = config.get('mappings')
    if not isinstance(mappings, list) or any(not isinstance(m, dict) for m in mappings):
        raise DeploymentError('Invalid audio mapping configuration')
    matching = [m for m in mappings if m.get('abs_id') == abs_id]
    if len(matching) != 1 or _positive_id(matching[0].get('bo_id')) != expected['audio_source_id']:
        raise DeploymentError('BookBridge preparation pause belongs to another audio pair')
    return dict(policy, mapping=expected)


def bookbridge_policy(config):
    """Recognize reviewed phases; detachment is explicit and keeps EPUB pins."""
    if not isinstance(config, dict):
        raise DeploymentError('Invalid deployment configuration')
    if 'bookbridge_policy' not in config:
        return None
    policy = config['bookbridge_policy']
    if not isinstance(policy, dict):
        raise DeploymentError('Invalid BookBridge policy schema')
    if policy.get('phase') == 'detached':
        if set(policy) != {'phase'}:
            raise DeploymentError('Detached policy must not retain BookBridge settings')
        mappings = config.get('mappings')
        pins = config.get('native_readalong')
        if (not isinstance(mappings, list) or not mappings
                or any(not isinstance(m, dict) for m in mappings)
                or not isinstance(pins, dict)):
            raise DeploymentError('Detached policy requires audio mappings and native EPUB pins')
        from mapping import validate_native_readalong
        for abs_id, pin in pins.items():
            if (not isinstance(abs_id, str) or not abs_id or abs_id != abs_id.strip()
                    or not abs_id.isprintable()):
                raise DeploymentError('Invalid detached native ABS identity')
            matching = [m for m in mappings if m.get('abs_id') == abs_id]
            if len(matching) != 1:
                raise DeploymentError('Detached native EPUB has no unique audio mapping')
            try:
                validate_native_readalong(pin, matching[0].get('bo_id'), matching[0].get('folder'), folder_config=config)
            except (ValueError, TypeError):
                raise DeploymentError('Invalid detached native EPUB pin') from None
        return dict(policy)
    if policy.get('phase') == 'preparation':
        return preparation_policy(config)
    if policy.get('phase') != 'native_readalong':
        raise DeploymentError('Unsupported BookBridge policy phase')
    if set(policy) != NATIVE_POLICY_FIELDS:
        raise DeploymentError('Invalid native BookBridge policy schema')
    if not isinstance(policy['image'], str) or not IMAGE_PATTERN.fullmatch(policy['image']):
        raise DeploymentError('BookBridge image is not pinned')
    expected = normalize_bookbridge_row(policy['mapping'])
    abs_id = policy['abs_id']
    if (not isinstance(abs_id, str) or not abs_id or abs_id != abs_id.strip()
            or not abs_id.isprintable()):
        raise DeploymentError('Invalid native BookBridge ABS identity')
    mappings = config.get('mappings')
    if not isinstance(mappings, list) or any(not isinstance(m, dict) for m in mappings):
        raise DeploymentError('Invalid audio mapping configuration')
    matching = [m for m in mappings if m.get('abs_id') == abs_id]
    if len(matching) != 1 or _positive_id(matching[0].get('bo_id')) != expected['audio_source_id']:
        raise DeploymentError('Native BookBridge policy belongs to another audio pair')
    pins = config.get('native_readalong')
    if not isinstance(pins, dict) or abs_id not in pins:
        raise DeploymentError('Native BookBridge policy requires the corresponding EPUB pin')
    # Share the coordinator's pure pin validator, including exact schema, integer
    # file identity, positive size, and an EPUB path inside this book's folder.
    from mapping import validate_native_readalong
    try:
        validate_native_readalong(pins[abs_id], matching[0].get('bo_id'), matching[0].get('folder'), folder_config=config)
    except (ValueError, TypeError):
        raise DeploymentError('Invalid corresponding native EPUB pin') from None
    return dict(policy, mapping=expected)


def validate_containers(containers, config):
    """Check complete Docker evidence; do not accept absent/duplicate entries."""
    policy = bookbridge_policy(config)
    linked = policy is not None and policy['phase'] != 'detached'
    expected_names = {'audiobookshelf', 'bookorbit'} | ({'bookbridge'} if linked else set())
    if not isinstance(containers, list) or len(containers) != len(expected_names):
        raise DeploymentError('Incomplete Docker deployment snapshot')
    expected = config.get('deployment')
    if not isinstance(expected, dict):
        raise DeploymentError('Invalid Docker deployment configuration')
    seen = set()
    for container in containers:
        if not isinstance(container, dict) or not isinstance(container.get('Name'), str):
            raise DeploymentError('Invalid Docker deployment snapshot')
        name = container['Name'].removeprefix('/')
        if name not in expected_names or name in seen:
            raise DeploymentError('Unexpected or duplicate Docker container')
        seen.add(name)
        spec = policy if name == 'bookbridge' else expected.get(name)
        if not isinstance(spec, dict) or not isinstance(spec.get('image'), str):
            raise DeploymentError('Missing pinned Docker image')
        state = container.get('State')
        if (container.get('Image') != spec['image'] or not isinstance(state, dict)
                or state.get('Status') != 'running'):
            raise DeploymentError('Container image/state changed: ' + name)
        if name == 'bookbridge':
            if policy['phase'] == 'native_readalong':
                env = container.get('Config', {}).get('Env') if isinstance(container.get('Config'), dict) else None
                if not isinstance(env, list) or any(not isinstance(value, str) for value in env):
                    raise DeploymentError('Invalid BookBridge environment snapshot')
                # Missing database settings may fall back to container variables.
                # Examine only presence; never include a value in diagnostics.
                for value in env:
                    key, separator, content = value.partition('=')
                    if key in {'BOOKORBIT_KOSYNC_USER', 'BOOKORBIT_KOSYNC_KEY'} and (not separator or content):
                        raise DeploymentError('BookBridge annotation credentials remain in the environment')
            continue
        mounts = container.get('Mounts')
        if not isinstance(mounts, list) or any(not isinstance(m, dict) for m in mounts):
            raise DeploymentError('Invalid media mount snapshot: ' + name)
        media = [m for m in mounts if m.get('Destination') == '/audiobooks']
        if (len(media) != 1 or not isinstance(spec.get('media_source'), str)
                or media[0].get('Source') != spec['media_source']):
            raise DeploymentError('Media mount changed: ' + name)
        if name == 'bookorbit':
            env = container.get('Config', {}).get('Env') if isinstance(container.get('Config'), dict) else None
            if not isinstance(env, list) or any(not isinstance(v, str) for v in env):
                raise DeploymentError('Invalid BookOrbit version snapshot')
            versions = [v.split('=', 1)[1] for v in env if v.startswith('APP_VERSION=')]
            if len(versions) != 1 or versions[0] != spec.get('version'):
                raise DeploymentError('BookOrbit version changed')


def validate_bookbridge_snapshot(snapshot, config):
    """Native handoff requires the retained row and all writer checks to pass."""
    policy = bookbridge_policy(config)
    if policy is not None and policy['phase'] == 'detached':
        raise DeploymentError('Detached policy must not query BookBridge')
    native = policy is not None and policy['phase'] == 'native_readalong'
    fields = {'count', 'rows', 'writer_checks'} if native else {'count', 'rows'}
    if (not isinstance(snapshot, dict) or set(snapshot) != fields
            or not isinstance(snapshot['count'], int) or isinstance(snapshot['count'], bool)
            or snapshot['count'] < 0 or not isinstance(snapshot['rows'], list)):
        raise DeploymentError('Invalid BookBridge database snapshot')
    count, rows = snapshot['count'], snapshot['rows']
    if policy is None:
        if count != 0 or rows:
            raise DeploymentError('BookBridge mappings require coordinated review')
        return
    if count > 1 or len(rows) != count:
        raise DeploymentError('BookBridge has unexpected mappings')
    if count == 1 and normalize_bookbridge_row(rows[0]) != policy['mapping']:
        raise DeploymentError('BookBridge mapping differs from reviewed preparation')
    if native:
        if count != 1:
            raise DeploymentError('Native BookBridge handoff requires the retained mapping')
        checks = snapshot['writer_checks']
        if (not isinstance(checks, dict) or set(checks) != WRITER_CHECK_FIELDS
                or any(value is not True for value in checks.values())):
            raise DeploymentError('BookBridge writers are not verifiably disabled')


def snapshot_command(with_mapping, native_user_id=None):
    """Read mapping columns and, in native mode, SQL-derived safety booleans."""
    fields = ','.join(sorted(ROW_FIELDS))
    # LIMIT 2 is enough to detect an unauthorized second mapping. Count and rows
    # share one SQLite read snapshot. Preparation never reads credential tables.
    script = (
        "import json,sqlite3; "
        "c=sqlite3.connect('file:/data/database.db?mode=ro',uri=True); "
        "c.row_factory=sqlite3.Row; c.execute('PRAGMA query_only=ON'); c.execute('BEGIN'); "
        "n=c.execute('SELECT COUNT(*) FROM books').fetchone()[0]; "
        + ("rows=[dict(r) for r in c.execute('SELECT " + fields + " FROM books LIMIT 2')]; "
           if with_mapping else "rows=[]; ")
        + "snapshot={'count':n,'rows':rows}; "
    )
    if native_user_id is not None:
        if not with_mapping:
            raise DeploymentError('Native snapshot requires mapping evidence')
        user_id = int(_positive_id(native_user_id))
        # Encryption is deliberately not opened. Presence of any nonempty
        # stored KOReader credential, including ciphertext, fails closed. Check
        # every user's KOReader credentials because the annotation daemon does.
        disabled = "SELECT COUNT(*)=1 AND COALESCE(SUM(CASE WHEN LOWER(TRIM(value))='false' THEN 1 ELSE 0 END),0)=1 FROM {} WHERE key=?{}"
        absent = "SELECT COUNT(*)=0 FROM {} WHERE key IN ('BOOKORBIT_KOSYNC_USER','BOOKORBIT_KOSYNC_KEY') AND value IS NOT NULL AND value<>''"
        script += "snapshot['writer_checks']={}; "
        for prefix, table, suffix in (('global', 'settings', ''),
                                      ('personal', 'user_credentials', ' AND user_id=?')):
            for name, key in (('abs', 'ABS_ENABLED'), ('bookorbit', 'BOOKORBIT_ENABLED')):
                query = disabled.format(table, suffix)
                params = (key,) if prefix == 'global' else (key, user_id)
                label = prefix + '_' + name + '_disabled'
                script += f"snapshot['writer_checks'][{label!r}]=bool(c.execute({query!r},{params!r}).fetchone()[0]); "
            label = prefix + '_bookorbit_kosync_absent'
            script += f"snapshot['writer_checks'][{label!r}]=bool(c.execute({absent.format(table)!r}).fetchone()[0]); "
    script += "print(json.dumps(snapshot)); c.close()"
    return ['docker', 'exec', 'bookbridge', 'python', '-c', script]


def validate_ebook_mounts(containers, config):
    spec = config.get('jellyfin', {}).get('auto_ebooks')
    if not spec or spec.get('enabled') is not True:
        return
    if len(containers) != 2 or {x.get('Name') for x in containers} != {'/bookorbit', '/jellyfin'}:
        raise DeploymentError('Incomplete ebook mount snapshot')
    for c in containers:
        name = c['Name'][1:]
        root = PurePosixPath(spec['bo_root'] if name == 'bookorbit' else spec['jf_root'])
        image = config['deployment']['bookorbit']['image'] if name == 'bookorbit' else spec['jf_image']
        if c.get('Image') != image or c.get('State', {}).get('Status') != 'running':
            raise DeploymentError('Ebook container image/state changed: ' + name)
        mounts = c.get('Mounts', [])
        parents = [m for m in mounts if root.is_relative_to(PurePosixPath(m['Destination']))]
        if not parents:
            raise DeploymentError('Ebook root has no verified mount: ' + name)
        m = max(parents, key=lambda m:len(PurePosixPath(m['Destination']).parts))
        source = PurePosixPath(m['Source']) / root.relative_to(m['Destination'])
        if str(source) != spec['shared_host_root'] or any(PurePosixPath(x['Destination']).is_relative_to(root) and PurePosixPath(x['Destination']) != root for x in mounts):
            raise DeploymentError('Shared ebook mount changed: ' + name)


def check_deployment(config, runner=subprocess.run):
    policy = bookbridge_policy(config)
    detached = policy is not None and policy['phase'] == 'detached'
    names = ['audiobookshelf', 'bookorbit'] + (['bookbridge'] if policy and not detached else [])
    result = runner(['docker', 'inspect', *names], capture_output=True, text=True, timeout=20, check=True)
    validate_containers(json.loads(result.stdout), config)
    if config.get('jellyfin', {}).get('auto_ebooks', {}).get('enabled') is True:
        result = runner(['docker', 'inspect', 'bookorbit', 'jellyfin'], capture_output=True, text=True, timeout=20, check=True)
        validate_ebook_mounts(json.loads(result.stdout), config)
    if detached:
        return
    user_id = policy['mapping']['user_id'] if policy and policy['phase'] == 'native_readalong' else None
    result = runner(snapshot_command(policy is not None, user_id), capture_output=True, text=True, timeout=20, check=True)
    validate_bookbridge_snapshot(json.loads(result.stdout), config)



BB_REQUIRED_CONTAINERS = frozenset({'audiobookshelf', 'book-progress-sync', 'jellyfin'})
BB_OPTIONAL_CONTAINERS = frozenset({'bookbridge'})


def _canonical_absolute_path(value):
    if (not isinstance(value, str) or not value.startswith('/') or value == '/'
            or not value.isprintable() or '\\' in value
            or any(piece in {'', '.', '..'} for piece in value.split('/')[1:])
            or str(PurePosixPath(value)) != value):
        raise DeploymentError('Invalid BookBridge media root configuration')
    return PurePosixPath(value)


def bookbridge_deployment_policy(config):
    """Independent, explicit Docker trust for the optional BookBridge provider.

    This is host-managed deployment metadata, not GUI connection state. Nothing
    is inferred from the old BookOrbit deployment, an untrusted API, or an image
    tag. It remains usable when BookOrbit is absent.
    """
    spec = config.get('bookbridge_deployment') if isinstance(config, dict) else None
    if spec is None:
        raise DeploymentError('BookBridge deployment is not configured')
    if (not isinstance(spec, dict) or not BB_REQUIRED_CONTAINERS <= set(spec)
            or set(spec) - BB_REQUIRED_CONTAINERS - BB_OPTIONAL_CONTAINERS):
        raise DeploymentError('Invalid BookBridge deployment policy')
    host_roots = set()
    for name, entry in spec.items():
        fields = {'image'} if name == 'bookbridge' else {'image', 'audio_root', 'media_source'}
        if (not isinstance(entry, dict) or set(entry) != fields
                or not isinstance(entry.get('image'), str)
                or IMAGE_PATTERN.fullmatch(entry['image']) is None):
            raise DeploymentError('Invalid pinned BookBridge deployment image')
        if name != 'bookbridge':
            root = _canonical_absolute_path(entry['audio_root'])
            expected_root = '/data/audiobooks' if name == 'jellyfin' else '/audiobooks'
            if str(root) != expected_root:
                raise DeploymentError('Unsupported BookBridge audio mount destination')
            host_roots.add(str(_canonical_absolute_path(entry['media_source'])))
    if len(host_roots) != 1:
        raise DeploymentError('BookBridge audio roots do not share one host directory')
    return spec


def validate_bookbridge_containers(containers, config):
    """Pin ABS/worker/Jellyfin independently of BookOrbit and its EPUB pins."""
    spec = bookbridge_deployment_policy(config)
    if not isinstance(containers, list) or len(containers) != len(spec):
        raise DeploymentError('Incomplete BookBridge Docker deployment snapshot')
    seen = set()
    for container in containers:
        if not isinstance(container, dict) or not isinstance(container.get('Name'), str):
            raise DeploymentError('Invalid BookBridge Docker snapshot')
        name = container['Name'].removeprefix('/')
        if name not in spec or name in seen:
            raise DeploymentError('Unexpected or duplicate BookBridge Docker container')
        seen.add(name)
        entry = spec[name]
        state = container.get('State')
        if (container.get('Image') != entry['image'] or not isinstance(state, dict)
                or state.get('Status') != 'running'):
            raise DeploymentError('BookBridge container image/state changed: ' + name)
        if name == 'bookbridge':
            continue
        mounts = container.get('Mounts')
        if not isinstance(mounts, list) or any(not isinstance(m, dict) for m in mounts):
            raise DeploymentError('Invalid BookBridge audio mounts: ' + name)
        root = PurePosixPath(entry['audio_root'])
        destinations = set()
        parents = []
        for mount in mounts:
            destination = mount.get('Destination')
            # Unrelated mounts still must have well-formed destinations. Root
            # mounts are refused: they cannot be a narrowly reviewed media bind.
            dest = _canonical_absolute_path(destination)
            if str(dest) in destinations:
                raise DeploymentError('Duplicate BookBridge mount destination: ' + name)
            destinations.add(str(dest))
            if dest != root and dest.is_relative_to(root):
                raise DeploymentError('Nested BookBridge audio mount: ' + name)
            if root.is_relative_to(dest):
                parents.append((dest, mount))
        if not parents:
            raise DeploymentError('BookBridge audio root has no verified bind: ' + name)
        destination, mount = max(parents, key=lambda row: len(row[0].parts))
        if mount.get('Type') != 'bind':
            raise DeploymentError('BookBridge audio root is not a host bind: ' + name)
        source = _canonical_absolute_path(mount.get('Source')) / root.relative_to(destination)
        if str(source) != entry['media_source']:
            raise DeploymentError('BookBridge shared audio source changed: ' + name)
        if type(mount.get('RW')) is not bool:
            raise DeploymentError('BookBridge audio mount access is unknown: ' + name)
        if name == 'book-progress-sync' and mount['RW']:
            raise DeploymentError('BookBridge worker media must be read-only')
    if seen != set(spec):
        raise DeploymentError('Incomplete BookBridge Docker deployment snapshot')


def check_bookbridge_deployment(config, runner=subprocess.run):
    spec = bookbridge_deployment_policy(config)
    names = sorted(spec)
    result = runner(['docker', 'inspect', *names], capture_output=True, text=True,
                    timeout=20, check=True)
    validate_bookbridge_containers(json.loads(result.stdout), config)


FOLDER_ENDPOINTS = frozenset({'abs', 'bo', 'jf', 'bb', 'worker'})
CONTAINER_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')


PODCAST_ENDPOINTS = frozenset({'abs', 'jf', 'worker'})


def podcast_deployment_policy(config):
    """A host-owned, provider-independent trust policy for downloaded episodes."""
    spec = config.get('podcast_deployment') if isinstance(config, dict) else None
    if spec is None:
        raise DeploymentError('Podcast deployment is not configured')
    if not isinstance(spec, dict) or not PODCAST_ENDPOINTS <= set(spec) or set(spec) - PODCAST_ENDPOINTS - {'pinepods'}:
        raise DeploymentError('Invalid podcast deployment policy')
    sources, names = set(), set()
    media_spec = {role: spec[role] for role in PODCAST_ENDPOINTS}
    for endpoint, entry in media_spec.items():
        if (not isinstance(entry, dict) or set(entry) != {'container', 'image', 'podcast_root', 'media_source'}
                or not isinstance(entry.get('container'), str) or not CONTAINER_NAME.fullmatch(entry['container'])
                or not isinstance(entry.get('image'), str) or not IMAGE_PATTERN.fullmatch(entry['image'])
                or entry['container'] in names):
            raise DeploymentError('Invalid pinned podcast container')
        names.add(entry['container'])
        _canonical_absolute_path(entry['podcast_root'])
        sources.add(str(_canonical_absolute_path(entry['media_source'])))
    if len(sources) != 1:
        raise DeploymentError('Podcast applications do not share one host directory')
    return media_spec


def check_pinepods_deployment(config, runner=subprocess.run):
    spec = config.get('podcast_deployment', {}).get('pinepods')
    if (not isinstance(spec, dict) or set(spec) != {'container', 'image'}
            or not isinstance(spec.get('container'), str) or not CONTAINER_NAME.fullmatch(spec['container'])
            or not isinstance(spec.get('image'), str) or not IMAGE_PATTERN.fullmatch(spec['image'])):
        raise DeploymentError('PinePods image is not pinned')
    result = runner(['docker', 'inspect', spec['container']], capture_output=True, text=True, timeout=20, check=True)
    rows = json.loads(result.stdout)
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
            or rows[0].get('Name') != '/' + spec['container'] or rows[0].get('Image') != spec['image']
            or not isinstance(rows[0].get('State'), dict) or rows[0]['State'].get('Status') != 'running'):
        raise DeploymentError('PinePods container image/state changed')
    return True


def validate_podcast_containers(containers, config):
    """Only ABS may write media; Jellyfin and the coordinator are read-only."""
    spec = podcast_deployment_policy(config)
    names = {entry['container']: endpoint for endpoint, entry in spec.items()}
    if not isinstance(containers, list) or len(containers) != len(spec):
        raise DeploymentError('Incomplete podcast Docker snapshot')
    seen, receipts = set(), {}
    for container in containers:
        if not isinstance(container, dict) or not isinstance(container.get('Name'), str):
            raise DeploymentError('Invalid podcast Docker snapshot')
        name = container['Name'].removeprefix('/')
        if name not in names or name in seen:
            raise DeploymentError('Unexpected or duplicate podcast container')
        seen.add(name); endpoint = names[name]; entry = spec[endpoint]
        if (container.get('Image') != entry['image'] or not isinstance(container.get('State'), dict)
                or container['State'].get('Status') != 'running'):
            raise DeploymentError('Podcast container image/state changed')
        mounts = container.get('Mounts')
        if not isinstance(mounts, list) or any(not isinstance(m, dict) for m in mounts):
            raise DeploymentError('Invalid podcast mount snapshot')
        destinations = [_canonical_absolute_path(m.get('Destination')) for m in mounts]
        if len(destinations) != len(set(destinations)):
            raise DeploymentError('Duplicate podcast mount destination')
        root = _canonical_absolute_path(entry['podcast_root'])
        if any(destination != root and destination.is_relative_to(root) for destination in destinations):
            raise DeploymentError('Nested mount shadows the podcast folder')
        parents = [(destination, mount) for destination, mount in zip(destinations, mounts) if root.is_relative_to(destination)]
        if not parents:
            raise DeploymentError('Podcast folder has no verified media bind')
        destination, mount = max(parents, key=lambda pair: len(pair[0].parts))
        if mount.get('Type') != 'bind':
            raise DeploymentError('Podcast folder must use a host bind')
        source = _canonical_absolute_path(mount.get('Source')) / root.relative_to(destination)
        if str(source) != entry['media_source']:
            raise DeploymentError('Shared podcast source changed')
        if type(mount.get('RW')) is not bool:
            raise DeploymentError('Podcast mount access is unknown')
        if endpoint in {'jf', 'worker'} and mount['RW']:
            raise DeploymentError('Jellyfin and worker podcast media must be read-only')
        receipts[endpoint] = dict(source=str(source), destination=str(root), rw=mount['RW'])
    if seen != set(names):
        raise DeploymentError('Incomplete podcast Docker snapshot')
    return receipts


def check_podcast_deployment(config, runner=subprocess.run):
    spec = podcast_deployment_policy(config)
    result = runner(['docker', 'inspect', *sorted(row['container'] for row in spec.values())],
                    capture_output=True, text=True, timeout=20, check=True)
    return validate_podcast_containers(json.loads(result.stdout), config)


def validate_podcast_aliases(config, gate, now=None):
    """Resolve GUI-selected subfolders only inside the freshly pinned mounts."""
    current = time.time() if now is None else now
    checked = gate.get('checked_at') if isinstance(gate, dict) else None
    if (not isinstance(gate, dict) or gate.get('podcasts_ok') is not True
            or type(checked) not in (int, float) or not math.isfinite(checked)
            or type(current) not in (int, float) or not math.isfinite(current)
            or not 0 <= current - checked <= RECEIPT_MAX_AGE):
        raise DeploymentError('Fresh podcast deployment verification is unavailable')
    receipts, spec = gate.get('podcast_mounts'), config.get('podcasts')
    if not isinstance(receipts, dict) or set(receipts) != PODCAST_ENDPOINTS or not isinstance(spec, dict):
        raise DeploymentError('Podcast mount receipt is incomplete')
    if spec.get('folder_error'):
        raise DeploymentError('Active podcast media folder is unavailable')
    wanted = {'worker': spec.get('worker_root'), 'abs': spec.get('abs_root'), 'jf': spec.get('jellyfin_root')}
    resolved = {}
    for role, value in wanted.items():
        selected = _canonical_absolute_path(value)
        row = receipts[role]
        if (not isinstance(row, dict) or set(row) != {'source', 'destination', 'rw'}
                or type(row['rw']) is not bool or role in {'worker', 'jf'} and row['rw']):
            raise DeploymentError('Invalid trusted podcast mount receipt')
        source, destination = _canonical_absolute_path(row['source']), _canonical_absolute_path(row['destination'])
        if not selected.is_relative_to(destination):
            raise DeploymentError('Selected podcast folder is outside the verified mount')
        resolved[role] = str(source / selected.relative_to(destination))
    if len(set(resolved.values())) != 1:
        raise DeploymentError('Selected podcast folders do not resolve to the same media')
    if config.get('folder_mappings') is not None:
        from folder_manager import active_folders
        rows = [row for row in active_folders(config) if row.get('kind') == 'podcasts']
        if len(rows) != 1:
            raise DeploymentError('An active podcast folder profile is required')
        paths = rows[0].get('paths', {})
        if (not isinstance(paths, dict) or paths.get('server') != resolved['worker']
                or any(paths.get(role) != selected for role, selected in wanted.items())):
            raise DeploymentError('Podcast folder aliases differ from the selected profile')
    return True


def _book_folder_config(config):
    """Podcast mount failures belong to their independent receipt, not books."""
    roots = config.get('folder_access_roots')
    if not isinstance(roots, list): return config
    podcast = config.get('podcasts', {})
    deployment = config.get('podcast_deployment', {})
    worker = deployment.get('worker', {}) if isinstance(deployment, dict) else {}
    podcast_roots = {value for value in (podcast.get('worker_root') if isinstance(podcast, dict) else None,
                     worker.get('podcast_root') if isinstance(worker, dict) else None) if isinstance(value, str)}
    if not podcast_roots: return config
    return dict(config, folder_access_roots=[row for row in roots if not isinstance(row, dict) or row.get('worker') not in podcast_roots])


def folder_deployment_policy(config):
    specs = config.get('folder_deployment') if isinstance(config, dict) else None
    if specs is None:
        raise DeploymentError('Folder deployment is not configured')
    roots = config.get('folder_access_roots')
    if (not isinstance(specs, dict) or not specs or set(specs) - FOLDER_ENDPOINTS
            or not isinstance(roots, list) or not roots or len(roots) > 64):
        raise DeploymentError('Invalid folder deployment policy')
    if any(not isinstance(row, dict) or set(row) != {'worker', 'server'} for row in roots):
        raise DeploymentError('Invalid trusted folder access roots')
    for row in roots:
        _canonical_absolute_path(row['worker'])
    allowed = [_canonical_absolute_path(row['server']) for row in roots]
    if len(set(allowed)) != len(allowed):
        raise DeploymentError('Duplicate allowed media root')
    if any(a != b and a.is_relative_to(b) for a in allowed for b in allowed):
        raise DeploymentError('Overlapping allowed media roots')
    names = set()
    for entry in specs.values():
        if (not isinstance(entry, dict) or set(entry) != {'container', 'image'}
                or not isinstance(entry.get('container'), str)
                or not CONTAINER_NAME.fullmatch(entry['container'])
                or not isinstance(entry.get('image'), str)
                or not IMAGE_PATTERN.fullmatch(entry['image'])
                or entry['container'] in names):
            raise DeploymentError('Invalid pinned folder container')
        names.add(entry['container'])
    return specs, allowed


def _validate_folder_containers_strict(containers, config):
    """Return only trusted media aliases, never config/credential mount paths.

    A broad host bind is projected down to each allowed media root. Any nested
    mount under that projection invalidates the receipt instead of disguising a
    different directory as the same library. Docker access remains host-only.
    """
    specs, allowed = folder_deployment_policy(config)
    names = {entry['container']: endpoint for endpoint, entry in specs.items()}
    if not isinstance(containers, list) or len(containers) != len(specs):
        raise DeploymentError('Incomplete folder Docker snapshot')
    receipts, seen = {}, set()
    for container in containers:
        if not isinstance(container, dict) or not isinstance(container.get('Name'), str):
            raise DeploymentError('Invalid folder Docker snapshot')
        name = container['Name'].removeprefix('/')
        if name not in names or name in seen:
            raise DeploymentError('Unexpected or duplicate folder container')
        seen.add(name)
        endpoint = names[name]
        if (container.get('Image') != specs[endpoint]['image']
                or not isinstance(container.get('State'), dict)
                or container['State'].get('Status') != 'running'):
            raise DeploymentError('Folder container image/state changed')
        mounts = container.get('Mounts')
        if not isinstance(mounts, list) or any(not isinstance(m, dict) for m in mounts):
            raise DeploymentError('Invalid folder mount snapshot')
        destinations = []
        for mount in mounts:
            destinations.append(_canonical_absolute_path(mount.get('Destination')))
        if len(set(destinations)) != len(destinations):
            raise DeploymentError('Duplicate folder mount destination')
        aliases = []
        for index, mount in enumerate(mounts):
            if mount.get('Type') != 'bind':
                continue
            source = _canonical_absolute_path(mount.get('Source'))
            destination = destinations[index]
            for root in allowed:
                if source.is_relative_to(root):
                    host, target = source, destination
                elif root.is_relative_to(source):
                    host, target = root, destination / root.relative_to(source)
                else:
                    continue
                if any(other != destination and other.is_relative_to(target)
                       for other in destinations):
                    raise DeploymentError('Nested mount shadows a permitted media folder')
                if type(mount.get('RW')) is not bool:
                    raise DeploymentError('Media folder access is unknown')
                if endpoint == 'worker' and mount['RW']:
                    raise DeploymentError('Worker media folders must be read-only')
                aliases.append({'source': str(host), 'destination': str(target), 'rw': mount['RW']})
        # Empty is meaningful: the endpoint has no verified access to any
        # permitted media folder and cannot be selected as a corresponding alias.
        receipts[endpoint] = sorted(aliases, key=lambda row: (row['source'], row['destination']))
    if seen != set(names):
        raise DeploymentError('Incomplete folder Docker snapshot')
    return receipts


def validate_folder_containers(containers, config, errors=None):
    """Validate each endpoint independently; unavailable optional apps stay absent."""
    specs, _ = folder_deployment_policy(config)
    errors = errors if errors is not None else {}
    expected = {entry['container']: endpoint for endpoint, entry in specs.items()}
    if not isinstance(containers, list):
        raise DeploymentError('Invalid folder Docker snapshot')
    indexed = {}
    for row in containers:
        name = row.get('Name', '').removeprefix('/') if isinstance(row, dict) and isinstance(row.get('Name'), str) else None
        if name not in expected or name in indexed:
            raise DeploymentError('Unexpected or duplicate folder container')
        indexed[name] = row
    receipts = {}
    for endpoint, entry in specs.items():
        row = indexed.get(entry['container'])
        if row is None:
            errors.setdefault(endpoint, 'Folder container unavailable')
            continue
        scoped = dict(config, folder_deployment={endpoint: entry})
        try:
            receipts.update(_validate_folder_containers_strict([row], scoped))
            errors.pop(endpoint, None)
        except Exception as exc:
            errors[endpoint] = str(exc) if isinstance(exc, DeploymentError) else type(exc).__name__
    return receipts


def check_folder_deployment(config, runner=subprocess.run):
    config = _book_folder_config(config)
    specs, _ = folder_deployment_policy(config)
    snapshots, errors = [], {}
    for endpoint, entry in specs.items():
        try:
            result = runner(['docker', 'inspect', entry['container']], capture_output=True,
                            text=True, timeout=20, check=True)
            rows = json.loads(result.stdout)
            if (not isinstance(rows, list) or len(rows) != 1
                    or not isinstance(rows[0], dict) or rows[0].get('Name') != '/' + entry['container']):
                raise DeploymentError('Invalid individual folder container snapshot')
            snapshots.extend(rows)
        except Exception as exc:
            errors[endpoint] = str(exc) if isinstance(exc, DeploymentError) else type(exc).__name__
    return validate_folder_containers(snapshots, config, errors), errors


def validate_folder_aliases(config, gate, now=None):
    """Prove active aliases from fresh host evidence, scoped to the chosen provider.

    The sync never trusts editable aliases as proof of shared files. A stopped
    BookOrbit is irrelevant in BookBridge mode, and an inactive BookBridge is
    irrelevant in BookOrbit mode. The caller may pass its controlled clock.
    """
    if config.get('folder_mappings') is None:
        return True  # Legacy deployments retain their original mount checks.
    now = time.time() if now is None else now
    checked = gate.get('checked_at') if isinstance(gate, dict) else None
    if (not isinstance(gate, dict) or gate.get('folder_gate_ok') is not True
            or type(checked) not in (int, float) or not math.isfinite(checked)
            or type(now) not in (int, float) or not math.isfinite(now)
            or not 0 <= now - checked <= RECEIPT_MAX_AGE or not isinstance(gate.get('folder_mounts'), dict)):
        raise DeploymentError('Fresh folder mount verification is unavailable')
    from folder_manager import active_folders, validate_service_paths
    provider = config.get('mapping_provider', 'bookorbit')
    if provider not in {'bookorbit', 'bookbridge'}:
        raise DeploymentError('Unsupported folder mapping provider')
    jellyfin = config.get('jellyfin', {}).get('enabled') is True
    auto_ebooks = jellyfin and config.get('jellyfin', {}).get('auto_ebooks', {}).get('enabled') is True
    try:
        rows = active_folders(config)
    except Exception:
        raise DeploymentError('Invalid active folder configuration') from None
    if not any(row.get('kind') == 'audiobooks' for row in rows):
        raise DeploymentError('An active audiobook folder is required')
    if provider == 'bookorbit' and auto_ebooks and not any(row.get('kind') == 'ebooks' for row in rows):
        raise DeploymentError('An active ebook folder is required')
    for row in rows:
        if row.get('kind') == 'podcasts':
            continue  # Independently checked by validate_podcast_aliases.
        paths = row.get('paths')
        if not isinstance(paths, dict):
            raise DeploymentError('Invalid active folder paths')
        if row.get('kind') == 'audiobooks':
            required = {'worker', 'abs'}
            if provider == 'bookorbit':
                required.add('bo')
            elif paths.get('bb'):
                required.add('bb')
            if jellyfin:
                required.add('jf')
        elif row.get('kind') == 'ebooks':
            if provider == 'bookorbit' and auto_ebooks:
                required = {'worker', 'bo', 'jf'}
            elif provider == 'bookbridge' and paths.get('bb'):
                required = {'worker', 'bb'}
            else:
                continue
        else:
            raise DeploymentError('Unsupported active folder type')
        if not required <= set(paths) or not paths.get('server'):
            raise DeploymentError('Required folder aliases are missing')
        mounts = []
        for endpoint in required:
            receipt = gate['folder_mounts'].get(endpoint)
            if not isinstance(receipt, list) or not receipt:
                raise DeploymentError('Required folder endpoint is not verified')
            for mount in receipt:
                if (not isinstance(mount, dict) or set(mount) != {'source', 'destination', 'rw'}
                        or type(mount['rw']) is not bool
                        or endpoint == 'worker' and mount['rw']):
                    raise DeploymentError('Invalid trusted folder mount receipt')
                mounts.append({'endpoint': endpoint, 'container': mount['destination'], 'server': mount['source']})
        selected = dict(row, paths={key: value for key, value in paths.items() if key in required or key == 'server'})
        try:
            validate_service_paths(selected, mounts)
        except Exception:
            raise DeploymentError('Selected folders do not resolve to the same verified media') from None
    return True


def _safe_check(check, config, runner):
    try:
        return True, check(config, runner)
    except Exception as exc:
        # Never expose process output, arguments, credentials or config values.
        return False, str(exc) if isinstance(exc, DeploymentError) else type(exc).__name__


def _collect_gate(root, runner, clock):
    gate = {'checked_at': clock(), 'ok': False, 'bookbridge_ok': False, 'podcasts_ok': False,
            'podcasts_reason': 'Podcast deployment is not configured',
            'pinepods_ok': False, 'pinepods_reason': 'PinePods image is not pinned',
            'bookbridge_reason': 'BookBridge deployment is not configured',
            'folder_gate_ok': False, 'folder_reason': 'Folder deployment is not configured'}
    try:
        config = json.loads((root / 'config/config.json').read_text())
    except Exception as exc:
        gate['reason'] = str(exc) if isinstance(exc, DeploymentError) else type(exc).__name__
        return gate
    # Independent result: an absent or failed BookOrbit must not invalidate
    # an explicitly reviewed ABS/BookBridge/Jellyfin deployment.
    checks = [('ok', 'reason', check_deployment)]
    if isinstance(config, dict) and 'bookbridge_deployment' in config:
        checks.append(('bookbridge_ok', 'bookbridge_reason', check_bookbridge_deployment))
    if isinstance(config, dict) and 'podcast_deployment' in config:
        checks.append(('podcasts_ok', 'podcasts_reason', check_podcast_deployment))
    if isinstance(config, dict) and isinstance(config.get('podcast_deployment'), dict) and 'pinepods' in config['podcast_deployment']:
        checks.append(('pinepods_ok', 'pinepods_reason', check_pinepods_deployment))
    if isinstance(config, dict) and 'folder_deployment' in config:
        checks.append(('folder_gate_ok', 'folder_reason', check_folder_deployment))
    # Each group keeps its own ordered checks and 20s subprocess deadlines.
    # Running independent groups together avoids adding all their timeouts to
    # the age of one shared receipt. No worker mutates the gate or config.
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        pending = [(flag, reason, pool.submit(_safe_check, check, config, runner))
                   for flag, reason, check in checks]
        for flag, reason, future in pending:
            passed, result = future.result()
            if not passed:
                gate[reason] = result
                continue
            gate[flag] = result if flag == 'pinepods_ok' else True
            gate.pop(reason, None)
            if flag == 'podcasts_ok':
                gate['podcast_mounts'] = result
            elif flag == 'folder_gate_ok':
                gate['folder_mounts'], gate['folder_errors'] = result
    finished = clock()
    checked = gate['checked_at']
    if (any(isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) for value in (checked, finished))
            or not 0 <= finished - checked <= RECEIPT_MAX_AGE):
        # Keep the earliest observation time. Stamping completion as fresh
        # would disguise old image/mount evidence after a slow Docker daemon.
        for flag, reason, _ in checks:
            gate[flag] = False
            gate[reason] = 'Deployment verification expired before publication'
        for field in ('podcast_mounts', 'folder_mounts', 'folder_errors'):
            gate.pop(field, None)
    return gate


def _publish_gate(dest, gate):
    descriptor, name = tempfile.mkstemp(prefix='.deployment-', suffix='.tmp', dir=dest.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(gate, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, dest)
    finally:
        temporary.unlink(missing_ok=True)


def main(root=None, runner=subprocess.run, clock=time.time):
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    # Hold one persistent lock inode through inspection and publication. An
    # overlapping cron invocation must not publish an older snapshot last.
    with os.fdopen(os.open(root / 'state/.deployment.lock', os.O_WRONLY | os.O_CREAT, 0o600), 'w') as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 1
        gate = _collect_gate(root, runner, clock)
        _publish_gate(root / 'state/deployment.json', gate)
        return 0 if gate['ok'] else 1


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="Verify a Docker deployment and refresh its state receipt.")
    parser.add_argument("--root", type=Path, help="Directory containing config/ and state/")
    raise SystemExit(main(root=parser.parse_args().root))
