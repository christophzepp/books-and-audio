"""Read the separate tracking stack's diagnostics for the active PinePods profile."""
import os
from pathlib import Path
import re
from urllib.parse import parse_qsl

from tracking_delivery import read_episode_delivery, search_episode_delivery


def parameters(query, *, search):
    if len(query) > 1536 or re.search(r'%(?![0-9a-fA-F]{2})', query):
        raise ValueError('invalid_query')
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                      encoding='utf-8', errors='strict', max_num_fields=2)
    values = dict(pairs)
    if len(pairs) != len(values) or any(any(ord(ch) < 32 or ord(ch) == 127 for ch in v) for v in values.values()):
        raise ValueError('invalid_query')
    if search:
        if set(values) - {'q', 'offset'} or len(values.get('q', '')) > 160:
            raise ValueError('invalid_search')
        offset = values.get('offset', '0')
        if not re.fullmatch(r'[0-9]{1,7}', offset) or int(offset) > 1_000_000:
            raise ValueError('invalid_offset')
        return {'query': values.get('q', '').strip(), 'offset': int(offset)}
    if set(values) == {'native_id'} and re.fullmatch(r'[1-9][0-9]{0,18}:[1-9][0-9]{0,18}:[1-9][0-9]{0,18}', values['native_id']):
        return values
    if set(values) == {'episode_id'} and re.fullmatch(r'_podcast:[^:/?#\s]{1,128}:[^:/?#\s]{1,128}', values['episode_id']):
        return values
    raise ValueError('invalid_episode')


def report(store, args, *, search, env=None):
    env = os.environ if env is None else env
    db = Path(env.get('TRACKING_DELIVERY_DB', '/tracking-readonly/relay.sqlite'))
    profiles = [p for p in store.list_connections().get('profiles', [])
                if p.get('kind') == 'pinepods' and p.get('active') is True]
    settings = profiles[0]['settings'] if len(profiles) == 1 else {}
    upstream, username = settings.get('url', '').rstrip('/'), settings.get('username')
    if not upstream or not username:
        return {'state': 'scope_mismatch'}
    if upstream == env.get('TRACKING_PROFILE_ORIGIN', '').rstrip('/'):
        upstream = env.get('TRACKING_RELAY_UPSTREAM', '').rstrip('/')
        if not upstream:
            return {'state': 'scope_mismatch'}
    scope = {'upstream': upstream, 'username': username}
    pin = env.get('TRACKING_PINE_USER_ID', '')
    if pin:
        if not re.fullmatch(r'[1-9][0-9]{0,18}', pin):
            return {'state': 'scope_mismatch'}
        scope['user_id'] = int(pin)
    if search:
        return search_episode_delivery(db, **scope, **args)
    if 'native_id' in args:
        match = {'episode_id': args['native_id']}
    else:
        rows = store.status().get('podcasts', {}).get('episodes', [])
        matches = [r.get('delivery_lookup') for r in rows if r.get('id') == args['episode_id']]
        if len(matches) != 1 or not isinstance(matches[0], str) or not re.fullmatch(r'[0-9a-f]{64}', matches[0]):
            return {'state': 'not_found'}
        match = {'lookup_hash': matches[0]}
    data = read_episode_delivery(db, **scope, **match)
    from floppy_evidence import attach_floppy_evidence
    return attach_floppy_evidence(data, env.get('FLOPPY_EVIDENCE_FILE', '/tracking-proof/floppy.json'),
                                 upstream=upstream, username=username, floppy_user=env.get('FLOPPY_EVIDENCE_USER', ''),
                                 relay_url=env.get('FLOPPY_EVIDENCE_RELAY_URL', ''))
