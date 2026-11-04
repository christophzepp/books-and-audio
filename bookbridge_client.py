"""Read-only, user-scoped BookBridge 7.11 adapter.

BookBridge owns audio/ebook alignment and cross-format progress. This client
imports pair identities, never writes progress, settings, mappings or files.
The only POSTs are login and the upstream read-only saved-ABS connection test.
These are internal web APIs: refuse a different version until reviewed.
"""
import math
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests


class RequestError(Exception):
    """Fixed, sanitized failures; no credentials, URLs or upstream body text."""
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _Inputs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inputs = {}

    def handle_starttag(self, tag, attrs):
        if tag != 'input':
            return
        attrs = dict(attrs)
        name = attrs.get('name')
        # Never collect password/token fields or unrelated account information.
        if name in {'username', 'ABS_ENABLED', 'BOOKORBIT_ENABLED'} and attrs.get('type') != 'hidden':
            if name in self.inputs:
                raise ValueError('duplicate field')
            self.inputs[name] = attrs


def _text(value, maximum=2048, *, optional=False):
    if value is None and optional:
        return None
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise RequestError('BookBridge: invalid response schema')
    return value


def _number(value, *, minimum=0, maximum=None):
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value < minimum
            or maximum is not None and value > maximum):
        raise RequestError('BookBridge: invalid numeric schema')
    return value


