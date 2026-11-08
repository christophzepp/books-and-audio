"""Execute authenticated administration requests between sync cycles."""
from pathlib import Path
from contextlib import closing
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time

import requests
from abs_client import ABSClient
from bo_client import BOClient
from jellyfin_client import JellyfinClient
from admin_store import AdminStore, atomic, stamp
from connection_manager import ConnectionManager, variant_flag_only_enable
from folder_manager import FolderManager, FolderManagerError
from io_metrics import increment, snapshot


def options(kind, cfg, credentials):
    if kind == 'pinepods':
        return {k: v for k, v in cfg['pinepods'].items() if k in ('url', 'username', 'device_id', 'completion_margin_seconds', 'server_version')}, {'password': credentials['pinepods_password'], 'api_key': credentials.get('pinepods_api_key')}
    if kind == 'bookbridge':
        return {k: cfg['bookbridge'][k] for k in ('url', 'username', 'version')}, {'password': credentials['bookbridge_password']}
    if kind == 'audiobookshelf':
        return {'url': cfg['abs_url'], 'version': cfg['abs_version'],
                'library_id': cfg['auto_discovery']['abs_library_id']}, {'token': credentials['abs_token']}
    if kind == 'bookorbit':
        return {'url': cfg['bo_url'], 'username': credentials['bo_username'],
                'library_ids': cfg['auto_discovery']['bo_library_ids']}, {'password': credentials['bo_password']}
    if kind == 'jellyfin':
        value = cfg['jellyfin']
        return {k: value[k] for k in ('url', 'user_id', 'username', 'version', 'audio_library_id')}, {'token': credentials['jellyfin_token']}
    return {'url': cfg['ntfy_url']}, {'authorization': credentials['ntfy_authorization']}


def probe(kind, settings, secrets):
    """Only the ntfy test publishes a clearly identified test notification."""
    client = None
    try:
        if kind == 'pinepods':
            from pinepods_client import PinepodsClient
            client = PinepodsClient(settings['url'], settings['username'], secrets['password'],
                device_id=settings['device_id'], completion_margin_seconds=settings.get('completion_margin_seconds', 60),
                api_key=secrets.get('api_key'), server_version=settings.get('server_version', 'unverified'))
            value = client.probe()
            return dict(ok=True, username=value['username'], device_id=value['device_id'],
                        device_exists=value.get('device_exists') is True, protocol=value['protocol'],
                        progress_sync_supported=value.get('progress_sync_supported') is True)
        if kind == 'bookbridge':
            from bookbridge_client import BookBridgeClient
            client = BookBridgeClient(settings['url'], settings['username'], secrets['password'], expected_version=settings['version'])
            value = client.probe()
            return {'ok': True, 'username': value['username'], 'version': value['version'],
                    'writers_disabled': value.get('abs_enabled') is False and value.get('bookorbit_enabled') is False}
        if kind == 'audiobookshelf':
            client = ABSClient(settings['url'], secrets['token'])
            version = client.version()
            if version != settings['version']: return {'ok': False}
            user = client._request('GET', '/api/me', '/api/me')
            # /api/me includes a token. Never return, log or persist that object.
            ident, username = user.get('id'), user.get('username')
            if not isinstance(ident, str) or not ident or not isinstance(username, str): return {'ok': False}
            ids = [row['id'] for row in client.libraries()]
            if settings['library_id'] not in ids: return {'ok': False}
            return {'ok': True, 'user_id': ident, 'username': username, 'version': version,
                    'library_ids': [settings['library_id']]}
        if kind == 'bookorbit':
            client = BOClient(settings['url'], settings['username'], secrets['password'])
            client._login()
            user = client.user
            if not isinstance(user, dict) or not user.get('id'): return {'ok': False}
            for ident in settings['library_ids']:
                if client.library(ident).get('id') != ident: return {'ok': False}
            return {'ok': True, 'user_id': str(user['id']),
                    'username': user.get('username', settings['username']), 'library_ids': settings['library_ids']}
        if kind == 'jellyfin':
            client = JellyfinClient(settings['url'], secrets['token'], settings['user_id'], settings['username'], settings['version'])
            client.guard()
            info = client.request('GET', '/System/Info/Public')
            if not isinstance(info.get('Id'), str) or not info['Id']: return {'ok': False}
            # Exact library scope is validated again against every existing mapping.
            client.items(parent=settings['audio_library_id'])
            return {'ok': True, 'server_id': info['Id'], 'user_id': settings['user_id'],
                    'username': settings['username'], 'version': settings['version']}
        if kind == 'ntfy':
            session = requests.Session(); session.trust_env = False
            with session:
                response = session.post(settings['url'], headers={'Authorization': secrets['authorization'],
                    'Title': 'Books & Audio - Verbindungstest', 'Tags': 'books', 'Priority': '2', 'Cache': 'no'},
                    data='Books & Audio hat die Verbindung erfolgreich geprüft.'.encode(),
                    timeout=(5, 15), allow_redirects=False)
                return {'ok': 200 <= response.status_code < 300}
    except Exception:
        return {'ok': False}
    finally:
        if client is not None:
            if hasattr(client, 'close'): client.close()
            elif hasattr(client, 's'): client.s.close()
    return {'ok': False}


def close_bridge(bridge):
    if bridge is None: return
    for client in (bridge.abs, bridge.bo, bridge.jellyfin, getattr(bridge, 'bookbridge', None), getattr(bridge, 'pinepods', None)):
        if client is not None:
            if hasattr(client, 'close'): client.close()
            elif hasattr(client, 's'): client.s.close()
    bridge.db.close()


def variant_activation_signature(result, enrolled_ids):
    """Only unchanged, already enrolled media guards may survive this opt-in.

    System/account/deployment failures, pending journals and new per-episode
    conflicts are not accepted by this narrowly scoped configuration check.
    """
    from pinepods_client import EpisodeUnavailable
    allowed={'duration_mismatch','media_identity_mismatch','episode_identity_unresolved'}
    if (not isinstance(result,dict) or type(result.get('mapped'))is not int or result['mapped']<0
            or not isinstance(result.get('scope_fingerprint'),str) or len(result['scope_fingerprint'])!=64
            or any(c not in '0123456789abcdef' for c in result['scope_fingerprint'])
            or not isinstance(result.get('episodes'),list) or not isinstance(result.get('blocked'),list)
            or result.get('mode')!='dry-run' or result.get('enabled')is not True
            or result.get('fatal') or result.get('pine_metadata_orphaned')):
        raise ValueError('podcast_not_ready')
    rows={}
    for row in result['episodes']:
        if (not isinstance(row,dict) or not isinstance(row.get('id'),str) or row['id'] in rows
                or not row['id'].startswith('_podcast:')):raise ValueError('podcast_not_ready')
        rows[row['id']]=row
    blocked={}
    for failure in result['blocked']:
        if not isinstance(failure,dict) or failure.get('id')not in rows:raise ValueError('podcast_not_ready')
        ident=failure['id'];row=rows[ident];code=row.get('pine_reason_code')
        if (ident in blocked or ident not in enrolled_ids or code not in allowed
                or row.get('progress_status')!='blocked' or row.get('pine_status')!='blocked'
                or row.get('reason')!=EpisodeUnavailable.REASONS[code]
                or failure.get('reason')!=row['reason']):raise ValueError('podcast_not_ready')
        blocked[ident]=(code,row['reason'])
    if (result.get('ok') is not (not blocked)
            or {ident for ident,row in rows.items() if row.get('progress_status')=='blocked'}!=set(blocked)
            or any(row.get('progress_status')not in ('unchanged','proposed','waiting','blocked') for row in rows.values())):
        raise ValueError('podcast_not_ready')
    core={ident:{key:copy.deepcopy(row.get(key)) for key in ('position','status','duration')}
          for ident,row in rows.items()}
    for ident,row in rows.items():
        states=row.get('states')
        core[ident]['states']={key:copy.deepcopy(states.get(key)) for key in ('abs','jf')} if isinstance(states,dict) else None
    return dict(scope=result['scope_fingerprint'],mapped=result['mapped'],blocked=blocked,core=core)


