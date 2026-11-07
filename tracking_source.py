"""Read-only, account-scoped PinePods 0.9.0 state for podcast tracking.

The native Completed flag is authoritative; the separate GPodder action log is
not a complete record of native UI changes in this upstream release. Capture
returns a complete inventory or raises, never a partially successful snapshot.
No episode metadata requests, media downloads or playback writes are made.
"""
from __future__ import annotations

import base64
import math
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from pinepods_client import PinepodsClient, RequestError, _url, action_epoch


class TrackingSourceError(ValueError):
    """Fixed diagnostic codes; never upstream bodies, URLs or credentials."""


class NativeTrackingSource:
    PAGE_SIZE = 200
    MAX_BODY = 32 * 1024 * 1024

    def __init__(self, config, request=None, *, clock=None):
        self.clock = clock or time.time
        self.upstream = config['upstream'].rstrip('/')
        self.username = config['username']
        self.version = config.get('server_version', '0.9.0')
        if self.version != '0.9.0':
            raise TrackingSourceError('native_version_unverified')
        # Reuse the existing adapter's origin, private HTTP and account checks.
        try:
            probe = PinepodsClient(self.upstream, self.username, 'validation-only', server_version=self.version)
            probe.close()
        except (ValueError, TypeError, AttributeError):
            raise TrackingSourceError('native_source_scope_invalid') from None
        self.key_file = Path(config['api_key_file'])
        self.max_feeds = self._limit(config.get('max_feeds', 1000), 1, 10000)
        self.max_rows = self._limit(config.get('max_rows', 100000), 1, 1000000)
        self.max_pages = self._limit(config.get('max_pages', 1000), 1, 10000)
        self.max_seconds = self._limit(config.get('max_capture_seconds', 300), 1, 900)
        self.history_since = config.get('history_since', config.get('baseline', 0))
        if type(self.history_since) is not int or self.history_since < 0:
            raise TrackingSourceError('native_history_boundary_invalid')
        self._verified_user_id = config.get('user_id')
        if self._verified_user_id is not None and (type(self._verified_user_id) is not int or self._verified_user_id <= 1):
            raise TrackingSourceError('native_account_scope_invalid')
        self._session = None
        if request is None:
            self._session = requests.Session()
            self._session.trust_env = False
            request = self._session.request
        self.request = request
        self.last_status = {'ok': False, 'error': 'not_captured'}

    @staticmethod
    def _limit(value, minimum, maximum):
        if type(value) is not int or not minimum <= value <= maximum:
            raise TrackingSourceError('native_capture_limit_invalid')
        return value

    def close(self):
        if self._session is not None:
            self._session.close()

    def _password(self, authorization):
        try:
            if not isinstance(authorization, str) or len(authorization) > 8192:
                raise ValueError()
            scheme, value = authorization.split(' ', 1)
            user, password = base64.b64decode(value, validate=True).decode('utf-8').split(':', 1)
            if (scheme != 'Basic' or user != self.username or not password
                    or len(password) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in password)):
                raise ValueError()
            return password
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise TrackingSourceError('native_tracking_authentication_required') from None

    def _key(self):
        try:
            with self.key_file.open('rb') as source:
                raw = source.read(4097)
            if not raw or len(raw) > 4096:
                raise ValueError()
            key = raw.decode('utf-8').strip()
            if not key or any(ord(c) < 32 or ord(c) == 127 for c in key):
                raise ValueError()
            return key
        except (OSError, UnicodeError, ValueError):
            raise TrackingSourceError('native_key_unavailable') from None

    def _get(self, path, key, deadline, params=None):
        if time.monotonic() >= deadline:
            raise TrackingSourceError('native_capture_time_limit')
        # Paths are fixed internally, never URLs supplied by native metadata.
        if not path.startswith('/api/data/') or urlsplit(path).netloc:
            raise TrackingSourceError('native_request_scope_invalid')
        try:
            response = self.request('GET', self.upstream + path,
                                    headers={'Api-Key': key, 'Accept': 'application/json',
                                             'User-Agent': 'Books-Audio-Tracking/2'},
                                    params=params, timeout=(5, 20), allow_redirects=False)
        except (requests.RequestException, OSError, UnicodeError):
            raise TrackingSourceError('native_transport_unavailable') from None
        if response.status_code != 200:
            raise TrackingSourceError('native_api_unavailable')
        try:
            if len(response.content) > self.MAX_BODY:
                raise ValueError()
            data = response.json()
        except (ValueError, TypeError, AttributeError):
            raise TrackingSourceError('native_response_invalid') from None
        if time.monotonic() > deadline:
            raise TrackingSourceError('native_capture_time_limit')
        return data

    def _pods(self, client):
        data = client._request('GET', '/api/data/return_pods/' + str(client._native_user_id), native=True)
        try:
            pods = data['pods']
            if not isinstance(pods, list) or len(pods) > self.max_feeds:
                raise ValueError()
            seen_ids, seen_feeds, normalized = set(), set(), []
            for row in pods:
                ident, feed = row['podcastid'], row['feedurl']
                if type(ident) is not int or ident <= 0 or ident in seen_ids:
                    raise ValueError()
                _url(feed)
                if feed in seen_feeds:
                    raise ValueError()
                seen_ids.add(ident); seen_feeds.add(feed)
                normalized.append((ident, feed))
            return sorted(normalized)
        except (KeyError, ValueError, TypeError, AttributeError):
            raise TrackingSourceError('native_feed_inventory_invalid') from None

    def _pages(self, client, path, row_key, extra, stats):
        offset, expected_total = 0, None
        while True:
            stats['pages'] += 1
            if stats['pages'] > self.max_pages:
                raise TrackingSourceError('native_capture_page_limit')
            data = client._request('GET', path, native=True,
                                   params=dict(extra, limit=self.PAGE_SIZE, offset=offset,
                                               sort_by='date', sort_order='desc', filter='all'))
            try:
                rows, total = data[row_key], data['total']
                if (not isinstance(rows, list) or type(total) is not int or not 0 <= total <= self.max_rows
                        or expected_total is not None and total != expected_total
                        or len(rows) > self.PAGE_SIZE or offset + len(rows) > total
                        or any(not isinstance(row, dict) for row in rows)
                        or not rows and offset != total):
                    raise ValueError()
            except (KeyError, ValueError, TypeError):
                raise TrackingSourceError('native_inventory_incomplete_or_changed') from None
            expected_total = total
            yield rows
            offset += len(rows)
            if offset == total:
                return

    @staticmethod
    def _position(value):
        if value is None:
            return 0
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2147483647:
            raise TrackingSourceError('native_progress_invalid')
        return value

    def _episodes(self, client, pods, stats):
        output, seen_global = [], set()
        for podcast_id, feed in pods:
            seen_local, seen_urls = set(), set()
            for rows in self._pages(client, '/api/data/podcast_episodes', 'episodes',
                                    {'user_id': client._native_user_id, 'podcast_id': podcast_id}, stats):
                stats['inventory_rows'] += len(rows)
                if stats['inventory_rows'] > self.max_rows:
                    raise TrackingSourceError('native_capture_row_limit')
                for row in rows:
                    episode_id, youtube = row.get('Episodeid'), row.get('is_youtube')
                    if (type(episode_id) is not int or episode_id <= 0 or type(youtube) is not bool
                            or (youtube, episode_id) in seen_local):
                        raise TrackingSourceError('native_episode_identity_invalid')
                    seen_local.add((youtube, episode_id))
                    if youtube:
                        stats['skipped_youtube'] += 1
                        continue
                    if episode_id in seen_global:
                        raise TrackingSourceError('native_episode_scope_ambiguous')
                    seen_global.add(episode_id)
                    if type(row.get('Completed')) is not bool:
                        raise TrackingSourceError('native_completion_invalid')
                    position = self._position(row.get('Listenduration'))
                    try:
                        published = action_epoch(row.get('Episodepubdate'))
                    except ValueError:
                        raise TrackingSourceError('native_publication_invalid') from None
                    enclosure = row.get('Episodeurl')
                    if enclosure in ('', None):
                        stats['pending_missing_url'] += 1
                        continue
                    try:
                        _url(enclosure)
                    except ValueError:
                        raise TrackingSourceError('native_episode_url_invalid') from None
                    if enclosure in seen_urls:
                        raise TrackingSourceError('native_episode_url_ambiguous')
                    seen_urls.add(enclosure)
                    duration = row.get('Episodeduration')
                    if duration in (None, 0) and type(duration) is not bool:
                        stats['pending_missing_duration'] += 1
                        continue
                    duration = self._position(duration)
                    if duration == 0:
                        raise TrackingSourceError('native_duration_invalid')
                    if not row['Completed'] and position > duration:
                        # A refreshed advertisement cut may be shorter than its
                        # saved unfinished position. Keep this episode pending;
                        # neither clip it nor infer completion, and let the rest
                        # of the complete account inventory continue to sync.
                        stats['pending_invalid_progress'] += 1
                        continue
                    output.append({'id': f'{client._native_user_id}:{podcast_id}:{episode_id}',
                                   'podcast': feed, 'episode': enclosure,
                                   'completed': row['Completed'], 'position': position,
                                   'total': duration, 'published_at': published})
        return output

    def _history(self, client, output, stats):
        by_id = {int(row['id'].rsplit(':', 1)[1]): row for row in output}
        seen, previous_time, past_boundary = set(), math.inf, False
        for rows in self._pages(client, '/api/data/user_history/' + str(client._native_user_id),
                                'data', {}, stats):
            stats['history_rows'] += len(rows)
            if stats['history_rows'] > self.max_rows:
                raise TrackingSourceError('native_capture_row_limit')
            for history in rows:
                episode_id, youtube = history.get('episodeid'), history.get('is_youtube')
                if type(episode_id) is not int or episode_id <= 0 or type(youtube) is not bool or (youtube, episode_id) in seen:
                    raise TrackingSourceError('native_history_identity_invalid')
                seen.add((youtube, episode_id))
                raw_date = history.get('listendate')
                try:
                    stamp = action_epoch(raw_date) if raw_date is not None else None
                except ValueError:
                    raise TrackingSourceError('native_history_time_invalid') from None
                # NULL dates sort last in the released PostgreSQL date query.
                order_time = stamp if stamp is not None else -math.inf
                if order_time > previous_time:
                    raise TrackingSourceError('native_history_order_changed')
                previous_time = order_time
                if stamp is None or stamp < self.history_since:
                    past_boundary = True
                    continue
                if stamp > int(self.clock()):
                    # Upstream 0.9.0 emits a timezone-free database timestamp.
                    # Its NOW() writes and imported UTC values can have different
                    # provenance. A future value is not trusted activity evidence:
                    # never shift it heuristically or let it bootstrap a listen.
                    stats['history_untrusted_time'] += 1
                    continue
                if youtube:
                    continue
                target = by_id.get(episode_id)
                if target is None:
                    stats['history_not_in_inventory'] += 1
                    continue
                if history.get('episodeurl') != target['episode']:
                    stats['history_changed'] += 1
                    continue
                if (type(history.get('completed')) is not bool
                        or history['completed'] != target['completed']
                        or self._position(history.get('listenduration')) != target['position']):
                    stats['history_changed'] += 1
                    continue
                target['updated_at'] = stamp
            # The explicit date boundary concerns migration evidence only. It
            # does not exclude old-publication episodes from the state inventory.
            if past_boundary:
                stats['history_boundary_reached'] = True
                return

    def capture(self, authorization):
        try:
            password = self._password(authorization)
            key = self._key()
            client = PinepodsClient(self.upstream, self.username, password, api_key=key, server_version=self.version)
        except TrackingSourceError as error:
            self.last_status = {'ok': False, 'error': str(error)}
            raise
        except ValueError:
            self.last_status = {'ok': False, 'error': 'native_credentials_invalid'}
            raise TrackingSourceError('native_credentials_invalid') from None
        deadline = time.monotonic() + self.max_seconds
        stats = dict(ok=False, feeds=0, rows=0, inventory_rows=0, history_rows=0, pages=0,
                     skipped_youtube=0, pending_missing_url=0, pending_missing_duration=0,
                     pending_invalid_progress=0,
                     history_changed=0, history_not_in_inventory=0, history_boundary_reached=False,
                     history_untrusted_time=0)
        # Only _ensure_native is reused. Its native ownership checks are fresh
        # per capture; every native request is forced to the GET-only transport.
        def native_request(method, path, *, params=None, payload=None, native=False):
            if method != 'GET' or payload is not None or native is not True:
                raise TrackingSourceError('native_write_forbidden')
            return self._get(path, key, deadline, params)
        client._request = native_request
        try:
            user_id = client._ensure_native()
            if self._verified_user_id is not None and user_id != self._verified_user_id:
                raise TrackingSourceError('native_account_changed')
            pods = self._pods(client)
            stats['feeds'] = len(pods)
            output = self._episodes(client, pods, stats)
            self._history(client, output, stats)
            if self._pods(client) != pods:
                raise TrackingSourceError('native_feed_scope_changed')
            output.sort(key=lambda row: row['id'])
            stats.update(ok=True, rows=len(output), user_id=user_id)
            self._verified_user_id = user_id
            self.last_status = stats
            return output
        except TrackingSourceError as error:
            self.last_status = {'ok': False, 'error': str(error)}
            raise
        except RequestError:
            self.last_status = {'ok': False, 'error': 'native_account_unverified'}
            raise TrackingSourceError('native_account_unverified') from None
        finally:
            client.close()
