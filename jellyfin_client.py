"""User-pinned Jellyfin client. Mutations are never automatically retried."""
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
import requests


def utc_timestamp(value):
    """Use UTC Kind on Jellyfin's wire, retaining its 100 ns precision.

    System.Text.Json reads an explicit offset as a local DateTime. Jellyfin's
    legacy writer stamps a literal Z when Millisecond == 0 without converting
    that local value to UTC. An actual Z input avoids the erroneous time shift.
    """
    if not isinstance(value, str):
        raise ValueError('Invalid Jellyfin timestamp')
    match = re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d{1,7}))?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)', value)
    if not match:
        raise ValueError('Jellyfin timestamp must have an explicit timezone')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError('Invalid Jellyfin timestamp') from None
    fraction = match.group(1) or ''
    # datetime stores microseconds; preserve a supplied seventh 100 ns digit.
    final_digit = fraction[6] if len(fraction) == 7 else '0'
    return parsed.isoformat(timespec='microseconds').removesuffix('+00:00') + final_digit + 'Z'


class JellyfinClient:
    def __init__(self, url, token, user_id, username, version):
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.path not in ('', '/') or p.query or p.fragment:
            raise ValueError('Invalid Jellyfin endpoint')
        if not re.fullmatch('[a-f0-9]{32}', user_id) or not token or '"' in token or '\n' in token:
            raise ValueError('Invalid Jellyfin identity')
        self.url, self.user, self.username, self.version = url.rstrip('/'), user_id, username, version
        self.s = requests.Session(); self.s.trust_env = False
        self.s.headers['Authorization'] = ('MediaBrowser Client="Book progress sync", Device="Server", '
            'DeviceId="book-progress-sync", Version="2.0", Token="' + token + '"')

    def request(self, method, path, params=None, body=None):
        try:
            r = self.s.request(method, self.url + path, params=params, json=body,
                               timeout=(5, 30), allow_redirects=False)
        except requests.RequestException:
            raise RuntimeError('Jellyfin transport failure') from None
        if r.status_code not in (200, 204):
            raise RuntimeError('Jellyfin ' + method + ': HTTP ' + str(r.status_code))
        if r.status_code == 204: return None
        try: return r.json()
        except ValueError: raise RuntimeError('Invalid Jellyfin response') from None

    def guard(self):
        info = self.request('GET', '/System/Info/Public')
        if info.get('Version') != self.version: raise RuntimeError('Jellyfin version changed')
        u = self.request('GET', '/Users/' + self.user)
        if u.get('Id') != self.user or u.get('Name') != self.username or u.get('Policy', {}).get('IsDisabled'):
            raise RuntimeError('Jellyfin personal user changed')
        return u

    def items(self, parent=None, ids=None, types='AudioBook,Book'):
        if ids and len(ids) > 100:
            if len(set(ids)) != len(ids): raise ValueError('Duplicate requested Jellyfin identifiers')
            return [item for start in range(0, len(ids), 100)
                    for item in self.items(parent, ids[start:start+100], types)]
        result, total, seen = [], None, set()
        # Jellyfin has no stable ID tiebreaker for repeated chapter titles.
        # Read this bounded personal library in one snapshot rather than risk
        # missing/duplicating rows across pages with equal sort keys.
        for start in (0,):
            q = dict(userId=self.user, recursive='true', includeItemTypes=types,
                     fields='Path,MediaSources', startIndex=start, limit=20000,
                     enableUserData='true', sortBy='SortName', sortOrder='Ascending')
            if parent: q['parentId'] = parent
            if ids:
                if any(not re.fullmatch('[a-f0-9]{32}', x) for x in ids): raise ValueError('Invalid requested Jellyfin identifier')
                q['ids'] = ','.join(ids)
            j = self.request('GET', '/Items', q)
            if not isinstance(j.get('Items'), list) or not isinstance(j.get('TotalRecordCount'), int):
                raise RuntimeError('Invalid Jellyfin inventory')
            if total is not None and total != j['TotalRecordCount']: raise RuntimeError('Jellyfin inventory changed')
            total = j['TotalRecordCount']
            for item in j['Items']:
                if item.get('Id') in seen: raise RuntimeError('Duplicate Jellyfin item during pagination')
                if not item.get('Id') or not isinstance(item.get('UserData'), dict):
                    raise RuntimeError('Jellyfin item has no personal user data')
                if ids and item['Id'] not in ids: raise RuntimeError('Jellyfin ignored item scope')
                seen.add(item['Id']); result.append(item)
            if len(result) >= total:
                if len(result) != total or (ids and seen != set(ids)): raise RuntimeError('Incomplete Jellyfin inventory')
                return result
            if not j['Items']: break
        raise RuntimeError('Incomplete Jellyfin pagination')

    def update(self, item_id, values):
        if not re.fullmatch('[a-f0-9]{32}', item_id): raise ValueError('Invalid Jellyfin item')
        allowed = {'PlaybackPositionTicks', 'Played', 'LastPlayedDate', 'PlayCount'}
        if not values or set(values) - allowed: raise ValueError('Unexpected Jellyfin user-data write')
        values = dict(values)
        if values.get('LastPlayedDate') is not None:
            values['LastPlayedDate'] = utc_timestamp(values['LastPlayedDate'])
        return self.request('POST', '/UserItems/' + item_id + '/UserData', {'userId': self.user}, values)

    def active_ids(self):
        sessions = self.request('GET', '/Sessions')
        return {s['NowPlayingItem']['Id'] for s in sessions if s.get('UserId') == self.user
                and s.get('NowPlayingItem') and not s.get('PlayState', {}).get('IsPaused', False)}