def coordinator_state_signature(db):
    """Private state digest; never return the potentially sensitive row values."""
    value={table:list(db.execute('SELECT * FROM '+table+' ORDER BY 1')) for table in ('state','operations')}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def variant_activation_deployment(root,config,previous=None):
    """Read a real fresh host receipt; only its observation time may advance."""
    from watch_deployment import validate_podcast_aliases
    raw=(Path(root)/'deployment.json').read_bytes()
    gate=json.loads(raw)
    validate_podcast_aliases(config,gate,now=time.time())
    if config.get('podcasts',{}).get('pinepods_enabled') and gate.get('pinepods_ok') is not True:
        raise ValueError('podcast_deployment_changed')
    identity={key:value for key,value in gate.items() if key!='checked_at'}
    if previous is not None and (identity!=previous['identity'] or gate['checked_at']<previous['checked_at']):
        raise ValueError('podcast_deployment_changed')
    return dict(raw=raw,identity=identity,checked_at=gate['checked_at'])


class Administration:
    def __init__(self, root, key, base_config, base_credentials, bridge_type):
        self.root, self.bridge_type = Path(root), bridge_type
        self.store = AdminStore(root, key)
        self.manager = ConnectionManager(root, base_config, base_credentials, Path(key).read_bytes())
        self.manager.seed()
        self.folders = FolderManager(root, base_config) if 'folder_access_roots' in base_config else None
        if self.folders: self.folders.seed()
        self.base_config, self.base_credentials = copy.deepcopy(base_config), copy.deepcopy(base_credentials)
        self.identity_path = self.store.control / 'account-identities.enc'
        self.store.recover()
        self.identities = None
        self._published_snapshots = {}
        if self.identity_path.exists():
            self.identities = self.store.decode(self.identity_path.read_bytes())

    def ensure_identities(self):
        if self.identities is not None: return
        identities = {}
        current, _, _ = self.manager.effective()
        for kind in ('audiobookshelf', 'bookorbit', 'jellyfin'):
            if kind == 'bookorbit' and current.get('mapping_provider', 'bookorbit') == 'bookbridge': continue
            if kind == 'jellyfin' and not self.base_config.get('jellyfin', {}).get('enabled'): continue
            settings, secrets = options(kind, self.base_config, self.base_credentials)
            value = probe(kind, settings, secrets)
            if value.get('ok') is not True:
                raise ValueError('Bestehende Konten konnten noch nicht sicher geprüft werden')
            identities[kind] = value
        raw = self.store.encode(identities)
        fd, tmp = tempfile.mkstemp(prefix='.identities-', dir=self.store.control)
        try:
            with os.fdopen(fd, 'wb') as handle:
                os.fchmod(handle.fileno(), 0o600); handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            os.replace(tmp, self.identity_path)
            directory = os.open(self.store.control, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        self.identities = identities

    def compatible(self, kind, old_cfg, old_credentials, cfg, credentials, result):
        if kind == 'ntfy': return True
        if kind == 'pinepods':
            if (result.get('device_exists') is not True or result.get('protocol') != 'gpodder-v2'
                    or result.get('progress_sync_supported') is not True): return False
            old = old_cfg.get('pinepods', {})
            value = cfg.get('pinepods', {})
            if old.get('enabled') and any(old.get(k) != value.get(k) for k in ('url', 'username', 'device_id')): return False
            if self.podcast_pending(): return False
            return result.get('username') == value.get('username') and result.get('device_id') == value.get('device_id')
        if kind == 'bookbridge':
            old = old_cfg.get('bookbridge', {})
            if old.get('enabled') and old.get('username') != result.get('username'): return False
            if cfg.get('mapping_provider', 'bookorbit') == 'bookorbit':
                from bookbridge_client import BookBridgeClient
                settings, secrets = options(kind, cfg, credentials)
                client = BookBridgeClient(settings['url'], settings['username'], secrets['password'], expected_version=settings['version'])
                try:
                    proof = client.probe()
                    if proof.get('abs_enabled') is not False or proof.get('bookorbit_enabled') is not False: return False
                finally: client.close()
            return result.get('username') == cfg['bookbridge'].get('username')
        self.ensure_identities()
        expected = self.identities.get(kind)
        if expected is None: return False
        for key in ('server_id', 'user_id', 'username', 'version'):
            if key in expected and result.get(key) != expected[key]: return False
        if kind == 'audiobookshelf' and result.get('library_ids') != expected.get('library_ids'): return False
        if kind == 'bookorbit' and not set(expected.get('library_ids', [])).issubset(result.get('library_ids', [])): return False
        # Credential rotation on the already pinned endpoint/account must stay
        # possible even while one book is waiting for a separate file review.
        reference_settings, _ = options(kind, self.base_config, self.base_credentials)
        candidate_settings, _ = options(kind, cfg, credentials)
        if candidate_settings == reference_settings:
            return True
        # For a previously disabled required connection, validate the candidate
        # with the known other required account. This does not reactivate it.
        test_cfg, test_credentials = copy.deepcopy(cfg), copy.deepcopy(credentials)
        if not test_cfg.get('abs_url'):
            test_cfg.update(abs_url=self.base_config['abs_url'], abs_version=self.base_config['abs_version'])
            test_cfg.setdefault('auto_discovery', {})['abs_library_id'] = self.base_config['auto_discovery']['abs_library_id']
        if not test_cfg.get('bo_url') and test_cfg.get('mapping_provider', 'bookorbit') != 'bookbridge':
            test_cfg['bo_url'] = self.base_config['bo_url']
            test_cfg.setdefault('auto_discovery', {})['bo_library_ids'] = self.base_config['auto_discovery']['bo_library_ids']
        for key in ('abs_token', 'bo_username', 'bo_password'):
            if key not in test_credentials and key in self.base_credentials: test_credentials[key] = self.base_credentials[key]
        if self.folders: test_cfg = self.folders.effective(test_cfg)
        if test_cfg.get('bookbridge'):
            test_cfg['bookbridge']['expected_abs_username'] = self.identities['audiobookshelf']['username']
        if test_cfg.get('mapping_provider') == 'bookbridge':
            return self.validate_candidate(test_cfg, test_credentials).get('ok') is True
        candidate = None
        with tempfile.TemporaryDirectory(prefix='connection-check-', dir=self.store.control) as directory:
            path = Path(directory)
            with sqlite3.connect('file:' + str(self.root / 'state.sqlite') + '?mode=ro', uri=True) as source, sqlite3.connect(path / 'state.sqlite') as dest:
                source.backup(dest)
            shutil.copy2(self.root / 'deployment.json', path / 'deployment.json')
            try:
                candidate = self.bridge_type(path, test_cfg, test_credentials)
                candidate.guard()
                mappings = candidate.list_mappings()
                if not mappings and kind in ('audiobookshelf', 'bookorbit'):
                    key = 'abs_url' if kind == 'audiobookshelf' else 'bo_url'
                    if cfg.get(key, '').rstrip('/') != self.base_config[key].rstrip('/'): return False
                for mapping in mappings:
                    candidate.read_pair(mapping)
                if kind == 'jellyfin':
                    from jellyfin_audio import bind
                    inventory = candidate.jellyfin.items(parent=test_cfg['jellyfin']['audio_library_id'])
                    if any(bind(mapping, inventory, test_cfg) is None for mapping in mappings): return False
                    ebooks = test_cfg['jellyfin'].get('ebooks', []) + candidate.load('_ebook_discovery').get('mappings', [])
                    for ebook in ebooks:
                        items = candidate.jellyfin.items(ids=[ebook['jf_id']], types='Book')
                        if len(items) != 1 or items[0].get('Path') != ebook['jf_path']: return False
                return True
            finally: close_bridge(candidate)

    def effective(self):
        cfg, creds, disabled = self.manager.effective()
        cfg = self.podcast_effective(cfg)
        if self.folders:
            try: cfg = self.folders.effective(cfg, context=self.folder_context())
            except FolderManagerError as exc: disabled = folder_error(str(exc))
        if self.identities and cfg.get('bookbridge'):
            cfg['bookbridge']['expected_abs_username'] = self.identities.get('audiobookshelf', {}).get('username')
        return cfg, creds, disabled

    def folder_context(self, kind=None):
        """Generation of real host evidence, independent of its heartbeat time."""
        try:
            gate = json.loads((self.root / 'deployment.json').read_text())
            if not isinstance(gate, dict): raise ValueError('Invalid receipt')
            # Only structural fields are cache identities. Observation times or
            # unrelated podcast checks must not reread unchanged book contents.
            if kind == 'podcasts':
                return {k: gate.get(k) for k in ('podcasts_ok', 'podcast_mounts', 'pinepods_ok')}
            fields = ('folder_gate_ok', 'folder_mounts', 'folder_errors',
                      'ok', 'bookbridge_ok')
            value = {k: gate.get(k) for k in fields}
            if kind in {'ebooks', 'audiobooks'}:
                cfg, _, _ = self.manager.effective()
                provider = cfg.get('mapping_provider', 'bookorbit')
                endpoints = {'worker', 'bb'} if provider == 'bookbridge' else {'worker', 'bo'}
                if kind == 'audiobooks': endpoints.add('abs')
                if cfg.get('jellyfin', {}).get('enabled'): endpoints.add('jf')
                for key in ('folder_mounts', 'folder_errors'):
                    rows = value[key]
                    value[key] = {k: v for k, v in rows.items() if k in endpoints} if isinstance(rows, dict) else rows
                value.pop('ok' if provider == 'bookbridge' else 'bookbridge_ok', None)
            return value
        except (OSError, ValueError):
            return {'deployment_unavailable': True}

    def media_guard(self, kind):
        if self.folders is not None:
            self.folders.require(kind, context=self.folder_context())

    def media_generation(self, kind):
        if self.folders is not None:
            return self.folders.generation(kind, context=self.folder_context(kind))
        return None

    def publish_changed(self, name, value, *, heartbeat=None):
        """Deduplicate disposable GUI snapshots; keep a bounded live heartbeat.

        Coordinator receipts, journals and command transactions keep their
        existing durable writes. Only presentation files use this suppression.
        """
        comparison = copy.deepcopy(value)
        if name == 'status.json':
            comparison.get('worker', {}).pop('at', None)
            comparison.pop('io_metrics', None)
        signature = hashlib.sha256(json.dumps(comparison, sort_keys=True,
            ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        previous = self._published_snapshots.get(name)
        now = time.monotonic()
        path = self.store.control / name
        if (previous and previous[0] == signature and path.exists()
                and (heartbeat is None or 0 <= now - previous[1] < heartbeat)):
            increment(self, 'gui_snapshot_skips')
            return False
        if name == 'status.json': value = dict(value, published_at=time.time())
        atomic(path, value)
        self._published_snapshots[name] = (signature, now)
        increment(self, 'gui_snapshot_writes')
        return True

    def podcast_effective(self, cfg):
        cfg = copy.deepcopy(cfg)
        static = self.base_config.get('podcasts', {})
        if static or cfg.get('podcasts'):
            value = cfg.setdefault('podcasts', {})
            for key in ('abs_root', 'jellyfin_root', 'worker_root', 'expected_abs_username', 'expected_abs_user_id', 'settle_seconds'):
                if key in static and key not in value: value[key] = copy.deepcopy(static[key])
        return cfg

    def podcast_pending(self):
        with sqlite3.connect('file:' + str(self.root / 'state.sqlite') + '?mode=ro', uri=True) as db:
            for key, raw in db.execute("SELECT id,value FROM state WHERE id LIKE '_podcast:%' OR id LIKE '_podcast_status:%'"):
                if json.loads(raw).get('pending'): return True
            return bool(db.execute("SELECT 1 FROM operations WHERE status='pending' AND (book LIKE '_podcast:%' OR book LIKE '_podcast_status:%') LIMIT 1").fetchone())

    def podcast_check(self, cfg, creds, *, verified_folders=False):
        """Podcast checks never contact BookOrbit or alter the live coordinator DB."""
        if self.podcast_pending(): raise ValueError('podcast_pending')
        cfg = self.podcast_effective(cfg)
        if not cfg.get('podcasts', {}).get('enabled'):
            return dict(ok=True, mapped=0, message='Podcast-Abgleich wird deaktiviert. Gespeicherte Fortschritte bleiben erhalten.')
        if self.folders and not verified_folders: cfg = self.folders.effective(cfg)
        with tempfile.TemporaryDirectory(prefix='podcast-check-', dir=self.store.control) as directory:
            path = Path(directory)
            with sqlite3.connect('file:' + str(self.root / 'state.sqlite') + '?mode=ro', uri=True) as source, sqlite3.connect(path / 'state.sqlite') as dest:
                source.backup(dest)
            shutil.copy2(self.root / 'deployment.json', path / 'deployment.json')
            candidate = None
            try:
                candidate = self.bridge_type(path, cfg, creds)
                import podcast_sync
                result = podcast_sync.preview(candidate)
                retained = 0
                if not isinstance(result, dict): raise ValueError('podcast_not_ready')
                if result.get('ok') is not True:
                    # A preference-only change may retain the very same identity
                    # locks; it cannot grant them progress access or change scope.
                    previous, previous_creds, _ = self.manager.effective()
                    previous = self.podcast_effective(previous)
                    if self.folders: previous = self.folders.effective(previous)
                    old, new = copy.deepcopy(previous), copy.deepcopy(cfg)
                    old.setdefault('podcasts', {}).setdefault('primary_source', 'auto')
                    new.setdefault('podcasts', {}).setdefault('primary_source', 'auto')
                    old_primary = old['podcasts'].pop('primary_source')
                    new_primary = new['podcasts'].pop('primary_source')
                    if old != new or old_primary == new_primary or previous_creds != creds:
                        raise ValueError('podcast_not_ready')
                    other = None
                    try:
                        other = self.bridge_type(path, previous, creds)
                        before_result = podcast_sync.preview(other)
                        from podcast_sync import pine_history_ids
                        known = pine_history_ids(candidate) | {r['id'] for r in before_result.get('blocked', []) if isinstance(r, dict) and isinstance(r.get('id'), str)}
                        if variant_activation_signature(before_result, known) != variant_activation_signature(result, known):
                            raise ValueError('podcast_not_ready')
                        retained = len(result['blocked'])
                    finally: close_bridge(other)
                # Feed/enclosure addresses can contain private subscription tokens.
                public = {key: copy.deepcopy(result[key]) for key in ('ok', 'mapped', 'message') if key in result}
                from podcast_scope import public_scope
                public['scope'] = public_scope(result)
                public['ok'] = True
                if retained:
                    public['retained_blocked'] = retained
                    public['message'] = f'Primärquelle geprüft; {retained} bestehende Folgensperren bleiben erhalten.'
                for key in ('pine_waiting', 'pine_enrolled', 'pine_joining', 'pine_blocked'):
                    if type(result.get(key)) is int and result[key] >= 0:
                        public[key] = result[key]
                return public
            finally: close_bridge(candidate)

    def podcast_variant_activation_check(self, previous, candidate, creds):
        """Flag-only opt-in retains exactly the existing per-episode locks.

        All three dry runs use one immutable coordinator snapshot.  A changed
        inventory, status, blocked reason, pending operation, account or mount
        rejects the change. Nothing here grants a blocked episode progress
        access or changes its saved state.
        """
        if not variant_flag_only_enable(previous,candidate):raise ValueError('podcast_not_ready')
        if self.podcast_pending():raise ValueError('podcast_pending')
        previous=self.podcast_effective(previous);candidate=self.podcast_effective(candidate)
        if self.folders:
            previous=self.folders.effective(previous);candidate=self.folders.effective(candidate)
        if not variant_flag_only_enable(previous,candidate):raise ValueError('podcast_not_ready')
        with tempfile.TemporaryDirectory(prefix='podcast-variant-check-',dir=self.store.control) as temporary:
            directory=Path(temporary);master=directory/'snapshot.sqlite'
            with closing(sqlite3.connect('file:'+str(self.root/'state.sqlite')+'?mode=ro',uri=True)) as source, closing(sqlite3.connect(master)) as dest:
                source.execute('BEGIN')
                if (source.execute("SELECT 1 FROM operations WHERE status='pending' LIMIT 1").fetchone()
                        or any(json.loads(value).get('pending') for (value,) in source.execute('SELECT value FROM state'))):
                    raise ValueError('podcast_pending')
                before=coordinator_state_signature(source);source.backup(dest)
            from podcast_sync import pine_history_ids
            with closing(sqlite3.connect(master)) as snapshot:
                enrolled=pine_history_ids(type('ReadOnlyState',(),{'db':snapshot})())
            deployment=None
            results=[]
            for index,cfg in enumerate((previous,candidate,previous)):
                root=directory/str(index);root.mkdir(mode=0o700)
                with closing(sqlite3.connect(master)) as source,closing(sqlite3.connect(root/'state.sqlite')) as dest:source.backup(dest)
                deployment=variant_activation_deployment(self.root,cfg,deployment)
                (root/'deployment.json').write_bytes(deployment['raw'])
                bridge=None
                try:
                    bridge=self.bridge_type(root,cfg,creds)
                    import podcast_sync
                    result=podcast_sync.preview(bridge)
                    if coordinator_state_signature(bridge.db)!=before:raise ValueError('podcast_preview_changed_state')
                    results.append(result)
                finally:close_bridge(bridge)
            signatures=[variant_activation_signature(result,enrolled) for result in results]
            if signatures[0]!=signatures[1] or signatures[0]!=signatures[2]:raise ValueError('podcast_not_ready')
            variant_activation_deployment(self.root,candidate,deployment)
            with closing(sqlite3.connect('file:'+str(self.root/'state.sqlite')+'?mode=ro',uri=True)) as live:
                if coordinator_state_signature(live)!=before:raise ValueError('podcast_state_changed')
            if self.podcast_pending():raise ValueError('podcast_pending')
            retained=len(signatures[0]['blocked'])
            return dict(ok=True,mapped=signatures[0]['mapped'],retained_blocked=retained,
                message=f'Statusmodus geprüft; {retained} bestehende Folgen bleiben unverändert gesperrt.')

    def validate_candidate(self, cfg, creds):
        """Read-only service checks and an isolated copy of all coordinator state."""
        with sqlite3.connect('file:' + str(self.root / 'state.sqlite') + '?mode=ro', uri=True) as source:
            if source.execute("SELECT 1 FROM operations WHERE status='pending' LIMIT 1").fetchone():
                raise ValueError('pending_operations')
            if any(json.loads(row[0]).get('pending') for row in source.execute('SELECT value FROM state')):
                raise ValueError('pending_operations')
            with tempfile.TemporaryDirectory(prefix='provider-check-', dir=self.store.control) as directory:
                path = Path(directory)
                with sqlite3.connect(path / 'state.sqlite') as dest: source.backup(dest)
                shutil.copy2(self.root / 'deployment.json', path / 'deployment.json')
                candidate = None
                try:
                    candidate = self.bridge_type(path, cfg, creds)
                    candidate.guard()
                    if cfg.get('mapping_provider', 'bookorbit') == 'bookbridge':
                        from provider_runtime import provider_preview
                        result = provider_preview(candidate)
                        return dict(result, ok=True)
                    mappings = candidate.list_mappings()
                    for mapping in mappings: candidate.read_pair(mapping)
                    if candidate.jellyfin is not None:
                        from jellyfin_audio import bind
                        from jellyfin_ebook import EbookCoordinator
                        candidate.jellyfin.guard()
                        inventory = candidate.jellyfin.items(parent=cfg['jellyfin']['audio_library_id'])
                        if any(bind(mapping, inventory, cfg) is None for mapping in mappings):
                            raise ValueError('unmatched_jellyfin_audio')
                        ebooks = cfg['jellyfin'].get('ebooks', []) + candidate.load('_ebook_discovery').get('mappings', [])
                        coordinator = EbookCoordinator(candidate)
                        for mapping in ebooks: coordinator.handle(mapping, False, 0)
                    return {'ok': True, 'provider': 'bookorbit', 'audiobooks': len(mappings),
                            'message': 'BookOrbit-Verbindungen und bestehende Hörbuch-Zuordnungen sind geprüft.'}
                finally: close_bridge(candidate)

    def provider_check(self, cfg, creds):
        self.ensure_identities()
        if self.folders: cfg = self.folders.effective(cfg)
        if cfg.get('bookbridge'):
            cfg['bookbridge']['expected_abs_username'] = self.identities['audiobookshelf']['username']
        return self.validate_candidate(cfg, creds)

    def folder_compatible(self, old_cfg, new_cfg):
        cfg, creds, disabled = self.manager.effective()
        if disabled: return False
        # A missing active root must never fall back to the former hardcoded path.
        kinds = {r['kind'] for r in new_cfg.get('folder_mappings', [])}
        if not {'audiobooks', 'ebooks'}.issubset(kinds): return False
        cfg['folder_mappings'] = new_cfg['folder_mappings']
        if cfg.get('bookbridge'):
            self.ensure_identities()
            cfg['bookbridge']['expected_abs_username'] = self.identities['audiobookshelf']['username']
        spec = new_cfg.get('jellyfin', {}).get('auto_ebooks')
        if spec is not None: cfg.setdefault('jellyfin', {})['auto_ebooks'] = spec
        if self.validate_candidate(cfg, creds).get('ok') is not True: return False
        if cfg.get('podcasts', {}).get('enabled'):
            cfg['podcasts'].update({key: value for key, value in new_cfg.get('podcasts', {}).items()
                if key in ('abs_root', 'jellyfin_root', 'worker_root')})
            return self.podcast_check(cfg, creds, verified_folders=True).get('ok') is True
        return True

    def process_one(self, bridge, preview_only=False):
        command = self.store.claim(preview_only=True) if preview_only else self.store.claim()
        if command is None: return False
        ident, kind, payload = command['id'], command['kind'], command['payload']
        def require_media(media_kind):
            guard = getattr(self, 'media_guard', None)
            if guard is not None: guard(media_kind)
        try:
            if kind.startswith('connection.'):
                if kind.split('.')[1] in {'update', 'activate', 'deactivate', 'delete'} and self.podcast_pending():
                    profiles = self.manager.public()['profiles']
                    row = next((p for p in profiles if p['id'] == payload.get('id')), {})
                    if row.get('kind') in {'audiobookshelf', 'jellyfin', 'pinepods'}:
                        raise ValueError('podcast_pending')
                result = self.manager.mutate(kind.split('.')[1], payload, probe, self.compatible)
                if not result.get('ok'): self.store.finish(ident, result=result, error=connection_error(result.get('code')))
                else: self.store.finish(ident, result=result)
            elif kind.startswith('folder.'):
                if self.folders is None: raise ValueError('folder_management_unavailable')
                result = self.folders.mutate(kind.split('.')[1], payload, self.folder_compatible)
                self.store.finish(ident, result=result, error=None if result.get('ok') else folder_error(result.get('code')))
            elif kind in ('episode.status_preview','episode.status_apply'):
                import episode_editor
                from podcast_review import ReviewError
                try:
                    if bridge is None:raise ReviewError('Verbindungen nicht verfügbar.')
                    if kind=='episode.status_preview' and set(payload)=={'episode_id'}:
                        require_media('podcasts')
                        row=episode_editor.review(bridge,payload['episode_id'])
                        self.store.finish(ident,result=dict(row,review_id=ident),private=row)
                    elif kind=='episode.status_apply' and set(payload)=={'review_id','digest','source','confirm'} and payload['confirm'] is True:
                        row=self.store.review(payload['review_id'],'episode.status_preview')
                        if row['digest']!=payload['digest']:raise ReviewError('Die Freigabe passt nicht zur Prüfung.')
                        require_media('podcasts')
                        episode_editor.review(bridge,row['episode_id'],record=row,source=payload['source'])
                        self.store.finish(ident,result={'approved':True})
                    else:raise ReviewError('Ungültiger Prüfauftrag.')
                except ReviewError as exc:self.store.finish(ident,error=str(exc))
            elif kind in ('episode.preview','episode.approve'):
                import podcast_review
                try:
                    if bridge is None:
                        raise podcast_review.ReviewError('Bitte zuerst die Podcast-Verbindungen prüfen.')
                    if kind == 'episode.preview' and set(payload) == {'episode_id'}:
                        require_media('podcasts')
                        row = podcast_review.review(bridge, payload['episode_id'])
                        self.store.finish(ident, result=dict(row,review_id=ident), private=row)
                    elif (kind == 'episode.approve' and set(payload) == {'review_id','digest','source','confirm'}
                          and payload['confirm'] is True):
                        row = self.store.review(payload['review_id'], 'episode.preview')
                        if row['digest'] != payload['digest']:
                            raise podcast_review.ReviewError('Die Freigabe passt nicht zur angezeigten Prüfung.')
                        require_media('podcasts')
                        podcast_review.review(bridge,row['episode_id'],record=row,source=payload['source'])
                        self.store.finish(ident, result={'approved':True})
                    else:
                        raise podcast_review.ReviewError('Ungültiger Prüfauftrag für die Folge.')
                except podcast_review.ReviewError as exc:
                    self.store.finish(ident, error=str(exc))
            elif kind.startswith('podcast.'):
                result = self.manager.podcasts(payload,self.podcast_check,apply=kind=='podcast.configure',
                    variant_activation_check=self.podcast_variant_activation_check)
                self.store.finish(ident, result=result, error=None if result.get('ok') else connection_error(result.get('code')))
            elif kind.startswith('provider.'):
                result = self.manager.provider(payload, self.provider_check, apply=kind == 'provider.activate')
                self.store.finish(ident, result=result, error=None if result.get('ok') else connection_error(result.get('code')))
            elif kind == 'events.clear':
                if set(payload) != {'confirm'} or payload['confirm'] is not True:
                    raise ValueError('Das Leeren des Protokolls muss bestätigt werden.')
                self.store.clear_event_history(ident)
            elif kind == 'sync.pause':
                self.store.pause(True); self.store.finish(ident, {'paused': True})
            elif kind == 'sync.resume':
                self.store.pause(False); self.store.finish(ident, {'paused': False, 'checks_remain_enabled': True})
            else:
                if bridge is None: raise ValueError('Bitte zuerst die erforderlichen Verbindungen aktivieren')
                import manual_review
                if kind == 'review.preview':
                    if set(payload) != {'abs_id'}: raise ValueError('Ungültiger Prüfauftrag')
                    require_media('audiobooks')
                    record = manual_review.preview(bridge, payload['abs_id'])
                    self.store.finish(ident, result=public_review(ident, record), private=record)
                elif kind == 'review.approve':
                    if set(payload) != {'review_id', 'digest', 'confirm'} or payload['confirm'] is not True:
                        raise ValueError('Die konkrete Dateifreigabe muss bestätigt werden')
                    record = self.store.review(payload['review_id'])
                    require_media('audiobooks')
                    result = manual_review.approve(bridge, record, payload['digest'])
                    self.store.finish(ident, result=result)
        except Exception as exc:
            # Our pure review failures contain only fixed diagnostics. Do not
            # echo arbitrary transport/configuration exception messages.
            message = 'Auftrag konnte nicht sicher ausgeführt werden. Status prüfen und erneut versuchen.'
            if isinstance(exc, ValueError) and kind.startswith('review.'):
                message = 'Die Dateiprüfung ist nicht mehr gültig oder der Titel ist nicht freigabefähig. Bitte neu prüfen.'
            self.store.finish(ident, error=message)
        return True

    def publish(self, bridge, disabled_reason=None, busy=None):
        if self.folders: self.publish_changed('folders-public.json', self.folders.public())
        try: last = json.loads((self.root / 'last-run.json').read_text())
        except (OSError, ValueError): last = {}
        issues = list(last.get('blocked', []))
        paused = self.store.paused()
        podcast_cfg = self.manager.public().get('podcasts', {})
        try: podcasts = json.loads((self.root / 'last-podcast-run.json').read_text())
        except (OSError, ValueError): podcasts = {}
        podcasts = public_podcasts(podcasts, podcast_cfg, paused)
        rows = []
        if bridge is not None:
            try:
                for mapping in bridge.list_mappings():
                    ident, title = mapping['abs_id'], mapping['title']
                    bad = next((x for x in issues if x.get('title') == title), None)
                    pause = bridge.config.get('paused_mappings', {}).get(ident)
                    wait = next((x for x in last.get('native_readalong', []) if x.get('title') == title), None)
                    bb_mode = bridge.config.get('mapping_provider', 'bookorbit') == 'bookbridge'
                    state = bridge.load(('_bookbridge_audio:' if bb_mode else '') + ident).get('baseline', {}).get('abs', {})
                    rows.append({'id': ident, 'abs_id': ident, 'bo_id': mapping.get('bo_id'), 'bb_id': mapping.get('bb_id'), 'title': title,
                        'type': 'audiobook', 'can_review': not bb_mode,
                        'item_blocked': bool(bad or pause),
                        'has_readalong': bool(ident in bridge.config.get('native_readalong', {}) or bridge.db.execute('SELECT 1 FROM native_readalong_enrollments WHERE id=?', (ident,)).fetchone()),
                        'status': 'blocked' if bad else 'paused' if pause or paused else 'waiting' if wait else 'ok',
                        'reason': (bad or wait or {}).get('reason') or pause or disabled_reason or '',
                        'position': state.get('position'), 'progress_status': state.get('status')})
                issues = [dict(x, abs_id=next((r['id'] for r in rows if r['title'] == x.get('title')), None)) for x in issues]
                ebook_mappings = [] if bridge.config.get('mapping_provider') == 'bookbridge' else bridge.config.get('jellyfin', {}).get('ebooks', []) + bridge.load('_ebook_discovery').get('mappings', [])
                for mapping in ebook_mappings:
                    bad = next((x for x in last.get('ebooks', {}).get('blocked', []) if x.get('title') == mapping['title']), None)
                    rows.append({'id': 'ebook:' + str(mapping['file_id']), 'type': 'ebook',
                        'title': mapping['title'], 'bo_id': mapping['bo_id'], 'can_review': False,
                        'status': 'blocked' if bad else 'paused' if paused else 'ok',
                        'reason': bad.get('reason') if bad else '', 'position_percent': None})
                recent = []
                for at, book, kind, detail in bridge.db.execute('SELECT at,book,kind,detail FROM events ORDER BY id DESC LIMIT 30'):
                    info = json.loads(detail)
                    recent.append({'at': at, 'book': book, 'kind': kind,
                        'detail': {k: info[k] for k in ('title', 'direction', 'seconds', 'status', 'reason', 'progress_unchanged') if k in info}})
                self.publish_changed('sync-events.json', recent)
            except Exception:
                issues.append({'title': 'Zuordnungen', 'reason': 'Gespeicherte Zuordnungen konnten nicht vollständig gelesen werden'})
        ebooks = last.get('ebooks', {})
        for item in ebooks.get('blocked', []): issues.append(dict(item, type='ebook'))
        fatal = last.get('fatal')
        podcast_bad = podcasts['enabled'] and (not podcasts.get('at') or podcasts.get('blocked') or podcasts.get('fatal'))
        self.publish_changed('status.json', {'paused': paused, 'last_cycle': last.get('at'), 'blocked': issues, 'books': rows,
            'summary': {'mapped': last.get('mapped', len(rows)), 'ebooks': ebooks.get('mapped', 0),
                'blocked': len(issues), 'waiting': last.get('waiting', 0),
                'healthy': not (issues or fatal or disabled_reason or paused or podcast_bad),
                'podcasts_blocked': len(podcasts.get('blocked', [])) if podcasts['enabled'] else 0},
            'last_run': last, 'podcasts': podcasts, 'admin': {'paused': paused, 'reason': disabled_reason or fatal},
            'io_metrics': {'folders': snapshot(self.folders), 'presentation': snapshot(self), 'media': snapshot(bridge)},
            'worker': {'busy': busy, 'at': stamp()},
            'services': {}}, heartbeat=60)
        self.publish_changed('connections-public.json', self.manager.public())


def connection_error(code):
    # Fixed user-facing failures never quote credentials or response bodies.
    values = {
        'stale_revision': 'Die Verbindungen wurden inzwischen geändert. Bitte neu laden.',
        'probe_failed': 'Verbindungstest fehlgeschlagen. Adresse, Zugang und Bibliothek prüfen.',
        'incompatible_connection': 'Konto oder Bibliothek passen nicht zu den bestehenden Zuordnungen.',
        'incomplete_profile': 'Für diese Verbindung fehlen erforderliche Angaben.',
        'invalid_url': 'Die Adresse ist ungültig oder nicht als Dienstadresse erlaubt.',
        'address_unavailable': 'Die Dienstadresse kann nicht sicher aufgelöst werden.',
        'required_connection_disabled': 'Bitte zuerst Audiobookshelf und den gewählten Zuordnungsdienst verbinden und aktivieren.',
        'provider_not_ready': 'Der Modus ist noch nicht bereit. BookBridge benötigt dasselbe Audiobookshelf-Konto, aktivierten Hörbuch-Abgleich und passende Paare; seine BookOrbit-Anbindung muss ausgeschaltet sein. Laufende Übertragungen müssen beendet sein.',
        'pinepods_device_missing': 'Bitte die angegebene GPodder-Geräte-ID zuerst in PinePods anlegen.',
        'pinepods_progress_not_supported': 'PinePods benötigt für zuverlässigen Abschlussstatus einen persönlichen API-Schlüssel und eine geprüfte Version. Der GPodder-Zugang allein reicht bei Version 0.9.0 nicht aus.',
        'podcast_connections_missing': 'Für Podcasts werden aktive Verbindungen zu Audiobookshelf und Jellyfin benötigt.',
        'podcast_pinepods_missing': 'Bitte zuerst ein PinePods-Profil mit bestehendem GPodder-Gerät prüfen und aktivieren.',
        'bookorbit_podcasts_not_ready': 'BookOrbit-Podcasts sind vorbereitet und bleiben bis zur verifizierten Unterstützung inaktiv.',
        'invalid_podcast_settings': 'Bitte Podcast-Bibliotheken und Teilnehmer prüfen.',
        'invalid_podcast_primary_source': 'Bitte eine verfügbare primäre Podcastquelle auswählen.',
        'podcast_primary_pinepods_disabled': 'PinePods muss am Podcast-Abgleich teilnehmen, bevor es als primäre Quelle ausgewählt werden kann.',
        'podcast_not_ready': 'Podcast-Abgleich noch nicht bereit. Bibliotheken, Ordner, Konten und offene Übertragungen prüfen.',
        'bookbridge_writers_active': 'BookBridge schreibt noch Fortschritte. Bitte dort zuerst den Audiobookshelf- und BookOrbit-Abgleich ausschalten; erst danach kann dieses Profil getrennt, ersetzt oder entfernt werden.',
    }
    return values.get(code, 'Verbindungsänderung nicht übernommen. Eingaben, Test und aktuelle Version prüfen.')


def folder_error(code):
    return {
        'stale_folder_revision': 'Die Ordner wurden inzwischen geändert. Bitte neu laden.',
        'folder_not_in_allowed_mount': 'Dieser Ordner liegt außerhalb der freigegebenen Medienordner. Die Docker-Freigabe muss zuerst eingerichtet werden.',
        'folder_mount_unavailable': 'Die Medienfreigabe ist nicht eingehängt. Abgleich bleibt gesperrt.',
        'folder_unavailable': 'Der Ordner ist nicht vorhanden oder nicht lesbar.',
        'folder_check_pending': 'Der Ordner wurde noch nicht geprüft. Bitte „Ordner prüfen“ ausführen.',
        'folder_symlink_or_not_directory': 'Bitte einen echten Medienordner ohne Verknüpfung auswählen.',
        'incompatible_folder_mapping': 'Die Ordner passen nicht zu den bestehenden Zuordnungen. Dateien werden nicht verschoben; zuerst die Freigaben der Anwendungen prüfen.',
    }.get(code, 'Ordneränderung nicht übernommen. Pfade und Freigaben prüfen.')


def public_review(ident, record):
    files = record.get('files', [])
    if isinstance(files, dict): files = [dict(value, role=role) for role, value in files.items()]
    files = [{k: value[k] for k in ('role', 'file_id', 'path', 'size', 'sha256', 'content_version') if k in value} for value in files]
    if not files:
        proof = record.get('proof', record.get('current_proof', {}))
        for role in ('source', 'overlay'):
            value = proof.get(role, {})
            pin = value.get('pin', {})
            if pin: files.append({'role': role, 'path': pin.get('overlay_path'), 'size': pin.get('overlay_size'), 'sha256': value.get('sha256')})
    return {'review_id': ident, 'digest': record.get('review_digest'), 'title': record.get('title'),
            'files': files, 'reason': record.get('reason', ''), 'differences': record.get('differences', []),
            'blockers': record.get('blockers', []), 'can_approve': record.get('can_approve') is True,
            'expires_at': record.get('expires_at')}


def public_podcasts(value, config, paused):
    allowed = ('id', 'title', 'show', 'position', 'reason')
    episodes = []
    for row in value.get('episodes', []):
        if not isinstance(row, dict): continue
        item = {key: copy.deepcopy(row[key]) for key in allowed if key in row}
        # The coordinator names the playback state `status` and its cycle result
        # `progress_status`; the GUI uses the same shape as existing book rows.
        canonical = row.get('status')
        action = row.get('progress_status')
        if canonical in {'not_started', 'in_progress', 'finished'}:
            item['progress_status'] = canonical
            item['status'] = {'unchanged': 'ok', 'proposed': 'waiting'}.get(action, action or 'waiting')
        else:
            item['status'] = action if action in {'blocked', 'waiting'} else row.get('status', 'waiting')
            item['progress_status'] = row.get('progress_status')
        item['can_review'] = (config.get('pinepods_enabled') is True and (
            row.get('pine_reason_code') == 'duration_mismatch' or row.get('pine_status_reason_code') in {
                'variant_full_enrollment_review','variant_initial_status_conflict','variant_concurrent_status_change'}))
        item['can_edit_status'] = bool(item.get('id') and str(item['id']).startswith('_podcast:') and
            (row.get('pine_status') in {'waiting','joining'} or item['status']=='waiting' or row.get('pine_reason_code') in
             {'episode_identity_unresolved','feed_missing','episode_missing','media_identity_mismatch'}
             or row.get('pine_status_reason_code') in ('variant_identity_changed','variant_proof_stale') or row.get('reviewed_route')))
        peers = row.get('participants', [])
        if isinstance(peers, dict): peers = list(peers)
        item['participants'] = [peer for peer in peers if peer in {'abs', 'jf', 'pinepods', 'gpodder', 'bo'}] if isinstance(peers, list) else []
        pine_status = row.get('pine_status')
        mode = row.get('pine_mode')
        if isinstance(mode, str) and mode in {'position', 'status_only'}:
            item['pine_mode'] = mode
        if mode == 'status_only':
            status_reasons = {
                'variant_proof_required': 'Die abweichende Audiofassung ist noch nicht vollständig bestätigt.',
                'variant_proof_stale': 'Die abweichende Audiofassung wird vor dem Statusabgleich erneut geprüft.',
                'variant_identity_changed': 'Die Identität einer Folge hat sich geändert. Vor dem Statusabgleich ist eine Prüfung nötig.',
                'variant_initial_status_conflict': 'Die vorhandenen Anfangszustände widersprechen sich. Es wird kein Stand ungeprüft überschrieben.',
                'variant_concurrent_status_change': 'Mehrere Dienste melden widersprüchliche Statusänderungen. Vor einer Übernahme ist eine Prüfung nötig.',
                'variant_settle_wait': 'Eine neue Statusänderung wird nach einer kurzen Wartezeit erneut geprüft.',
                'variant_active_player': 'Die Folge ist gerade in einem Player aktiv. Der Statusabgleich wartet.',
                'variant_pending_review': 'Eine begonnene Statusübertragung muss vor weiteren Änderungen geprüft werden.',
                'variant_full_enrollment_review': 'Die Folge war bereits im Positionsabgleich. Der Wechsel zum Statusabgleich benötigt eine Prüfung.',
                'variant_status_disabled': 'Der Statusabgleich für verschiedene Audiofassungen ist ausgeschaltet.',
                'variant_local_position_conflict': 'Audiobookshelf und Jellyfin haben widersprüchliche lokale Zwischenstände. Der Statusabgleich wartet auf Prüfung.',
                'variant_progress_missing': 'Ein vorhandener Fortschrittsstand ist nicht mehr vollständig verfügbar. Es wird nichts zurückgesetzt.',
                'variant_core_pending': 'Der laufende Abgleich zwischen Audiobookshelf und Jellyfin wird zuerst abgeschlossen.',
            }
            status_code = row.get('pine_status_reason_code')
            if isinstance(status_code, str) and status_code in status_reasons:
                item['pine_status_reason_code'] = status_code
                item['pine_status_reason'] = status_reasons[status_code]
        if isinstance(pine_status, str) and pine_status in {'disabled', 'waiting', 'enrolled', 'joining', 'blocked'}:
            item['pine_status'] = pine_status
            code = row.get('pine_reason_code')
            # Only fixed reason codes cross this boundary; exceptions can carry
            # feed URLs or provider credentials and must not reach the dashboard.
            reasons = {
                'feed_missing': 'PinePods führt keinen Podcast unter dieser Feed-Adresse. Über „Folge prüfen“ kannst du eine vorhandene Ausgabe vergleichen.',
                'feed_ambiguous': 'Der Podcast ist in PinePods nicht eindeutig zugeordnet.',
                'episode_missing': 'Diese Folge fehlt in PinePods.',
                'episode_ambiguous': 'Diese Folge ist in PinePods nicht eindeutig zugeordnet.',
                'episode_identity_unresolved': 'Zuordnung ungeklärt: kein Treffer für die gespeicherte Audio-Adresse. Für den Abgleich ist eine eindeutige Zuordnung erforderlich. Öffne „Folge prüfen“, um den Katalogeintrag zu vergleichen.',
                'duration_mismatch': 'Die Audiodauer in PinePods stimmt nicht mit der gemeinsamen Folge überein.',
                'media_identity_mismatch': 'Die Mediendatei in PinePods konnte nicht eindeutig bestätigt werden.',
                'source_identity_missing': 'Die Ursprungsangaben dieser Folge reichen für eine PinePods-Zuordnung noch nicht aus.',
                'source_identity_ambiguous': 'Die Ursprungsangaben dieser Folge sind nicht eindeutig.',
                'core_transfer_pending': 'Eine laufende Übertragung zwischen Audiobookshelf und Jellyfin wird zuerst abgeschlossen.',
                'jellyfin_not_indexed': 'Die Folge ist noch nicht in Jellyfin eingelesen.',
            }
            if isinstance(code, str) and code in reasons:
                item['pine_reason_code'] = code
                item['pine_reason'] = reasons[code]
            elif pine_status == 'waiting':
                item['pine_reason'] = 'Die PinePods-Zuordnung ist noch nicht bestätigt.'
            elif pine_status == 'blocked':
                item['pine_reason'] = 'Der Abgleich mit PinePods benötigt eine Prüfung; diese Folge bleibt gesperrt.'
            elif pine_status == 'joining':
                item['pine_reason'] = 'Die vorhandenen Fortschritte werden vor der Aufnahme von PinePods geprüft.'
        if paused:
            item['item_blocked'] = item['status'] == 'blocked'
            item['status'] = 'paused'
        episodes.append(item)
    return dict(enabled=config.get('enabled') is True, paused=paused, at=value.get('at'),
        mapped=value.get('mapped', 0), waiting=value.get('waiting', 0), synced=value.get('synced', 0),
        blocked=[{k: copy.deepcopy(row[k]) for k in ('id', 'title', 'reason') if k in row}
                 for row in value.get('blocked', []) if isinstance(row, dict)], episodes=episodes,
        fatal='Podcast-Abgleich gesperrt. Verbindungen und Bibliotheken prüfen.' if value.get('fatal') else None,
        bookorbit='prepared', pinepods_enabled=config.get('pinepods_enabled') is True,
        pinepods_optional_enrollment=config.get('pinepods_optional_enrollment') is True,
        pinepods_variant_status_only=config.get('pinepods_variant_status_only') is True,
        primary_source=config.get('primary_source', 'auto'),
        pine_status_only=sum(row.get('pine_mode') == 'status_only' and row.get('pine_status') == 'enrolled' for row in episodes),
        pine_waiting=sum(row.get('pine_status') == 'waiting' for row in episodes),
        pine_enrolled=sum(row.get('pine_status') == 'enrolled' for row in episodes),
        pine_joining=sum(row.get('pine_status') == 'joining' for row in episodes),
        pine_blocked=sum(row.get('pine_status') == 'blocked' for row in episodes))


def participant_block(root, cfg):
    """An optional participant cannot disappear from an interrupted transfer."""
    if cfg.get('jellyfin', {}).get('enabled'): return None
    with sqlite3.connect('file:' + str(Path(root) / 'state.sqlite') + '?mode=ro', uri=True) as db:
        if db.execute("SELECT 1 FROM operations WHERE status='pending' LIMIT 1").fetchone():
            return 'Ein Abgleich ist noch nicht abgeschlossen. Jellyfin wieder verbinden und den Vorgang prüfen.'
        for key, value in db.execute("SELECT id,value FROM state WHERE id LIKE '_jellyfin_%' OR id LIKE '_bookbridge_audio:%' OR id LIKE '_podcast:%' OR id LIKE '_podcast_status:%'"):
            if json.loads(value).get('pending'):
                return 'Ein Jellyfin-Abgleich ist noch nicht abgeschlossen. Verbindung wiederherstellen oder den Vorgang prüfen.'
    return None


def run_admin_loop(args, root, config, credentials, bridge_type):
    admin = Administration(root, args.key, config, credentials, bridge_type)
    try: admin.ensure_identities()
    except Exception: pass  # Existing sync safety checks still govern operation.
    bridge, signature, next_cycle = None, None, 0
    next_preview = 0
    def serve_preview():
        nonlocal next_preview
        if time.monotonic() >= next_preview:
            next_preview=time.monotonic()+0.5
            admin.process_one(bridge,preview_only=True)
    try:
        while True:
            cfg, creds, disabled = admin.effective()
            disabled = disabled or participant_block(root, cfg)
            if disabled == 'required_connection_disabled':
                disabled = 'Audiobookshelf oder der gewählte Zuordnungsdienst ist nicht aktiv verbunden.'
            podcasts_enabled = cfg.get('podcasts', {}).get('enabled') is True
            podcast_ready = bool(podcasts_enabled and cfg.get('abs_url') and creds.get('abs_token')
                                 and cfg.get('jellyfin', {}).get('enabled') and creds.get('jellyfin_token'))
            current = hashlib.sha256(json.dumps([cfg, creds, disabled], sort_keys=True).encode()).hexdigest()
            if current != signature:
                close_bridge(bridge); bridge = None
                if not disabled or podcast_ready: bridge = bridge_type(root, cfg, creds)
                if bridge is not None:
                    bridge.serve_preview=serve_preview
                    bridge.media_guard=admin.media_guard
                    bridge.media_generation=admin.media_generation
                signature = current; next_cycle = 0
            admin.publish(bridge, disabled)
            if admin.process_one(bridge):
                # Apply changes at a cycle boundary, never in the middle of a write.
                admin.publish(bridge, disabled)
                next_cycle = 0
                continue
            if time.monotonic() >= next_cycle:
                if disabled or admin.store.paused():
                    try: previous = json.loads((Path(root) / 'last-run.json').read_text())
                    except (OSError, ValueError): previous = {}
                    previous.pop('fatal', None)
                    previous.update(at=stamp(), mode='paused', reason=disabled or 'Über die Verwaltung pausiert')
                    atomic(Path(root) / 'last-run.json', previous)
                else:
                    admin.publish(bridge, busy='Synchronisierung')
                    try:
                        bridge.cycle(args.apply, args.settle)
                    except Exception as exc:
                        message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) or type(exc).__name__ == 'RequestError' else type(exc).__name__
                        if args.apply and bridge.load('_global_error').get('error') != message:
                            bridge.alert('global', 'Hoerbuch-Sync gesperrt: ' + message)
                            bridge.save('_global_error', {'error': message})
                        if args.apply: bridge.deliver_notifications()
                        atomic(Path(root) / 'last-run.json', {'at': stamp(), 'fatal': message})
                if podcasts_enabled and not admin.store.paused():
                    if not podcast_ready or bridge is None:
                        atomic(Path(root) / 'last-podcast-run.json', {'at': stamp(), 'fatal': 'Podcast-Verbindungen sind nicht vollständig aktiv.', 'episodes': []})
                    else:
                        try:
                            import podcast_sync
                            podcast_sync.run(bridge, args.apply, args.settle)
                            if args.apply: bridge.deliver_notifications()
                        except Exception:
                            message = 'Podcast-Abgleich gesperrt. Verbindungen, Bibliotheken und Prüfprotokoll kontrollieren.'
                            atomic(Path(root) / 'last-podcast-run.json', {'at': stamp(), 'fatal': message, 'episodes': []})
                            if args.apply and bridge.load('_podcast_global_error').get('error') != message:
                                bridge.alert('podcasts', message)
                                bridge.save('_podcast_global_error', {'error': message})
                            if args.apply: bridge.deliver_notifications()
                admin.publish(bridge, disabled)
                next_cycle = time.monotonic() + cfg.get('interval_seconds', 60)
            for _ in range(4):
                time.sleep(0.5)
                if bridge is not None:serve_preview()
    finally: close_bridge(bridge)
