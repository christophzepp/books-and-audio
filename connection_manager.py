"""Encrypted connection profiles; only the control worker calls seed/mutate.

The original configuration and credential files are inputs, never write targets.
Probe callbacks must refuse redirects and validate the actual HTTP destination.
The URL checks here also reject metadata aliases resolved before each probe.
"""
import contextlib
import copy
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import uuid
from urllib.parse import urlsplit

from cryptography.fernet import Fernet


class ConnectionManagerError(ValueError):
    """A fixed code; never a credential, response body, or callback exception."""


def field(key, label, *, required=False, secret=False, kind='text'):
    return dict(key=key, label=label, type=kind, required=required, secret=secret)


SCHEMAS = {
    'audiobookshelf': dict(label='Audiobookshelf', fields=[
        field('url', 'Adresse', required=True), field('version', 'Erwartete Version', required=True),
        field('library_id', 'Hörbuch-Bibliothek'), field('token', 'API-Token', required=True, secret=True)]),
    'bookorbit': dict(label='BookOrbit', fields=[
        field('url', 'Adresse', required=True), field('username', 'Benutzername', required=True),
        field('library_ids', 'Bibliotheks-IDs', kind='json'), field('password', 'Passwort', required=True, secret=True)]),
    'bookbridge': dict(label='BookBridge', fields=[
        field('url', 'Adresse', required=True), field('username', 'Benutzername', required=True),
        field('version', 'Erwartete Version (7.11.0)', required=True),
        field('password', 'Passwort', required=True, secret=True)]),
    'jellyfin': dict(label='Jellyfin', fields=[
        field('url', 'Adresse', required=True), field('user_id', 'Benutzer-ID', required=True),
        field('username', 'Benutzername', required=True), field('version', 'Erwartete Version', required=True),
        field('audio_library_id', 'Hörbuch-Bibliothek', required=True),
        field('token', 'API-Token', required=True, secret=True)]),
    'pinepods': dict(label='PinePods · GPodder', fields=[
        field('url', 'PinePods-Adresse', required=True), field('username', 'Benutzername', required=True),
        field('device_id', 'Vorhandene GPodder-Geräte-ID', required=True),
        field('server_version', 'Erwartete PinePods-Version (geprüft: 0.9.0)', required=True),
        field('completion_margin_seconds', 'Abschlussgrenze in Sekunden (wie in PinePods)', kind='number'),
        field('password', 'GPodder-App-Passwort (ältere Versionen: bestehendes Passwort)', required=True, secret=True),
        field('api_key', 'Persönlicher PinePods-API-Schlüssel für Abschlussstatus', secret=True)]),
    'ntfy': dict(label='Benachrichtigungen', fields=[
        field('url', 'Themen-Adresse', required=True), field('authorization', 'Authorization', required=True, secret=True)]),
}
MANAGED_SECRETS = {'abs_token', 'bo_username', 'bo_password', 'jellyfin_token', 'ntfy_authorization', 'bookbridge_password', 'pinepods_password', 'pinepods_api_key'}
PROBE_KEYS = {'ok', 'server_id', 'user_id', 'username', 'version', 'library_ids', 'writers_disabled', 'device_exists', 'device_id', 'protocol', 'progress_sync_supported'}
ID = re.compile(r'[a-z][a-z0-9-]{0,79}\Z')
LIMIT = 32 * 1024 * 1024


PODCAST_DEFAULTS = dict(enabled=False, abs_library_id='', jellyfin_library_id='', pinepods_enabled=False,
                       bookorbit_enabled=False, pinepods_optional_enrollment=False,
                       pinepods_variant_status_only=False, primary_source='auto')


def podcast_settings(cfg):
    value = cfg.get('podcasts', {})
    return {key: copy.deepcopy(value.get(key, default)) for key, default in PODCAST_DEFAULTS.items()}