class BookBridgeClient:
    REVIEWED_VERSION = '7.11.0'
    MAX_BOOKS = 20000
    MAX_BODY = 32 * 1024 * 1024

    def __init__(self, base_url, username, password, expected_version='7.11.0'):
        try:
            parsed = urlsplit(base_url)
            valid = (parsed.scheme in ('http', 'https') and parsed.hostname
                     and parsed.username is None and parsed.password is None
                     and not parsed.query and not parsed.fragment
                     and parsed.path in ('', '/') and '\\' not in base_url
                     and not any(c.isspace() for c in base_url))
            parsed.port
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError('Invalid BookBridge base URL')
        if not isinstance(username, str) or not username or username != username.strip():
            raise ValueError('BookBridge username is missing')
        _text(username, 512)
        if not isinstance(password, str) or not password:
            raise ValueError('BookBridge password is missing')
        if expected_version != self.REVIEWED_VERSION:
            raise ValueError('BookBridge version has not been reviewed')
        self.base_url = base_url.rstrip('/')
        self.username = username
        self._password = password
        self.expected_version = expected_version
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})
        self._authenticated = False
        self._csrf = None
        self._verified_version = None

    def close(self):
        self.session.close()
        self._password = ''
        self._csrf = None
        self._authenticated = False

    def _send(self, method, path, *, data=None, payload=None, csrf=False):
        headers = {'X-CSRF-Token': self._csrf} if csrf else None
        try:
            response = self.session.request(
                method, self.base_url + path, data=data, json=payload,
                headers=headers, timeout=(5, 20), allow_redirects=False, verify=True)
        except requests.RequestException:
            raise RequestError('BookBridge: transport failure') from None
        return response

    def _json(self, response):
        try:
            if len(response.content) > self.MAX_BODY:
                raise ValueError()
            result = response.json()
        except (ValueError, TypeError):
            raise RequestError('BookBridge: invalid JSON response', response.status_code) from None
        if not isinstance(result, dict):
            raise RequestError('BookBridge: invalid object schema', response.status_code)
        return result

    def _html(self, response):
        if response.status_code != 200:
            raise RequestError('BookBridge: account page unavailable', response.status_code)
        if len(response.content) > self.MAX_BODY:
            raise RequestError('BookBridge: account page too large')
        return response.text

    def version(self):
        response = self._send('GET', '/api/health')
        if response.status_code != 200:
            raise RequestError('BookBridge: health check failed', response.status_code)
        data = self._json(response)
        if data.get('ok') is not True or data.get('version') != self.expected_version:
            raise RequestError('BookBridge: unsupported server version')
        self._verified_version = data['version']
        return self._verified_version

    def _login(self):
        self._authenticated = False
        self._csrf = None
        self.version()
        # A stale authenticated session would need CSRF for POST /login.
        self.session.cookies.clear()
        response = self._send('POST', '/login', data={'username': self.username, 'password': self._password})
        if response.status_code != 302 or response.headers.get('Location') != '/':
            raise RequestError('BookBridge: login failed', response.status_code)
        account = self._html(self._send('GET', '/account'))
        parser = _Inputs()
        try:
            parser.feed(account)
            username = parser.inputs['username'].get('value')
            tokens = re.findall(r'var t = "([A-Za-z0-9_-]{32,128})";', account)
        except (ValueError, KeyError):
            raise RequestError('BookBridge: account schema changed') from None
        if username != self.username or len(tokens) != 1:
            raise RequestError('BookBridge: account identity not verified')
        self._csrf = tokens[0]
        self._authenticated = True

    def _request(self, method, path, *, payload=None):
        if not self._authenticated:
            self._login()
        response = self._send(method, path, payload=payload, csrf=method != 'GET')
        if response.status_code == 401 and method == 'GET':
            self._login()
            response = self._send(method, path, payload=payload)
        if response.status_code != 200:
            if response.status_code == 401:
                self._authenticated = False
            raise RequestError('BookBridge: API request failed', response.status_code)
        return self._json(response)

    @staticmethod
    def _rows(data, key):
        rows = data.get(key)
        if (not isinstance(rows, list) or len(rows) > BookBridgeClient.MAX_BOOKS
                or any(not isinstance(row, dict) for row in rows)):
            raise RequestError('BookBridge: invalid inventory schema')
        seen = set()
        for row in rows:
            identity = _text(row.get('abs_id'), 255)
            if not identity or identity in seen:
                raise RequestError('BookBridge: duplicate or missing mapping identity')
            seen.add(identity)
        return rows, seen

    @classmethod
    def _mapping(cls, row):
        mode = row.get('sync_mode')
        if mode not in {'audiobook', 'audiobook_only', 'ebook_only'}:
            raise RequestError('BookBridge: unsupported mapping mode')
        source = _text(row.get('audio_source'), 100, optional=True)
        source_id = _text(row.get('audio_source_id'), 255, optional=True)
        abs_id = row['abs_id'] if source == 'ABS' and mode != 'ebook_only' else None
        if abs_id and source_id != abs_id:
            raise RequestError('BookBridge: inconsistent ABS mapping identity')
        duration = _number(row.get('audio_duration') or row.get('duration') or 0)
        state_rows = row.get('states')
        if not isinstance(state_rows, dict) or len(state_rows) > 100:
            raise RequestError('BookBridge: invalid progress schema')
        states = {}
        for key, state in state_rows.items():
            if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', key) or not isinstance(state, dict):
                raise RequestError('BookBridge: invalid progress schema')
            states[key] = {
                'timestamp': _number(state.get('timestamp') or 0),
                'percentage': _number(state.get('percentage') or 0, maximum=100),
                'last_updated': _number(state.get('last_updated') or 0),
                'xpath': _text(state.get('xpath'), 16384, optional=True),
                'cfi': _text(state.get('cfi'), 16384, optional=True),
            }
        ready = row.get('readalong_ready', False)
        if not isinstance(ready, bool):
            raise RequestError('BookBridge: invalid alignment status')
        return {
            'id': row['abs_id'], 'abs_id': abs_id, 'audio_source': source,
            'audio_source_id': source_id, 'title': _text(row.get('display_title') or row.get('abs_title') or '', 1000),
            'ebook_path': _text(row.get('original_ebook_filename') or row.get('ebook_filename'), 4096, optional=True),
            'ebook_source': _text(row.get('ebook_source'), 100, optional=True),
            'ebook_source_id': _text(row.get('ebook_source_id'), 512, optional=True),
            'kosync_doc_id': _text(row.get('kosync_doc_id'), 255, optional=True),
            'status': _text(row.get('status'), 100), 'sync_mode': mode,
            'duration': duration, 'alignment_ready': ready, 'states': states,
        }

    def inventory(self):
        """Cross-check two current-user inventories; never infer removals from errors."""
        data = self._request('GET', '/api/status')
        rows, identities = self._rows(data, 'mappings')
        claims = self._request('GET', '/api/me/books')
        claim_rows, claim_ids = self._rows(claims, 'books')
        if (type(claims.get('total')) is not int or claims['total'] != len(claim_rows)
                or claim_ids != identities):
            raise RequestError('BookBridge: inventory changed or is incomplete')
        return [self._mapping(row) for row in rows]

    mappings = inventory

    def integration_status(self):
        if not self._authenticated:
            self._login()
        html = self._html(self._send('GET', '/account/integrations'))
        parser = _Inputs()
        try:
            parser.feed(html)
            switches = {key: parser.inputs[key] for key in ('ABS_ENABLED', 'BOOKORBIT_ENABLED')}
        except (ValueError, KeyError):
            raise RequestError('BookBridge: integration schema changed') from None
        def enabled(key):
            return 'checked' in switches[key] and 'disabled' not in switches[key]
        # This upstream endpoint only checks the saved token. It never saves
        # posted fields; the empty body prevents credential overrides.
        result = self._request('POST', '/api/account/test-connection/abs', payload={})
        message = result.get('message')
        match = re.fullmatch(r"Connected as '([^\r\n]{1,512})'", message) if isinstance(message, str) else None
        if result.get('ok') is not True or match is None:
            raise RequestError('BookBridge: saved Audiobookshelf connection not verified')
        return {'abs_username': _text(match.group(1), 512),
                'abs_enabled': enabled('ABS_ENABLED'), 'bookorbit_enabled': enabled('BOOKORBIT_ENABLED')}

    def probe(self):
        mappings = self.inventory()
        result = {'ok': True, 'username': self.username, 'version': self._verified_version,
                  'mappings_count': len(mappings),
                  'abs_mappings_count': sum(bool(row['abs_id']) for row in mappings)}
        result.update(self.integration_status())
        return result
