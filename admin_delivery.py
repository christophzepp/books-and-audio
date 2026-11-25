"""Read the separate tracking stack's diagnostics for the active PinePods profile."""
import os
from pathlib import Path
import re
from urllib.parse import parse_qsl

from tracking_delivery import read_episode_delivery, search_episode_delivery


def alias_review(store, payload, *, apply, env=None):
    from delivery_monitor import relay_scope_key
    from tracking_delivery import _read
    import tracking_alias

    fields = {'episode_id', 'event_id', 'scope'}
    if apply:
        fields.update(('token', 'confirm'))
    if set(payload) != fields:
        raise ValueError('invalid_alias_request')
    if (not isinstance(payload['episode_id'], str)
            or not re.fullmatch(r'[1-9][0-9]{0,18}:[1-9][0-9]{0,18}:[1-9][0-9]{0,18}', payload['episode_id'])
            or not all(isinstance(payload[k], str) and re.fullmatch('[0-9a-f]{64}', payload[k])
                       for k in fields - {'episode_id', 'confirm'})
            or (apply and payload['confirm'] is not True)):
        raise ValueError('invalid_alias_request')
    env = os.environ if env is None else env
    view = store.list_connections()
    context = relay_context(store, env, view=view)
    if context is None:
        return {'state': 'scope_mismatch'}
    db, account = context
    def check(relay):
        current = relay_scope_key(relay, view, env.get('FLOPPY_EVIDENCE_USER', ''),
                                  env.get('FLOPPY_EVIDENCE_RELAY_URL', ''))
        return {'state': 'ok' if current == payload['scope'] else 'scope_mismatch'}
    scoped = _read(db, account['upstream'], account['username'], account.get('user_id'), check)
    if scoped['state'] != 'ok':
        return scoped
    args = db, account, payload['episode_id'], payload['event_id']
    paths = dict(exchange_dir=Path(env.get('TRACKING_ALIAS_DIR', '/tracking-alias')),
                 proof_file=Path(env.get('TRACKING_GUID_PROOF_FILE', '/tracking-proof/pinepods-guids.json')))
    if apply:
        return tracking_alias.approve(*args, payload['token'], **paths)
    return tracking_alias.preview(*args, **paths)


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


def relay_context(store, env=None, *, view=None):
    env = os.environ if env is None else env
    db = Path(env.get('TRACKING_DELIVERY_DB', '/tracking-readonly/relay.sqlite'))
    view = store.list_connections() if view is None else view
    profiles = [p for p in view.get('profiles', [])
                if p.get('kind') == 'pinepods' and p.get('active') is True]
    settings = profiles[0]['settings'] if len(profiles) == 1 else {}
    upstream, username = settings.get('url', '').rstrip('/'), settings.get('username')
    if not upstream or not username:
        return None
    if upstream == env.get('TRACKING_PROFILE_ORIGIN', '').rstrip('/'):
        upstream = env.get('TRACKING_RELAY_UPSTREAM', '').rstrip('/')
        if not upstream:
            return None
    scope = {'upstream': upstream, 'username': username}
    pin = env.get('TRACKING_PINE_USER_ID', '')
    if pin:
        if not re.fullmatch(r'[1-9][0-9]{0,18}', pin):
            return None
        scope['user_id'] = int(pin)
    return db, scope


def report(store, args, *, search, env=None):
    env = os.environ if env is None else env
    context = relay_context(store, env)
    if context is None:
        return {'state': 'scope_mismatch'}
    db, scope = context
    upstream, username = scope['upstream'], scope['username']
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