def variant_flag_only_enable(previous, candidate):
    """No scope, account, folder or other switch can ride this opt-in."""
    if not isinstance(previous,dict) or not isinstance(candidate,dict):return False
    old=copy.deepcopy(previous);new=copy.deepcopy(candidate)
    before=old.get('podcasts');after=new.get('podcasts')
    if not isinstance(before,dict) or not isinstance(after,dict):return False
    before.setdefault('primary_source', 'auto');after.setdefault('primary_source', 'auto')
    flag='pinepods_variant_status_only'
    if (before.get(flag,False) is not False or after.get(flag) is not True
            or any(before.get(k) is not True or after.get(k) is not True
                   for k in ('enabled','pinepods_enabled','pinepods_optional_enrollment'))):return False
    before.pop(flag,None);after.pop(flag,None)
    return old==new


def _fail(code):
    raise ConnectionManagerError(code)


def _text(value, maximum=512, *, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or (not value and not empty)
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        _fail('invalid_field')
    return value


def validate_target_address(value):
    """Private, loopback, Docker and Tailscale endpoints are intentional options."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        _fail('invalid_address')
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if (address.is_link_local or address.is_unspecified or address.is_multicast or address.is_reserved
            or str(address) in {'100.100.100.200', '168.63.129.16', 'fd00:ec2::254'}
            or isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network('0.0.0.0/8')):
        _fail('unsafe_address')
    return str(address)


def validate_url(value, *, resolve=False):
    _text(value, 2048)
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
        if (parsed.scheme not in {'http', 'https'} or not host or parsed.username is not None
                or parsed.password is not None or '?' in value or '#' in value or '\\' in value
                or any(c.isspace() for c in value) or '%' in parsed.netloc
                or port is not None and not 1 <= port <= 65535):
            _fail('invalid_url')
        host = host.lower().rstrip('.')
        if host in {'metadata', 'metadata.google.internal', 'metadata.azure.internal', 'instance-data'}:
            _fail('unsafe_address')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if (not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', host)
                    or any(not part or len(part) > 63 or part.startswith('-') or part.endswith('-')
                           for part in host.split('.'))
                    or re.fullmatch(r'[0-9.]+', host) or host.startswith('0x')
                    or any(part.startswith('0x') for part in host.split('.'))):
                _fail('invalid_url')
        else:
            validate_target_address(host)
        if resolve:
            rows = socket.getaddrinfo(host, port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
            if not rows:
                _fail('address_unavailable')
            for row in rows:
                validate_target_address(row[4][0])
    except ConnectionManagerError:
        raise
    except Exception:
        _fail('address_unavailable' if resolve else 'invalid_url')
    return value


def _validated(kind, settings, secrets, *, complete=False):
    if kind not in SCHEMAS or not isinstance(settings, dict) or not isinstance(secrets, dict):
        _fail('invalid_profile')
    fields = {f['key']: f for f in SCHEMAS[kind]['fields']}
    if (set(settings) - {k for k, f in fields.items() if not f['secret']}
            or set(secrets) - {k for k, f in fields.items() if f['secret']}):
        _fail('invalid_field')
    for key, value in settings.items():
        if key == 'library_ids':
            if (not isinstance(value, list) or len(value) > 1000 or len(value) != len(set(value))
                    or any(type(x) is not int or x <= 0 for x in value)):
                _fail('invalid_library_scope')
        elif key == 'completion_margin_seconds':
            if type(value) is not int or not 0 <= value <= 3600: _fail('invalid_field')
        elif key == 'device_id':
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value): _fail('invalid_field')
        elif key == 'url':
            validate_url(value)
        else:
            _text(value, empty=not fields[key]['required'])
    for value in secrets.values():
        _text(value, 16384, empty=True)
    if complete:
        for key, f in fields.items():
            if f['required'] and not (secrets if f['secret'] else settings).get(key):
                _fail('incomplete_profile')


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


class ConnectionManager:
    """Construct/read anywhere; seed and mutate only in the sole control worker.

    probe_callback(kind, settings, secrets) returns a dict with ok=True.
    compatibility_callback(kind, old_cfg, old_creds, new_cfg, new_creds,
                           sanitized_probe_result) must return exactly True.
    Neither callback's output or exception text is returned to the browser.
    """
    def __init__(self, root, base_config, base_credentials, key_bytes):
        self.root = Path(root) / 'control'
        self.path = self.root / 'connections.enc'
        try:
            self.cipher = Fernet(key_bytes)
            self.initial = self._initial(copy.deepcopy(base_config), copy.deepcopy(base_credentials))
        except Exception:
            raise ConnectionManagerError('connection_initialization_failed') from None

    def _initial(self, config, credentials):
        if not isinstance(config, dict) or not isinstance(credentials, dict):
            _fail('invalid_initial_configuration')
        auto, jf = config.get('auto_discovery', {}), config.get('jellyfin', {})
        rows = [
            ('audiobookshelf', {'url': config.get('abs_url'), 'version': config.get('abs_version'),
                'library_id': auto.get('abs_library_id')}, {'token': credentials.get('abs_token')}, bool(config.get('abs_url'))),
            ('bookorbit', {'url': config.get('bo_url'), 'username': credentials.get('bo_username'),
                'library_ids': auto.get('bo_library_ids')}, {'password': credentials.get('bo_password')}, bool(config.get('bo_url'))),
            ('jellyfin', {key: jf.get(key) for key in ('url', 'user_id', 'username', 'version', 'audio_library_id')},
                {'token': credentials.get('jellyfin_token')}, jf.get('enabled') is True),
            ('ntfy', {'url': config.get('ntfy_url')}, {'authorization': credentials.get('ntfy_authorization')},
                bool(config.get('ntfy_url') and credentials.get('ntfy_authorization'))),
        ]
        if config.get('bookbridge', {}).get('url'):
            bb = config['bookbridge']
            rows.append(('bookbridge', {k: bb.get(k) for k in ('url', 'username', 'version')},
                         {'password': credentials.get('bookbridge_password')}, bb.get('enabled') is True))
        if config.get('pinepods', {}).get('url'):
            pine = config['pinepods']
            rows.append(('pinepods', {k: pine.get(k) for k in ('url', 'username', 'device_id', 'completion_margin_seconds', 'server_version')},
                         {'password': credentials.get('pinepods_password'), 'api_key': credentials.get('pinepods_api_key')}, pine.get('enabled') is True))
        profiles = []
        for kind, settings, secrets, active in rows:
            settings = {k: v for k, v in settings.items() if v is not None}
            secrets = {k: v for k, v in secrets.items() if v is not None}
            if not settings.get('url'):
                continue
            _validated(kind, settings, secrets, complete=active)
            profiles.append(dict(id=kind + '-default', kind=kind, name=SCHEMAS[kind]['label'],
                                 settings=settings, secrets=secrets, active=active, identity=None))
        for key in ('abs_url', 'abs_version', 'bo_url', 'ntfy_url'):
            config.pop(key, None)
        if 'auto_discovery' in config:
            config['auto_discovery'].pop('abs_library_id', None)
            config['auto_discovery'].pop('bo_library_ids', None)
        if 'jellyfin' in config:
            for key in ('url', 'user_id', 'username', 'version', 'audio_library_id', 'enabled'):
                config['jellyfin'].pop(key, None)
        if 'bookbridge' in config:
            for key in ('url', 'username', 'version', 'enabled'):
                config['bookbridge'].pop(key, None)
        if 'pinepods' in config:
            for key in ('url', 'username', 'device_id', 'completion_margin_seconds', 'server_version', 'enabled'):
                config['pinepods'].pop(key, None)
        return self._materialize(dict(schema=1, revision=0, base_config=config,
            base_credentials={k: v for k, v in credentials.items() if k not in MANAGED_SECRETS}, profiles=profiles))

    def _materialize(self, state):
        cfg, creds = copy.deepcopy(state['base_config']), copy.deepcopy(state['base_credentials'])
        active = {p['kind']: p for p in state['profiles'] if p['active']}
        provider = cfg.get('mapping_provider', 'bookorbit')
        if provider not in {'bookorbit', 'bookbridge'}: _fail('invalid_provider')
        missing = [kind for kind in ('audiobookshelf', provider) if kind not in active]
        for kind, profile in active.items():
            s, secret = profile['settings'], profile['secrets']
            if kind == 'audiobookshelf':
                cfg.update(abs_url=s['url'], abs_version=s['version']); creds['abs_token'] = secret['token']
                if s.get('library_id'):
                    cfg.setdefault('auto_discovery', {})['abs_library_id'] = s['library_id']
            elif kind == 'bookorbit':
                cfg['bo_url'] = s['url']; creds.update(bo_username=s['username'], bo_password=secret['password'])
                if s.get('library_ids'):
                    cfg.setdefault('auto_discovery', {})['bo_library_ids'] = copy.deepcopy(s['library_ids'])
            elif kind == 'jellyfin':
                cfg.setdefault('jellyfin', {}).update(copy.deepcopy(s), enabled=True)
                creds['jellyfin_token'] = secret['token']
            elif kind == 'bookbridge':
                cfg.setdefault('bookbridge', {}).update(copy.deepcopy(s), enabled=True)
                creds['bookbridge_password'] = secret['password']
            elif kind == 'pinepods':
                cfg.setdefault('pinepods', {}).update(copy.deepcopy(s), enabled=True)
                creds['pinepods_password'] = secret['password']
                if secret.get('api_key'): creds['pinepods_api_key'] = secret['api_key']
            else:
                cfg['ntfy_url'] = s['url']; creds['ntfy_authorization'] = secret['authorization']
        if 'pinepods' in cfg and 'pinepods' not in active:
            cfg['pinepods']['enabled'] = False
        if 'jellyfin' not in active:
            cfg.setdefault('jellyfin', {})['enabled'] = False
        auto = cfg.get('auto_discovery')
        if auto and not (auto.get('abs_library_id') and auto.get('bo_library_ids')):
            auto['enabled'] = False
        state.update(effective_config=cfg, effective_credentials=creds,
                     disabled_reason='required_connection_disabled' if missing else None)
        return state

    def _directory(self, *, create=False):
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022 or info.st_uid != os.geteuid():
            _fail('unsafe_connection_directory')
        if create:
            directory = os.open(self.root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    @contextlib.contextmanager
    def _locked(self):
        self._directory(create=True)
        fd = os.open(self.root / '.connections.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
                _fail('unsafe_connection_lock')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self):
        if not self.path.exists() and not self.path.is_symlink():
            if (self.root / '.connections.seeded').exists():
                _fail('connection_store_unavailable')
            return copy.deepcopy(self.initial), None
        try:
            self._directory()
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                        or info.st_uid != os.geteuid() or not 0 < info.st_size <= LIMIT):
                    _fail('unsafe_connection_store')
                raw = handle.read(LIMIT + 1)
            state = json.loads(self.cipher.decrypt(raw))
            if (state['schema'] != 1 or type(state['revision']) is not int or state['revision'] < 0
                    or not isinstance(state['profiles'], list) or len(state['profiles']) > 100):
                _fail('invalid_connection_store')
            ids, active = set(), set()
            for p in state['profiles']:
                if not ID.fullmatch(p['id']) or p['id'] in ids or type(p['active']) is not bool:
                    _fail('invalid_connection_store')
                ids.add(p['id']); _text(p['name'], 120)
                _validated(p['kind'], p['settings'], p['secrets'], complete=p['active'])
                if p['active']:
                    if p['kind'] in active:
                        _fail('invalid_connection_store')
                    active.add(p['kind'])
            rebuilt = self._materialize(copy.deepcopy(state))
            if any(rebuilt[k] != state[k] for k in ('effective_config', 'effective_credentials', 'disabled_reason')):
                _fail('invalid_connection_store')
            return state, raw
        except Exception:
            raise ConnectionManagerError('connection_store_unavailable') from None

    def _write(self, state, previous=None):
        raw = self.cipher.encrypt(_encode(state))
        if len(raw) > LIMIT:
            _fail('connection_store_limit')
        if previous is not None:
            backup = self.root / ('connections.rev-%020d.enc' % (state['revision'] - 1))
            try:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                if backup.is_symlink() or backup.read_bytes() != previous:
                    _fail('connection_backup_conflict')
            else:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(previous); handle.flush(); os.fsync(handle.fileno())
        temporary = self.root / ('.connections-' + uuid.uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def seed(self):
        try:
            with self._locked():
                state, raw = self._read()
                if raw is None:
                    self._write(state)
                marker = self.root / '.connections.seeded'
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
                        _fail('unsafe_connection_store')
                    os.fsync(fd)
                finally:
                    os.close(fd)
                directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            return self.public()
        except Exception:
            raise ConnectionManagerError('connection_seed_failed') from None

    def public(self):
        state, _ = self._read()
        profiles = []
        for p in state['profiles']:
            profiles.append({k: copy.deepcopy(p[k]) for k in ('id', 'kind', 'name', 'active', 'settings')})
            profiles[-1]['has_secrets'] = {f['key']: bool(p['secrets'].get(f['key']))
                for f in SCHEMAS[p['kind']]['fields'] if f['secret']}
        return dict(revision=state['revision'], profiles=profiles, disabled_reason=state['disabled_reason'],
                    schemas=copy.deepcopy(SCHEMAS), mapping_provider=state['effective_config'].get('mapping_provider', 'bookorbit'),
                    podcasts=podcast_settings(state['effective_config']))

    def provider(self, payload, check_callback, *, apply=False):
        """Validate and switch only at a worker boundary. Existing states stay intact."""
        revision = None
        try:
            with self._locked():
                old, raw = self._read(); revision = old['revision']
                if (not isinstance(payload, dict) or set(payload) != {'expected_revision', 'provider'}
                        or type(payload['expected_revision']) is not int or payload['expected_revision'] != revision):
                    _fail('stale_revision')
                if payload['provider'] not in {'bookorbit', 'bookbridge'}: _fail('invalid_provider')
                state = copy.deepcopy(old)
                state['base_config']['mapping_provider'] = payload['provider']
                self._materialize(state)
                if state['disabled_reason']: _fail('required_connection_disabled')
                result = check_callback(copy.deepcopy(state['effective_config']), copy.deepcopy(state['effective_credentials']))
                if not isinstance(result, dict) or result.get('ok') is not True: _fail('provider_not_ready')
                if apply:
                    state['revision'] += 1
                    self._write(state, raw)
                return dict(ok=True, revision=state['revision'], provider=payload['provider'], applied=apply, preview=result)
        except ConnectionManagerError as exc:
            return dict(ok=False, code=str(exc), revision=revision)
        except Exception:
            return dict(ok=False, code='provider_not_ready', revision=revision)

    def podcasts(self, payload, check_callback, *, apply=False, variant_activation_check=None):
        """Podcast scope uses the same encrypted, revisioned configuration store."""
        revision = None
        try:
            with self._locked():
                old, raw = self._read(); revision = old['revision']
                if (not isinstance(payload, dict) or set(payload) != {'expected_revision', 'settings'}
                        or type(payload['expected_revision']) is not int or payload['expected_revision'] != revision):
                    _fail('stale_revision')
                requested = payload['settings']
                optional = 'pinepods_optional_enrollment'
                variant = 'pinepods_variant_status_only'
                required = set(PODCAST_DEFAULTS) - {optional, variant, 'primary_source'}
                if (not isinstance(requested, dict) or not required.issubset(requested)
                        or set(requested) - set(PODCAST_DEFAULTS)):
                    _fail('invalid_podcast_settings')
                requested = copy.deepcopy(requested)
                # Older clients preserve an explicit choice, and never opt in
                # implicitly when upgrading an existing configuration.
                requested.setdefault(optional, old['base_config'].get('podcasts', {}).get(optional, False))
                requested.setdefault(variant, old['base_config'].get('podcasts', {}).get(variant, False))
                requested.setdefault('primary_source', old['base_config'].get('podcasts', {}).get('primary_source', 'auto'))
                from podcast_policy import validate_primary
                try: validate_primary(requested['primary_source'])
                except ValueError: _fail('invalid_podcast_primary_source')
                if requested['primary_source'] == 'pinepods' and not requested['pinepods_enabled']:
                    _fail('podcast_primary_pinepods_disabled')
                for key in ('enabled', 'pinepods_enabled', 'bookorbit_enabled', optional, variant):
                    if type(requested[key]) is not bool: _fail('invalid_podcast_settings')
                if requested['bookorbit_enabled']: _fail('bookorbit_podcasts_not_ready')
                should_check = requested['enabled'] or not apply
                for key in ('abs_library_id', 'jellyfin_library_id'):
                    _text(requested[key], 512, empty=not should_check)
                state = copy.deepcopy(old)
                state['base_config'].setdefault('podcasts', {}).update(copy.deepcopy(requested))
                self._materialize(state)
                cfg = state['effective_config']
                if should_check and (not cfg.get('abs_url') or not cfg.get('jellyfin', {}).get('enabled')):
                    _fail('podcast_connections_missing')
                if should_check and requested['pinepods_enabled'] and not cfg.get('pinepods', {}).get('enabled'):
                    _fail('podcast_pinepods_missing')
                test_cfg = copy.deepcopy(cfg)
                if not apply: test_cfg['podcasts']['enabled'] = True
                if (apply and callable(variant_activation_check)
                        and old['effective_credentials']==state['effective_credentials']
                        and variant_flag_only_enable(old['effective_config'],test_cfg)):
                    result=variant_activation_check(copy.deepcopy(old['effective_config']),
                        copy.deepcopy(test_cfg),copy.deepcopy(state['effective_credentials']))
                else:
                    result = check_callback(test_cfg, copy.deepcopy(state['effective_credentials']))
                if not isinstance(result, dict) or result.get('ok') is not True: _fail('podcast_not_ready')
                if apply:
                    state['revision'] += 1
                    self._write(state, raw)
                return dict(ok=True, revision=state['revision'], applied=apply, preview=result)
        except ConnectionManagerError as exc:
            return dict(ok=False, code=str(exc), revision=revision)
        except Exception:
            return dict(ok=False, code='podcast_not_ready', revision=revision)

    def effective(self):
        state, _ = self._read()
        return copy.deepcopy(state['effective_config']), copy.deepcopy(state['effective_credentials']), state['disabled_reason']

    def _probe(self, profile, callback):
        _validated(profile['kind'], profile['settings'], profile['secrets'], complete=True)
        validate_url(profile['settings']['url'], resolve=True)
        if not callable(callback):
            _fail('probe_required')
        try:
            result = callback(profile['kind'], copy.deepcopy(profile['settings']), copy.deepcopy(profile['secrets']))
            if not isinstance(result, dict) or result.get('ok') is not True:
                _fail('probe_failed')
            safe = {k: copy.deepcopy(v) for k, v in result.items() if k in PROBE_KEYS}
            for key, value in safe.items():
                if key in {'ok', 'writers_disabled', 'device_exists', 'progress_sync_supported'}:
                    if type(value) is not bool: _fail('probe_failed')
                    continue
                if key == 'library_ids':
                    if (not isinstance(value, list) or len(value) > 1000
                            or any(not (type(v) is int and v > 0 or isinstance(v, str) and 0 < len(v) <= 512)
                                   for v in value)):
                        _fail('probe_failed')
                else:
                    _text(value, 512)
            if len(_encode(safe)) > 16384:
                _fail('probe_failed')
            return safe
        except Exception:
            raise ConnectionManagerError('probe_failed') from None

    def mutate(self, action, payload, probe_callback=None, compatibility_callback=None):
        revision = None
        try:
            with self._locked():
                old, raw = self._read(); revision = old['revision']
                if raw is None:
                    _fail('connections_not_initialized')
                if (not isinstance(payload, dict) or type(payload.get('expected_revision')) is not int
                        or payload['expected_revision'] != revision):
                    _fail('stale_revision')
                if action not in {'create', 'update', 'delete', 'test', 'activate', 'deactivate'}:
                    _fail('invalid_action')
                allowed = {'expected_revision', 'kind', 'name', 'settings', 'secrets'} if action == 'create' else {'expected_revision', 'id'}
                if action == 'update':
                    allowed |= {'name', 'settings', 'secrets'}
                if set(payload) - allowed:
                    _fail('invalid_field')
                state = copy.deepcopy(old)
                if action == 'create':
                    kind = payload.get('kind')
                    if kind not in SCHEMAS or len(state['profiles']) >= 100:
                        _fail('invalid_profile')
                    profile = dict(id=kind + '-' + uuid.uuid4().hex, kind=kind,
                        name=_text(payload.get('name'), 120), settings=copy.deepcopy(payload.get('settings', {})),
                        secrets=copy.deepcopy(payload.get('secrets', {})), active=False, identity=None)
                    _validated(kind, profile['settings'], profile['secrets'])
                    state['profiles'].append(profile)
                else:
                    profile = next((p for p in state['profiles'] if p['id'] == payload.get('id')), None)
                    if profile is None:
                        _fail('profile_not_found')
                # Disconnecting our client does not stop BookBridge's own
                # background writers. Never forget a previously activated
                # endpoint until it proves those writers are disabled.
                forget = []
                if profile['kind'] == 'bookbridge':
                    if action in {'delete', 'deactivate'} and (profile['active'] or profile.get('identity')):
                        forget.append(profile)
                    if action == 'activate':
                        forget.extend(p for p in state['profiles'] if p['kind'] == 'bookbridge' and p['active'] and p['id'] != profile['id'])
                    if action == 'update' and (profile['active'] or profile.get('identity')) and any(
                            key in payload.get('settings', {}) and payload['settings'][key] != profile['settings'].get(key)
                            for key in ('url', 'username')):
                        forget.append(profile)
                for previous in forget:
                    if self._probe(previous, probe_callback).get('writers_disabled') is not True:
                        _fail('bookbridge_writers_active')
                if action == 'update':
                    if 'name' in payload:
                        profile['name'] = _text(payload['name'], 120)
                    settings, secrets = payload.get('settings', {}), payload.get('secrets', {})
                    if not isinstance(settings, dict) or not isinstance(secrets, dict):
                        _fail('invalid_field')
                    for key, value in secrets.items():
                        if value != '':
                            profile['secrets'][key] = value
                    profile['settings'].update(settings)
                    _validated(profile['kind'], profile['settings'], profile['secrets'])
                    # An unknown blank secret must not evade the field whitelist.
                    _validated(profile['kind'], {}, secrets)
                if action in {'test', 'activate'} or action == 'update' and profile['active']:
                    proof = self._probe(profile, probe_callback)
                    if action == 'test':
                        result = dict(ok=True, code='tested', revision=revision, profile_id=profile['id'],
                                      disabled_reason=old['disabled_reason'])
                        if profile['kind'] == 'pinepods':
                            result.update(device_exists=proof.get('device_exists') is True,
                                          progress_sync_supported=proof.get('progress_sync_supported') is True)
                            if not result['device_exists']:
                                result['message'] = 'GPodder-Zugang geprüft. Bitte die angegebene Geräte-ID zuerst in PinePods anlegen.'
                            elif not result['progress_sync_supported']:
                                result['message'] = 'GPodder-Zugang geprüft. Für den aktiven Abgleich fehlen der geprüfte Abschlussstatus, ein persönlicher API-Schlüssel oder eine unterstützte Version.'
                        return result
                    if profile['kind'] == 'pinepods':
                        if proof.get('device_exists') is not True: _fail('pinepods_device_missing')
                        if proof.get('progress_sync_supported') is not True: _fail('pinepods_progress_not_supported')
                    if action == 'activate':
                        for p in state['profiles']:
                            if p['kind'] == profile['kind']:
                                p['active'] = p['id'] == profile['id']
                    profile['identity'] = proof
                    self._materialize(state)
                    try:
                        compatible = callable(compatibility_callback) and compatibility_callback(profile['kind'],
                            copy.deepcopy(old['effective_config']), copy.deepcopy(old['effective_credentials']),
                            copy.deepcopy(state['effective_config']), copy.deepcopy(state['effective_credentials']), copy.deepcopy(proof)) is True
                    except Exception:
                        compatible = False
                    if not compatible:
                        _fail('incompatible_connection')
                elif action == 'deactivate':
                    profile['active'] = False
                elif action == 'delete':
                    state['profiles'] = [p for p in state['profiles'] if p['id'] != profile['id']]
                self._materialize(state); state['revision'] += 1
                self._write(state, raw)
                return dict(ok=True, code='saved', revision=state['revision'], profile_id=profile['id'],
                            disabled_reason=state['disabled_reason'])
        except ConnectionManagerError as exc:
            return dict(ok=False, code=str(exc), revision=revision)
        except Exception:
            return dict(ok=False, code='connection_change_failed', revision=revision)
