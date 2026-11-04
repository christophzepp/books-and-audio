"""Restricted PinePods gpodder.net-v2 episode progress adapter.

No subscriptions, downloads, account creation, device creation or media requests.
The coordinator owns durable plans, concurrency checks and per-action readback.
Gpodder has whole-second positions and no independent completion boolean.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import math
import re
from urllib.parse import quote, urlsplit

import requests


class RequestError(Exception):
    """Fixed failure text: no credentials, feed URLs or upstream response body."""
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class EpisodeUnavailable(RequestError):
    """Media cannot be admitted; never an auth/transport/progress failure.

Only the coordinator may decide whether a never-enrolled episode can keep its
existing ABS/Jellyfin participants while PinePods waits. Existing PinePods
baselines and interrupted operations must never be dropped on this exception.
"""
    REASONS = {
        'feed_missing': 'PinePods: matching feed is absent',
        'feed_ambiguous': 'PinePods: matching feed is ambiguous',
        'episode_missing': 'PinePods: matching episode is absent',
        'episode_ambiguous': 'PinePods: matching episode is ambiguous',
        'episode_identity_unresolved': 'PinePods: exact episode identity is unresolved because an enclosure is empty',
        'duration_mismatch': 'PinePods: episode duration is unavailable or differs',
        'media_identity_mismatch': 'PinePods: native media identity differs',
    }

    def __init__(self, reason):
        if reason not in self.REASONS:
            raise ValueError('Invalid PinePods media incompatibility reason')
        super().__init__(self.REASONS[reason])
        self.reason = reason


def _text(value, maximum=8192, *, empty=False):
    if (not isinstance(value, str) or len(value) > maximum
            or not empty and not value or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('Invalid PinePods text value')
    return value


def _number(value, minimum=0, maximum=2_147_483_647):
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise ValueError('Invalid PinePods numeric value')
    return value


def _url(value):
    _text(value)
    try:
        p = urlsplit(value)
        valid = p.scheme in {'http', 'https'} and p.hostname and not p.fragment and not p.username and not p.password
        p.port
    except ValueError:
        valid = False
    if not valid or '\\' in value or any(c.isspace() for c in value):
        raise ValueError('Invalid PinePods URL')
    return p


def _private_host(host):
    try:
        ip = ipaddress.ip_address(host)
        return (ip.is_loopback or ip.is_private or ip in ipaddress.ip_network('100.64.0.0/10')) and not ip.is_link_local
    except ValueError:
        return host == 'localhost' or '.' not in host or host.endswith('.local')


def action_epoch(value):
    """Whole UTC seconds; naive gpodder timestamps mean UTC, never local time."""
    if isinstance(value, dict):
        value = value.get('timestamp')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(_number(value, maximum=253_402_300_799))
    if not isinstance(value, str) or not value:
        raise ValueError('Invalid PinePods action timestamp')
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        result = parsed.timestamp()
        return int(_number(result, maximum=253_402_300_799))
    except (ValueError, OverflowError, OSError):
        raise ValueError('Invalid PinePods action timestamp') from None


def _timestamp(value):
    return dt.datetime.fromtimestamp(action_epoch(value), dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


class PinepodsClient:
    MAX_BODY = 32 * 1024 * 1024
    # Released Go 0.9.0 truncates at 25,000 without a total count. Never interpret
    # a possibly truncated response as absence; require a narrower feed query.
    MAX_ACTIONS = 24_999
    PROTOCOL = 'gpodder-v2'

    def __init__(self, base_url, username, password, device_id='book-progress-sync', completion_margin_seconds=60,
                 api_key=None, server_version='unverified'):
        parsed = _url(base_url)
        if parsed.query or parsed.path not in ('', '/'):
            raise ValueError('PinePods base URL must be the server origin')
        if parsed.scheme == 'http' and not _private_host(parsed.hostname):
            raise ValueError('Public PinePods connections require HTTPS')
        _text(username, 255)
        if username.strip() != username or ':' in username or '/' in username or username in ('.', '..'):
            raise ValueError('Invalid PinePods username')
        _text(password, 4096)
        _text(device_id, 100)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', device_id):
            raise ValueError('Invalid PinePods device identifier')
        margin = _number(completion_margin_seconds, maximum=3600)
        if int(margin) != margin:
            raise ValueError('PinePods completion margin must be whole seconds')
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.device_id = device_id
        # PinePods current source uses sixty seconds when the setting is off/zero.
        self.completion_margin_seconds = int(margin) or 60
        self.server_version = server_version
        self._api_key = _text(api_key, 4096) if api_key is not None else None
        self._native_user_id = None
        self._native_ids = {}
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.auth = (username, password)
        self.session.headers.update({'Accept': 'application/json', 'User-Agent': 'book-progress-sync/1'})
        self._user_path = quote(username, safe='') + '.json'

    @property
    def identity(self):
        return {'base_url': self.base_url, 'username': self.username,
                'device_id': self.device_id, 'protocol': self.PROTOCOL,
                'server_version': self.server_version,
                'native_user_id': self._native_user_id,
                'completion_margin_seconds': self.completion_margin_seconds}

    @property
    def identity_fingerprint(self):
        return _digest(self.identity)

    def close(self):
        self.session.auth = None
        self.session.cookies.clear()
        self.session.close()
        self._api_key = None
        self._native_user_id = None

    def _request(self, method, path, *, params=None, payload=None, native=False):
        try:
            r = self.session.request(method, self.base_url + path, params=params, json=payload,
                                     headers={'Api-Key': self._api_key} if native else None,
                                     auth=() if native else None,
                                     timeout=(5, 20), allow_redirects=False, verify=True)
        except (requests.RequestException, UnicodeError):
            raise RequestError('PinePods: transport failure') from None
        if r.status_code != 200:
            raise RequestError('PinePods: API request failed', r.status_code)
        try:
            if len(r.content) > self.MAX_BODY:
                raise ValueError()
            return r.json()
        except (ValueError, TypeError):
            raise RequestError('PinePods: invalid JSON response', r.status_code) from None

    def devices(self):
        data = self._request('GET', '/api/2/devices/' + self._user_path)
        try:
            if not isinstance(data, list) or len(data) > 10000:
                raise ValueError()
            seen = set()
            for row in data:
                if not isinstance(row, dict):
                    raise ValueError()
                name = _text(row.get('id'), 255)
                if name in seen:
                    raise ValueError()
                seen.add(name)
            return seen
        except ValueError:
            raise RequestError('PinePods: invalid device inventory') from None

    def actions(self, feed_url=None):
        params = {'since': 0, 'aggregated': 'false'}
        if feed_url is not None:
            _url(feed_url)
            params['podcast'] = feed_url
        data = self._request('GET', '/api/2/episodes/' + self._user_path, params=params)
        try:
            if not isinstance(data, dict) or not isinstance(data.get('actions'), list):
                raise ValueError()
            _number(data.get('timestamp'), maximum=253_402_300_799)
            if len(data['actions']) > self.MAX_ACTIONS:
                raise ValueError()
            for row in data['actions']:
                if not isinstance(row, dict):
                    raise ValueError()
                _url(row.get('podcast'))
                _url(row.get('episode'))
                _text(row.get('action'), 30)
                action_epoch(row)
                if row.get('device') is not None:
                    _text(row['device'], 255, empty=True)
                if row.get('guid') is not None:
                    _text(row['guid'], 8192)
                if row['action'] == 'play':
                    for key in ('started', 'position', 'total'):
                        _number(row.get(key))
                    if row['total'] <= 0 or row['started'] > row['position']:
                        raise ValueError()
            return data
        except ValueError:
            raise RequestError('PinePods: invalid episode action inventory') from None

    def probe(self):
        """Authenticated own-user GETs only, with no session or device creation."""
        devices = self.devices()
        actions = self.actions()
        supported = False
        if self._api_key and self.server_version == '0.9.0':
            self._ensure_native()
            self._verify_native_import_disabled()
            supported = True
        return {'username': self.username, 'device_id': self.device_id,
                'device_exists': self.device_id in devices, 'protocol': self.PROTOCOL,
                'actions': len(actions['actions']),
                'server_version': self.server_version, 'progress_sync_supported': supported,
                'native_import_disabled': supported,
                'completion_margin_seconds': self.completion_margin_seconds}

    def _verify_native_import_disabled(self):
        """Re-read the personal setting; never cache absence of another writer.

        Official 0.9's native importer ignores newer `new` reset events and can
        reapply an earlier completion. The Go server stays enabled with the
        personal native import URL unset; credentials/devices remain intact.
        """
        status = self._request('GET', '/api/data/gpodder/status', native=True)
        if (not isinstance(status, dict) or status.get('sync_type') != 'gpodder'
                or status.get('gpodder_enabled') is not True
                or status.get('external_enabled') is not False
                or 'external_url' not in status or status['external_url'] is not None):
            raise RequestError('PinePods: native background import must be disabled while the internal gpodder server stays enabled')

    def _read_gpodder(self, feed_url, enclosure_url, duration, guid=None, *, verified_native=None):
        _url(feed_url)
        _url(enclosure_url)
        duration = _number(duration, minimum=0.001)
        if guid is not None:
            _text(guid)
        actions = self.actions(feed_url)['actions']
        candidates = []
        for row in actions:
            if row['podcast'] != feed_url or row['episode'] != enclosure_url:
                continue
            if guid and row.get('guid') and row['guid'] != guid:
                raise RequestError('PinePods: episode identity mismatch')
            if row['action'] not in {'play', 'new'}:
                continue
            # Reading all action types, rather than aggregated=true, is necessary:
            # a later download action must never hide the latest listening action.
            candidates.append(row)
        if not candidates:
            return {'position': 0, 'status': 'not_started', 'missing': True,
                    'revision': _digest(['missing', feed_url, enclosure_url]),
                    'updated_at': None, 'action': None, 'device': None, 'raw_position': None}
        timestamp = max(action_epoch(row) for row in candidates)
        latest = [row for row in candidates if action_epoch(row) == timestamp]
        normalized = []
        for row in latest:
            if row['action'] == 'new':
                state = {'position': 0, 'status': 'not_started'}
            else:
                position = row['position']
                # Only the newest listening actions describe the current
                # receipt. Old structurally valid durations do not override a
                # newer reset/play. A finished historical duration is usable
                # solely when the freshly verified native episode independently
                # retains exactly that same completed position. Never scale an
                # in-progress position or weaken a write acknowledgement.
                historical_completion = (isinstance(verified_native, dict)
                    and verified_native.get('completed') is True
                    and verified_native.get('position') == position == row['total']
                    and row['total'] > 0
                    and type(verified_native.get('duration')) in (int, float)
                    and math.isfinite(verified_native['duration'])
                    and verified_native['duration'] > 0
                    and abs(verified_native['duration'] - duration) <= 2)
                if abs(row['total'] - duration) > 2 and not historical_completion:
                    raise RequestError('PinePods: episode duration mismatch')
                if position > duration + 2 and not historical_completion:
                    raise RequestError('PinePods: episode position exceeds duration')
                finished = position >= row['total'] - self.completion_margin_seconds
                state = {'position': duration if finished else min(position, duration),
                         'status': 'finished' if finished else 'in_progress'}
            normalized.append((row, state))
        if len({_digest(state) for _, state in normalized}) != 1:
            raise RequestError('PinePods: simultaneous episode actions conflict')
        row, state = normalized[-1]
        return {**state, 'missing': False, 'revision': _digest(sorted(_digest(r) for r, _ in normalized)),
                'updated_at': timestamp, 'action': row['action'], 'device': row.get('device'),
                'raw_position': 0 if row['action'] == 'new' else row['position']}

    def _ensure_native(self):
        if self.server_version != '0.9.0' or not self._api_key:
            raise RequestError('PinePods: native API verification is required for progress synchronization')
        if self._native_user_id is not None:
            return self._native_user_id
        account = self._request('GET', '/api/data/get_user', native=True)
        try:
            if not isinstance(account, dict) or account.get('status') != 'success':
                raise ValueError()
            user_id = account.get('retrieved_id')
            if type(user_id) is not int or user_id <= 1:
                # User 1 is the special broad web key, never an individual reader.
                raise ValueError()
            details = self._request('GET', '/api/data/user_details_id/' + str(user_id), native=True)
            if (not isinstance(details, dict) or details.get('UserID') != user_id
                    or not isinstance(details.get('Username'), str)
                    or details['Username'].casefold() != self.username.casefold()):
                raise ValueError()
        except ValueError:
            raise RequestError('PinePods: personal native API account is not verified') from None
        self._native_user_id = user_id
        return user_id

    def _native_episode(self, feed_url, enclosure_url, duration, *, _inspection=False):
        _url(feed_url)
        _url(enclosure_url)
        user_id = self._ensure_native()
        key = (feed_url, enclosure_url)
        resolved_podcast_id = None
        if key not in self._native_ids:
            pods = self._request('GET', '/api/data/return_pods/' + str(user_id), native=True)
            try:
                rows = pods['pods']
                if not isinstance(rows, list) or len(rows) > 10000 or any(not isinstance(p, dict) for p in rows):
                    raise ValueError()
                ids = set()
                for pod in rows:
                    ident = pod.get('podcastid')
                    if type(ident) is not int or ident <= 0 or ident in ids:
                        raise ValueError()
                    ids.add(ident)
                    _url(pod.get('feedurl'))
                matches = [p for p in rows if p.get('feedurl') == feed_url]
            except (KeyError, TypeError, ValueError):
                raise RequestError('PinePods: native feed inventory is invalid') from None
            if not matches:
                raise EpisodeUnavailable('feed_missing')
            if len(matches) > 1:
                raise EpisodeUnavailable('feed_ambiguous')
            podcast_id = matches[0]['podcastid']
            resolved_podcast_id = podcast_id
            offset, expected_total, matches, seen = 0, None, [], set()
            empty_urls = 0
            while True:
                data = self._request('GET', '/api/data/podcast_episodes', native=True,
                                     params={'user_id': user_id, 'podcast_id': podcast_id, 'limit': 200,
                                             # 0.9.0 limits each SQL union branch DESC
                                             # before the outer sort/offset. ASC repeats
                                             # earlier pages as that inner limit grows.
                                             'offset': offset, 'sort_by': 'date', 'sort_order': 'desc', 'filter': 'all'})
                try:
                    rows, total = data['episodes'], data['total']
                    if (not isinstance(rows, list) or type(total) is not int or total < 0 or total > 100000
                            or expected_total is not None and total != expected_total or len(rows) > 200
                            or offset + len(rows) > total):
                        raise ValueError()
                    expected_total = total
                    for row in rows:
                        episode_id = row.get('Episodeid')
                        if type(episode_id) is not int or episode_id <= 0 or episode_id in seen:
                            raise ValueError()
                        episode_url = row.get('Episodeurl')
                        if episode_url == '':
                            # PinePods 0.9.0 may retain an unrelated historical
                            # row with an empty enclosure. Never normalize it,
                            # infer its identity or treat it as a target match.
                            empty_urls += 1
                        else:
                            _url(episode_url)
                        seen.add(episode_id)
                        if episode_url == enclosure_url:
                            matches.append(episode_id)
                    offset += len(rows)
                    if offset == total:
                        break
                    if not rows:
                        raise ValueError()
                except (KeyError, TypeError, ValueError, AttributeError):
                    raise RequestError('PinePods: episode inventory is incomplete or changed') from None
            if not matches:
                if empty_urls:
                    # Complete IDs plus empty historical enclosures cannot prove
                    # absence or identity. Keep this distinct from missing: the
                    # coordinator may visibly defer a never-enrolled optional
                    # participant, but must retain every existing participant.
                    raise EpisodeUnavailable('episode_identity_unresolved')
                raise EpisodeUnavailable('episode_missing')
            if len(matches) > 1:
                raise EpisodeUnavailable('episode_ambiguous')
            self._native_ids[key] = matches[0]
        episode_id = self._native_ids[key]
        # This upstream POST only reads metadata; it changes no episode state.
        data = self._request('POST', '/api/data/get_episode_metadata', native=True,
                             payload={'episode_id': episode_id, 'user_id': user_id,
                                      'person_episode': False, 'is_youtube': False})
        try:
            row = data['episode']
            if (not isinstance(row, dict) or type(row.get('episodeid')) is not int or row['episodeid'] <= 0
                    or type(row.get('is_youtube')) is not bool or type(row.get('completed')) is not bool):
                raise ValueError()
            _url(row.get('feedurl'))
            _url(row.get('episodeurl'))
            total = _number(row.get('episodeduration'))
            position = row.get('listenduration')
            if position is not None:
                _number(position)
                # Completion is an explicit native boolean, independent of the
                # historical listen-duration row. A later duration refresh can
                # leave that old value beyond the current end. Keep it raw for
                # conflict/receipt checks; only unfinished positions need the
                # playable-range bound. Media-duration matching still follows.
                if not _inspection and row['completed'] is False and position > (total if total > 0 else duration) + 2:
                    raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise RequestError('PinePods: native episode metadata or progress is invalid') from None
        if (row['episodeid'] != episode_id or row['feedurl'] != feed_url
                or row['episodeurl'] != enclosure_url or row['is_youtube'] is not False):
            raise EpisodeUnavailable('media_identity_mismatch')
        if _inspection:
            # A separate repair coordinator may inspect invalid duration metadata,
            # but cannot use this object as a playback snapshot. The normal read
            # path below retains its duration/unfinished-position protections.
            if (type(row.get('is_downloaded')) is not bool
                    or type(row.get('podcastid')) is not int or row['podcastid'] <= 0
                    or row['podcastid'] != resolved_podcast_id):
                raise RequestError('PinePods: repair metadata is incomplete')
            return {'episode_id': episode_id, 'user_id': user_id, 'duration': total,
                    'position': position, 'completed': row['completed'],
                    'downloaded': row['is_downloaded'], 'podcast_id': row['podcastid'],
                    'expected_duration': duration, 'metadata': dict(row),
                    'account': dict(self.identity)}
        if total == 0 or abs(total - duration) > 2:
            raise EpisodeUnavailable('duration_mismatch')
        return {'episode_id': episode_id, 'user_id': user_id, 'duration': total,
                'position': position, 'completed': row['completed']}

    def inspect_episode_metadata(self, feed_url, enclosure_url, duration):
        """Fresh, read-only ownership/identity evidence for a separate repair.

        This deliberately does not normalize progress or satisfy read_episode.
        The official metadata endpoint filters by the personal owning user.
        """
        _url(feed_url); _url(enclosure_url)
        duration = _number(duration, minimum=0.001)
        previous_user = self._native_user_id
        self._native_user_id = None
        self._native_ids.pop((feed_url, enclosure_url), None)
        try:
            inspected = self._native_episode(feed_url, enclosure_url, duration, _inspection=True)
            if previous_user is not None and inspected['user_id'] != previous_user:
                raise RequestError('PinePods: personal native account changed during metadata inspection')
            return inspected
        except Exception:
            # An unsuccessful re-probe must never replace the identity already
            # pinned by the coordinator, including on a later candidate in the
            # same cycle or an error after the account endpoint succeeded.
            self._native_user_id = previous_user
            self._native_ids.clear()
            raise

    def write_episode_duration(self, expected, new_duration):
        """One official metadata POST after fresh evidence; caller journals first.

        No retries and no progress writes. Unlike our read endpoint, upstream's
        duration endpoint itself does not enforce personal episode ownership.
        """
        if not isinstance(expected, dict) or not isinstance(expected.get('metadata'), dict):
            raise ValueError('Invalid PinePods duration repair evidence')
        if type(new_duration) is not int or not 1 <= new_duration <= 172800:
            raise ValueError('Invalid PinePods measured duration')
        duration = _number(expected.get('expected_duration'), minimum=0.001)
        if abs(new_duration - duration) > 2:
            raise ValueError('PinePods measured duration differs from shared media')
        row = expected['metadata']
        current = self.inspect_episode_metadata(row.get('feedurl'), row.get('episodeurl'), duration)
        if (current != expected or current['downloaded']
                or not current['completed'] and (current['position'] or 0) > 0):
            raise RequestError('PinePods: duration repair evidence changed or episode is active')
        self._verify_native_import_disabled()
        data = self._request('POST', '/api/data/update_episode_duration', native=True,
                             payload={'episode_id': current['episode_id'],
                                      'new_duration': new_duration, 'is_youtube': False})
        if data != {'detail': 'Episode duration updated to ' + str(new_duration)}:
            raise RequestError('PinePods: duration repair acknowledgement requires readback')
        return {'accepted': True}

    def read_episode(self, feed_url, enclosure_url, duration, guid=None):
        """Actual PinePods state plus the internal gpodder action receipt."""
        _url(feed_url)
        _url(enclosure_url)
        duration = _number(duration, minimum=0.001)
        native = self._native_episode(feed_url, enclosure_url, duration)
        receipt = self._read_gpodder(feed_url, enclosure_url, duration, guid, verified_native=native)
        status = 'finished' if native['completed'] else 'in_progress' if (native['position'] or 0) > 0 else 'not_started'
        position = duration if native['completed'] else native['position'] or 0
        return {'position': position, 'status': status,
                'missing': native['position'] is None and not native['completed'] and receipt['missing'],
                'revision': _digest({'native': native, 'gpodder': receipt['revision']}),
                'updated_at': receipt['updated_at'], 'action': receipt['action'], 'device': receipt['device'],
                'native_episode_id': native['episode_id'], 'native_user_id': native['user_id'],
                'native_position': native['position'], 'native_completed': native['completed'],
                'gpodder_position': receipt['position'], 'gpodder_status': receipt['status'],
                'gpodder_missing': receipt['missing'], 'gpodder_revision': receipt['revision'],
                'gpodder_raw_position': receipt['raw_position']}

    def _read_variant_gpodder(self, feed_url, enclosure_url):
        """Status of this exact URL's own actions; never project their seconds.

        A publisher may have changed its ad cut since a historical action. Its
        own position/total describe a status, not a position on another cut.
        The ordinary position reader above deliberately remains unchanged.
        """
        rows = [row for row in self.actions(feed_url)['actions']
                if row['podcast'] == feed_url and row['episode'] == enclosure_url
                and row['action'] in ('play', 'new')]
        if not rows:
            return dict(status='not_started', missing=True, updated_at=None, action=None,
                        device=None, raw_position=None, raw_total=None,
                        revision=_digest(['missing', feed_url, enclosure_url]))
        timestamp = max(action_epoch(row) for row in rows)
        latest = [row for row in rows if action_epoch(row) == timestamp]
        states = []
        for row in latest:
            if row['action'] == 'new':
                state = dict(status='not_started', raw_position=0, raw_total=None)
            else:
                position, total = _number(row['position']), _number(row['total'], minimum=0.001)
                if position > total:
                    raise RequestError('PinePods: variant action position exceeds its own total')
                if total <= self.completion_margin_seconds and position < total:
                    raise RequestError('PinePods: variant action is too short for an unambiguous completion status')
                state = dict(status='finished' if position >= total - self.completion_margin_seconds else 'in_progress',
                             raw_position=position, raw_total=total)
            states.append(dict(state, action=row['action'], device=row.get('device')))
        if len({_digest(state) for state in states}) != 1:
            raise RequestError('PinePods: simultaneous variant status actions conflict')
        return dict(states[-1], missing=False, updated_at=timestamp,
                    revision=_digest(sorted(_digest(row) for row in latest)))

    def read_variant_status(self, feed_url, enclosure_url, own_duration, *, expected_episode_id, expected_user_id):
        """Read a proven different cut using its own verified duration only.

        Eligibility/media evidence belongs to the separate status coordinator.
        This method independently proves exact personal native identity, rejects
        cached downloads, and retains native and gpodder positions as receipts.
        """
        _url(feed_url); _url(enclosure_url)
        duration = _number(own_duration, minimum=1, maximum=172800)
        if (type(expected_episode_id) is not int or expected_episode_id <= 0
                or type(expected_user_id) is not int or expected_user_id <= 1):
            raise ValueError('Invalid PinePods variant identity')
        native = self.inspect_episode_metadata(feed_url, enclosure_url, duration)
        if (native['episode_id'] != expected_episode_id or native['user_id'] != expected_user_id
                or native['downloaded']):
            raise RequestError('PinePods: variant identity or media source changed')
        receipt = self._read_variant_gpodder(feed_url, enclosure_url)
        status = 'finished' if native['completed'] else 'in_progress' if (native['position'] or 0) > 0 else 'not_started'
        return dict(position=native['position'] or 0, status=status,
                    missing=native['position'] is None and not native['completed'] and receipt['missing'],
                    revision=_digest({'native':native, 'gpodder':receipt['revision']}),
                    own_duration=duration, native_duration=native['duration'],
                    native_episode_id=native['episode_id'], native_user_id=native['user_id'],
                    native_position=native['position'], native_completed=native['completed'],
                    native_revision=_digest(native),
                    native_observed_revision=_digest({key:value for key,value in native.items() if key!='expected_duration'}),
                    gpodder_status=receipt['status'], gpodder_missing=receipt['missing'],
                    gpodder_raw_position=receipt['raw_position'], gpodder_raw_total=receipt['raw_total'],
                    gpodder_revision=receipt['revision'], updated_at=receipt['updated_at'],
                    action=receipt['action'], device=receipt['device'])

    def prepare_status_actions(self, feed_url, enclosure_url, target_status, own_duration, timestamp, *,
                               previous, expected_episode_id, expected_user_id):
        """Plan only own-cut markers; caller must durably journal each step.

        A real intermediate position is preserved when representable. A fresh
        start or reopening from an old end uses the authorized one-second
        marker. Gpodder completion necessarily uses this cut's own endpoint.
        """
        duration = _number(own_duration, minimum=1, maximum=172800)
        _url(feed_url); _url(enclosure_url); epoch = action_epoch(timestamp)
        if target_status not in ('not_started', 'in_progress', 'finished'):
            raise ValueError('Invalid PinePods variant target status')
        if (not isinstance(previous, dict) or previous.get('own_duration') != duration
                or previous.get('native_episode_id') != expected_episode_id
                or previous.get('native_user_id') != expected_user_id
                or type(expected_episode_id) is not int or expected_episode_id <= 0
                or type(expected_user_id) is not int or expected_user_id <= 1
                or type(previous.get('native_completed')) is not bool
                or previous.get('gpodder_status') not in ('not_started','in_progress','finished')):
            raise ValueError('Invalid PinePods variant planning snapshot')
        position = previous.get('native_position')
        if position is not None: _number(position)
        completed = previous['native_completed']; gstatus = previous['gpodder_status']
        total = math.ceil(duration); plan = []
        base = dict(podcast=feed_url, episode=enclosure_url, device=self.device_id,
                    _variant_status='v1', _episode_id=expected_episode_id,
                    _user_id=expected_user_id, _own_duration=duration)
        def add(kind, value=None):
            nonlocal position, completed, gstatus
            out = dict(base, timestamp=_timestamp(epoch + len(plan)))
            if kind == 'uncompleted':
                out.update(action='new', _native_action='uncompleted')
                position = 0 if position is not None else None; completed = False
            elif kind == 'completed_preserving_position':
                out.update(action='play', _native_action=kind)
                completed = True
            elif kind == 'new':
                out['action'] = 'new'; gstatus = 'not_started'
            else:
                out.update(action='play', started=0, position=value, total=total)
                position = value
                gstatus = 'finished' if value == total else 'in_progress'
            out.update(_expected_native_position=position, _expected_native_completed=completed)
            plan.append(out)
        if target_status == 'finished':
            if gstatus != 'finished': add('play', total)
            if not completed: add('completed_preserving_position')
        elif target_status == 'not_started':
            if completed or (position or 0) != 0: add('uncompleted')
            if gstatus != 'not_started': add('new')
        else:
            if not completed and (position or 0) > 0 and gstatus == 'in_progress':
                return []  # Status already agrees; keep both actual positions.
            if position is not None and 0 < position < total - self.completion_margin_seconds:
                if int(position) != position:
                    raise RequestError('PinePods: local variant position is not representable by gpodder')
                wanted = int(position)
            elif not (position or 0) or position >= duration - 2:
                wanted = 1
            else:
                raise RequestError('PinePods: local variant position is too near the end for an unfinished marker')
            if not 0 < wanted < total - self.completion_margin_seconds:
                raise RequestError('PinePods: variant has no representable started marker')
            if completed:
                add('uncompleted'); add('new')
            elif gstatus == 'finished': add('new')
            add('play', wanted)
        return plan

    def _status_payload(self, payload):
        if not isinstance(payload, dict): raise ValueError('Invalid PinePods variant action')
        extra = {'_variant_status','_episode_id','_user_id','_own_duration',
                 '_expected_native_position','_expected_native_completed'}
        core = {'podcast','episode','device','timestamp','action'}
        native = payload.get('_native_action')
        allowed = core | extra | ({'_native_action'} if native is not None else
                                 ({'started','position','total'} if payload.get('action') == 'play' else set()))
        if (set(payload) - {'_server_timestamp'} != allowed or payload.get('_variant_status') != 'v1'
                or payload.get('device') != self.device_id
                or type(payload.get('_episode_id')) is not int or payload['_episode_id'] <= 0
                or type(payload.get('_user_id')) is not int or payload['_user_id'] <= 1
                or type(payload.get('_expected_native_completed')) is not bool):
            raise ValueError('Invalid PinePods variant action scope')
        _url(payload['podcast']); _url(payload['episode']); action_epoch(payload)
        if payload['timestamp'] != _timestamp(payload):
            raise ValueError('Variant action timestamp must be canonical UTC')
        if '_server_timestamp' in payload:
            value = _number(payload['_server_timestamp'], maximum=253_402_300_799)
            if int(value) != value:raise ValueError('Invalid variant action server receipt')
        duration = _number(payload['_own_duration'], minimum=1, maximum=172800)
        position = payload['_expected_native_position']
        if position is not None: _number(position)
        if native is not None:
            if (native not in ('uncompleted','completed_preserving_position')
                    or payload['action'] != ('new' if native == 'uncompleted' else 'play')
                    or payload['_expected_native_completed'] is not (native == 'completed_preserving_position')
                    or native == 'uncompleted' and position not in (None,0)):
                raise ValueError('Invalid PinePods variant native action')
        elif payload['action'] == 'play':
            for name in ('started','position','total'):
                value = _number(payload[name])
                if int(value) != value: raise ValueError('Variant gpodder markers require whole seconds')
            if (payload['started'] != 0 or payload['total'] != math.ceil(duration)
                    or not 0 < payload['position'] <= payload['total'] or position != payload['position']
                    or payload['position'] != payload['total']
                    and payload['position'] >= payload['total'] - self.completion_margin_seconds):
                raise ValueError('Invalid own-version gpodder marker')
        elif payload['action'] != 'new':
            raise ValueError('Invalid PinePods variant gpodder action')
        return duration

    def write_status_action(self, payload, *, expected):
        """One separately journaled POST, freshly rechecked, never retried."""
        duration = self._status_payload(payload)
        if '_server_timestamp' in payload:
            raise ValueError('Acknowledged variant action must not be sent again')
        fresh = self.read_variant_status(payload['podcast'], payload['episode'], duration,
                    expected_episode_id=payload['_episode_id'], expected_user_id=payload['_user_id'])
        if fresh != expected:
            raise RequestError('PinePods: variant progress changed before status write')
        native = payload.get('_native_action')
        if native == 'completed_preserving_position' and payload['_expected_native_position'] != fresh['native_position']:
            raise ValueError('Variant completion must preserve its preimage position')
        if native is None and payload['_expected_native_completed'] != fresh['native_completed']:
            raise ValueError('Gpodder cannot change the native completion flag')
        if (native is None and payload['action'] == 'new'
                and (payload['_expected_native_position'] or 0) != (fresh['native_position'] or 0)):
            raise ValueError('Gpodder new does not reset native position in this version')
        self._verify_native_import_disabled()
        if native == 'completed_preserving_position':
            data = self._request('POST','/api/data/bulk_mark_episodes_completed',native=True,
                payload={'episode_ids':[payload['_episode_id']], 'user_id':payload['_user_id'], 'is_youtube':False})
            if (not isinstance(data,dict) or type(data.get('processed_count')) is not int
                    or data['processed_count'] != 1 or not (data.get('failed_count') is None
                    or type(data.get('failed_count')) is int and data['failed_count'] == 0)):
                raise RequestError('PinePods: variant completion acknowledgement requires review')
            return {'accepted':True,'native':True}
        if native == 'uncompleted':
            data = self._request('POST','/api/data/mark_episode_uncompleted',native=True,
                payload={'episode_id':payload['_episode_id'], 'user_id':payload['_user_id'], 'is_youtube':False})
            if not isinstance(data,dict) or not isinstance(data.get('detail'),str):
                raise RequestError('PinePods: variant reset acknowledgement requires review')
            return {'accepted':True,'native':True}
        wire = {key:value for key,value in payload.items() if not key.startswith('_')}
        return self._write_gpodder(wire)

    def status_action_confirmed(self, snapshot, payload, own_duration, *, previous):
        """Confirm this action's actual local effects, not an echoed action."""
        try:
            duration = self._status_payload(payload)
            if (duration != own_duration or not isinstance(snapshot,dict) or not isinstance(previous,dict)
                    or any(snapshot.get(key) != previous.get(key) for key in
                           ('own_duration','native_duration','native_episode_id','native_user_id'))
                    or snapshot.get('own_duration') != duration
                    or snapshot.get('native_episode_id') != payload['_episode_id']
                    or snapshot.get('native_user_id') != payload['_user_id']
                    or snapshot.get('native_completed') is not payload['_expected_native_completed']):
                return False
            actual, wanted = snapshot.get('native_position'), payload['_expected_native_position']
            if payload.get('_native_action') == 'uncompleted':
                if (actual or 0) != 0:return False
            elif payload.get('_native_action') is None and payload['action'] == 'new':
                # A missing/NULL history can become zero during the preceding
                # authorized native reset. Go `new` itself must preserve the
                # exact current preimage and cannot change any positive value.
                if actual != previous.get('native_position') or (actual or 0) != (wanted or 0):return False
            elif actual != wanted:return False
            if payload.get('_native_action'):
                return snapshot.get('gpodder_revision') == previous.get('gpodder_revision')
            if (snapshot.get('action') != payload['action'] or snapshot.get('device') != self.device_id
                    or type(snapshot.get('updated_at')) is not int or snapshot['updated_at'] < action_epoch(payload)):
                return False
            if payload.get('_server_timestamp') is not None and snapshot['updated_at'] != payload['_server_timestamp']:
                return False
            if payload['action'] == 'new':
                return snapshot.get('gpodder_status') == 'not_started' and snapshot.get('gpodder_raw_position') == 0
            return (snapshot.get('gpodder_raw_position') == payload['position']
                    and snapshot.get('gpodder_raw_total') == payload['total']
                    and snapshot.get('gpodder_status') == ('finished' if payload['position'] == payload['total'] else 'in_progress'))
        except (KeyError, TypeError, ValueError, RequestError):
            return False

    def prepare_action(self, feed_url, enclosure_url, state, duration, timestamp, guid=None):
        """Build a serializable action; caller must journal it before write_action."""
        _url(feed_url)
        _url(enclosure_url)
        duration = _number(duration, minimum=0.001)
        if not isinstance(state, dict) or state.get('status') not in {'not_started', 'in_progress', 'finished'}:
            raise ValueError('Invalid PinePods target status')
        position = _number(state.get('position'))
        if position > duration + 1:
            raise ValueError('Invalid PinePods target position')
        out = {'podcast': feed_url, 'episode': enclosure_url, 'device': self.device_id,
               'timestamp': _timestamp(timestamp)}
        if guid is not None:
            out['guid'] = _text(guid)
        if state['status'] == 'not_started':
            if position > 1:
                raise ValueError('Unstarted PinePods state has a position')
            out['action'] = 'new'
        else:
            total = math.ceil(duration)
            position = total if state['status'] == 'finished' else int(position)
            if state['status'] == 'in_progress' and (position <= 0 or position >= total - self.completion_margin_seconds):
                raise RequestError('PinePods: this in-progress position is not representable by gpodder')
            out.update(action='play', started=0, position=position, total=total)
        return out

    def prepare_actions(self, feed_url, enclosure_url, state, duration, timestamp, guid=None, previous=None):
        """Journal each native and gpodder mutation separately before execution."""
        epoch = action_epoch(timestamp)
        native = self._native_episode(feed_url, enclosure_url, duration)
        final = self.prepare_action(feed_url, enclosure_url, state, duration, epoch, guid)
        def native_step(kind):
            return {'podcast': feed_url, 'episode': enclosure_url, 'device': self.device_id,
                    'timestamp': _timestamp(epoch), 'action': 'new' if kind == 'uncompleted' else 'play',
                    '_native_action': kind, '_episode_id': native['episode_id'], '_user_id': native['user_id']}
        plan = []
        if state['status'] == 'not_started':
            plan = [native_step('uncompleted'), final]
        elif state['status'] == 'finished':
            plan = [final, native_step('completed')]
        elif native['completed'] or previous and previous.get('status') == 'finished':
            reset = self.prepare_action(feed_url, enclosure_url, {'position': 0, 'status': 'not_started'}, duration, epoch, guid)
            plan = [native_step('uncompleted'), reset, final]
        else:
            plan = [final]
        for index, step in enumerate(plan):
            step['timestamp'] = _timestamp(epoch + index)
        return plan

    def action_confirmed(self, snapshot, payload, duration):
        """Confirm actual PinePods state, not merely an echoed action-log entry."""
        if not isinstance(snapshot, dict) or not isinstance(payload, dict):
            return False
        native = payload.get('_native_action')
        if native:
            if (snapshot.get('native_episode_id') != payload.get('_episode_id')
                    or snapshot.get('native_user_id') != payload.get('_user_id')):
                return False
            if native == 'completed':
                position = snapshot.get('native_position')
                return (snapshot.get('native_completed') is True
                        and isinstance(position, (int, float)) and not isinstance(position, bool)
                        and math.isfinite(position) and abs(position - duration) <= 2)
            if native == 'uncompleted':
                return snapshot.get('native_completed') is False and (snapshot.get('native_position') or 0) == 0
            return False
        if (snapshot.get('action') != payload.get('action') or snapshot.get('device') != self.device_id
                or snapshot.get('updated_at') is None or snapshot['updated_at'] < action_epoch(payload)):
            return False
        if payload.get('_server_timestamp') is not None and snapshot['updated_at'] != payload['_server_timestamp']:
            return False
        if payload['action'] == 'new':
            return snapshot.get('native_completed') is False and (snapshot.get('native_position') or 0) == 0
        if payload['action'] == 'play':
            return (snapshot.get('native_position') is not None
                    and abs(snapshot['native_position'] - payload['position']) <= 1
                    and abs(snapshot.get('gpodder_position', -9999) - payload['position']) <= 2)
        return False

    def write_action(self, payload):
        """One POST, no retry. Readback by the coordinator is mandatory."""
        if not isinstance(payload, dict) or payload.get('device') != self.device_id:
            raise ValueError('Invalid PinePods action device')
        self._ensure_native()
        if payload.get('_native_action') is not None:
            allowed = {'podcast', 'episode', 'device', 'timestamp', 'action', '_native_action', '_episode_id', '_user_id'}
            if (set(payload) != allowed or payload['_native_action'] not in {'completed', 'uncompleted'}
                    or type(payload['_episode_id']) is not int or payload['_episode_id'] <= 0
                    or payload['_user_id'] != self._native_user_id):
                raise ValueError('Invalid PinePods native action')
            _url(payload['podcast'])
            _url(payload['episode'])
            action_epoch(payload)
            # Re-resolve by already-verified exact feed+episode IDs. This blocks
            # replaying a persisted command after an identity has changed.
            if self._native_ids.get((payload['podcast'], payload['episode'])) != payload['_episode_id']:
                raise RequestError('PinePods: native action identity requires a fresh read')
            self._verify_native_import_disabled()
            data = self._request('POST', '/api/data/mark_episode_' + payload['_native_action'], native=True,
                                 payload={'episode_id': payload['_episode_id'], 'user_id': self._native_user_id,
                                          'is_youtube': False})
            if not isinstance(data, dict) or not isinstance(data.get('detail'), str):
                raise RequestError('PinePods: native action acknowledgement requires review')
            return {'accepted': True, 'native': True}
        self._verify_native_import_disabled()
        return self._write_gpodder(payload)

    def _write_gpodder(self, payload):
        """Protocol transport only; public write_action enforces native checks."""
        if not isinstance(payload, dict) or payload.get('device') != self.device_id:
            raise ValueError('Invalid PinePods action device')
        if set(payload) - {'podcast', 'episode', 'device', 'timestamp', 'guid', 'action', 'started', 'position', 'total'}:
            raise ValueError('Unsupported PinePods action fields')
        _url(payload.get('podcast'))
        _url(payload.get('episode'))
        if payload.get('timestamp') != _timestamp(payload):
            raise ValueError('PinePods action timestamp must be canonical UTC')
        if payload.get('guid') is not None:
            _text(payload['guid'])
        if payload.get('action') == 'play':
            for key in ('started', 'position', 'total'):
                n = _number(payload.get(key))
                if int(n) != n:
                    raise ValueError('PinePods action must use whole seconds')
            if not 0 <= payload['started'] <= payload['position'] <= payload['total'] or payload['position'] == 0:
                raise ValueError('Invalid PinePods playback action')
        elif payload.get('action') != 'new' or any(k in payload for k in ('started', 'position', 'total')):
            raise ValueError('Unsupported PinePods action')
        if self.device_id not in self.devices():
            raise RequestError('PinePods: configure the sync device before enabling writes')
        data = self._request('POST', '/api/2/episodes/' + self._user_path, payload=[payload])
        try:
            if not isinstance(data, dict) or data.get('update_urls') != []:
                raise ValueError()
            _number(data.get('timestamp'), maximum=253_402_300_799)
        except ValueError:
            raise RequestError('PinePods: action acknowledgement requires review') from None
        return {'accepted': True, 'timestamp': data['timestamp']}


# Both spellings kept for integrations that use the product's capital P.
PinePodsClient = PinepodsClient
