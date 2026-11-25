"""Event-scoped approval of a changed RSS enclosure. The web process never writes the relay DB."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import parse_qsl, urlsplit

from tracking_delivery import _read, _state, lookup_key

HEX = re.compile(r'[0-9a-f]{64}')
NATIVE = re.compile(r'[1-9][0-9]{0,18}(?::[1-9][0-9]{0,18}){2}')
MAX_AGE = 24 * 60 * 60


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                                     sort_keys=True).encode()).hexdigest()


def ensure_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS native_address_candidates (
        event_id TEXT PRIMARY KEY, evidence TEXT NOT NULL, checked_at INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS native_address_approvals (
        event_id TEXT PRIMARY KEY, token TEXT NOT NULL, approved_at INTEGER NOT NULL)''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS clear_address_candidate AFTER DELETE ON native_outbox
        BEGIN DELETE FROM native_address_candidates WHERE event_id=OLD.event_id; END''')


def read_json(path, limit):
    try:
        with Path(path).open('rb') as file:
            raw = file.read(limit+1)
        if len(raw) <= limit:
            value = json.loads(raw)
            if isinstance(value, dict):
                return value
    except (OSError, ValueError, RecursionError, TypeError):
        pass
    return {}


def proof_guid(proof, account, episode_id, feed, url, now):
    taken = proof.get('observed_at')
    if (proof.get('schema') != 1 or proof.get('scope') != account or type(taken) is not int
            or not now-MAX_AGE <= taken <= now or not isinstance(proof.get('rows'), dict)):
        return None
    item = proof['rows'].get(episode_id)
    if (isinstance(item, dict) and item.get('source') == lookup_key(feed, url)
            and isinstance(item.get('guid'), str) and HEX.fullmatch(item['guid'])):
        return item['guid']
    return None


def account_scope(db):
    scope = json.loads(_state(db, 'native_scope'))
    return digest(scope[:2])


def expectation(db, episode_id, event_id):
    row = db.execute('''SELECT e.payload,s.snapshot,s.scan_number,e.generation,s.generation,e.sequence,s.sequence
        FROM native_outbox o JOIN native_tracking_events e ON e.event_id=o.event_id
        JOIN native_tracking_episodes s ON s.id=e.episode_id
        WHERE o.event_id=? AND e.episode_id=? AND e.kind='play'
        AND NOT EXISTS (SELECT 1 FROM native_delivery_receipts r WHERE r.event_id=e.event_id)''',
        (event_id, episode_id)).fetchone()
    if not row or row[2] != _state(db, 'native_inventory_scan') or row[3] != row[4] or row[5] != row[6]:
        return None
    event, src = json.loads(row[0]), json.loads(row[1])
    if (src['id'] != episode_id or event['_tracking_episode_id'] != episode_id
            or event['_tracking_event_id'] != event_id or src['podcast'] != event['podcast']
            or src['episode'] != event['episode'] or src['completed'] != event['_tracking_completed']
            or not src['completed'] and (src['position'],src['total']) != (event['position'],event['total'])):
        return None
    return event, src


def uri_host(url):
    return urlsplit(url).hostname


def changed_keys(old, new):
    def fields(url):
        result = {}
        for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
            if re.fullmatch(r'[a-zA-Z][A-Za-z0-9_.-]{0,39}', name):
                result.setdefault(name, []).append(value)
        return result
    before, after = fields(old), fields(new)
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))[:12]


def source_guid(src, event, external_guid):
    known = {digest(guid) for guid in (src.get('guid'), event.get('guid')) if guid}
    if external_guid:
        known.add(external_guid)
    if len(known) == 1:
        return next(iter(known))


def address_evidence(db, scope, event, old, new, guid):
    return {'account': scope, 'journal': digest([_state(db,'scope'),_state(db,'native_scope')]),
            'native_id': event['_tracking_episode_id'], 'event_id':event['_tracking_event_id'],
            'event':digest(event), 'source':lookup_key(event['podcast'], old),
            'old_digest':digest(old), 'new_digest':digest(new), 'guid':digest(guid),
            'old_host':uri_host(old), 'new_host':uri_host(new),
            'changed_query_keys':changed_keys(old, new)}


def matches_request(directory, event_id, token):
    request = read_json(Path(directory)/(event_id+'.json'), 4096)
    return (request.get('version') == 1 and request.get('event_id') == event_id
            and request.get('token') == token and type(request.get('approved_at')) is int
            and request['approved_at'] > 0)


class AddressReviewPoll:
    def __init__(self, resolver, now):
        self.resolver, self.now = resolver, now
        self.proof = None
        self.directory = os.getenv('TRACKING_ALIAS_DIR', '/tracking-alias')
        self.proof_file = os.getenv('TRACKING_GUID_PROOF_FILE', '/tracking-proof/pinepods-guids.json')

    def consider(self, db, event, wire):
        """Called only for an unresolved identity against the RSS already fetched in this poll."""
        event_id, ident = event['_tracking_event_id'], event['_tracking_episode_id']
        if not HEX.fullmatch(event_id) or not NATIVE.fullmatch(ident):
            return None
        original = expectation(db, ident, event_id)
        if not original:
            return None
        account = account_scope(db)
        if self.proof is None:
            self.proof = read_json(self.proof_file, 8*1024*1024)
        old, feed = wire['episode'], wire['podcast']
        external_guid = proof_guid(self.proof, account, ident, feed, old, self.now)
        required = source_guid(original[1], event, external_guid)
        if not required and (external_guid or original[1].get('guid') or event.get('guid')):
            return None  # conflicting source evidence
        entries = self.resolver.cache.get(feed, [])
        if not entries or any(row['episode']==old for row in entries):
            return None
        if required:
            possible = [p for p in entries if p.get('guid') and digest(p['guid'])==required]
        else:
            # Only a suggestion until independently checked against the native GUID.
            # Helps an operator see the address and approve after the next proof export.
            anchor = urlsplit(old)
            possible = [p for p in entries if p.get('episode') and urlsplit(p['episode'])[:3]==anchor[:3]]
        if len(possible)!=1:
            return None
        new, guid = possible[0]['episode'], possible[0].get('guid')
        if (not new or not guid or new in getattr(entries,'ambiguous_urls',())
                or guid in getattr(entries,'ambiguous_guids',()) or sum(p['episode']==new for p in entries)!=1
                or sum(p.get('guid')==guid for p in entries)!=1):
            return None
        evidence = address_evidence(db, account, event, old, new, guid)
        db.execute('INSERT OR REPLACE INTO native_address_candidates VALUES(?,?,?)',
                   (event_id, json.dumps(evidence), self.now))
        token = digest(evidence)
        if required != evidence['guid'] or not matches_request(self.directory, event_id, token):
            return None
        prior = db.execute('SELECT token FROM native_address_approvals WHERE event_id=?', (event_id,)).fetchone()
        if prior and prior[0] != token:
            return None
        db.execute('INSERT OR IGNORE INTO native_address_approvals VALUES(?,?,?)', (event_id, token, self.now))
        return dict(wire, episode=new, guid=guid, url=guid)


def _review(db, episode_id, event_id, directory, proof_file, now, confirm):
    if confirm is not None:
        accepted = db.execute('''SELECT a.token FROM native_address_approvals a
            JOIN native_tracking_events e ON e.event_id=a.event_id
            WHERE a.event_id=? AND e.episode_id=?''', (event_id,episode_id)).fetchone()
        if accepted and hmac.compare_digest(accepted[0],confirm):
            return {'state':'approved','native_id':episode_id,'event_id':event_id}
    row = db.execute('SELECT evidence,checked_at FROM native_address_candidates WHERE event_id=?',(event_id,)).fetchone()
    current = expectation(db, episode_id, event_id)
    if not current or not row or not now-MAX_AGE <= row[1] <= now:
        return {'state':'stale' if confirm else 'waiting', 'waiting_reason':'fresh_candidate'}
    event, src = current
    evidence = json.loads(row[0])
    account = account_scope(db)
    if (evidence['account'] != account or evidence['native_id'] != episode_id or evidence['event_id'] != event_id
            or evidence['event'] != digest(event) or evidence['source'] != lookup_key(src['podcast'],src['episode'])):
        return {'state':'stale', 'waiting_reason':'source_changed'}
    proof = read_json(proof_file, 8*1024*1024)
    external = proof_guid(proof, account, episode_id, src['podcast'],src['episode'], now)
    if source_guid(src, event, external) != evidence['guid']:
        return {'state':'stale' if confirm else 'waiting','waiting_reason':'source_guid'}
    fields = {k:evidence[k] for k in ('native_id','event_id','old_host','new_host','changed_query_keys','old_digest','new_digest')}
    fields['proof'] = 'native_guid' if src.get('guid') or event.get('guid') else 'pinepods_guid'
    fields['rss_checked_at'] = row[1]
    fields['source_checked_at'] = row[1] if fields['proof']=='native_guid' else proof['observed_at']
    fields.update(status='completed' if event['_tracking_completed'] else 'in_progress',
                  position=event['position'], total=event['total'])
    token = digest(evidence)
    if confirm and not hmac.compare_digest(token, confirm):
        return {'state':'stale', 'waiting_reason':'source_changed'}
    directory = Path(directory)
    if not directory.is_dir():
        return {'state':'unavailable','waiting_reason':'exchange'}
    if matches_request(directory, event_id, token):
        return dict(fields,state='approved')
    if confirm is None:
        return dict(fields,state='ready',token=token)
    try:
        with tempfile.NamedTemporaryFile(mode='w',dir=directory,prefix='.approval-',delete=False) as file:
            temp = Path(file.name)
            try:
                json.dump({'version':1,'event_id':event_id,'token':token,'approved_at':now},file)
                file.flush()
                os.fsync(file.fileno())
                os.replace(temp,directory/(event_id+'.json'))
                fd=os.open(directory,os.O_RDONLY)
                try: os.fsync(fd)
                finally: os.close(fd)
            finally:
                temp.unlink(missing_ok=True)
    except OSError:
        return {'state':'unavailable','waiting_reason':'exchange'}
    return dict(fields,state='approved')


def _call(db_path, scope, episode_id, event_id, directory, proof_file, now, token):
    if not (isinstance(episode_id,str) and NATIVE.fullmatch(episode_id) and isinstance(event_id,str)
            and HEX.fullmatch(event_id) and isinstance(scope,dict) and scope.get('upstream') and scope.get('username')):
        raise ValueError('invalid_address_review')
    return _read(db_path, scope['upstream'], scope['username'], scope.get('user_id'),
                 lambda db: _review(db, episode_id, event_id, directory, proof_file,
                                    int(time.time() if now is None else now),token))


def preview(db_path, scope, episode_id, event_id, *, exchange_dir='/tracking-alias',
            proof_file='/tracking-proof/pinepods-guids.json', now=None):
    return _call(db_path,scope,episode_id,event_id,exchange_dir,proof_file,now,None)


def approve(db_path, scope, episode_id, event_id, token, *, exchange_dir='/tracking-alias',
            proof_file='/tracking-proof/pinepods-guids.json', now=None):
    if not isinstance(token,str) or not HEX.fullmatch(token):
        raise ValueError('invalid_address_token')
    return _call(db_path,scope,episode_id,event_id,exchange_dir,proof_file,now,token)
