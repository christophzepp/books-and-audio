"""Export scoped PinePods publisher GUID evidence for currently blocked deliveries (PostgreSQL)."""
import argparse
from contextlib import closing
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time


def digest(value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def required(db_path, upstream, pine_user):
    uri = Path(db_path).absolute().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        def state(key):
            row = db.execute('SELECT value FROM state WHERE id=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None
        scope = json.loads(state('native_scope'))
        if state('delivery_mode') != 'native-state' or scope[:2] != [upstream.rstrip('/'), pine_user]:
            raise ValueError('The active relay account does not match')
        wanted = {}
        for ident, feed, old in db.execute("""SELECT s.id,json_extract(s.snapshot,'$.podcast'),
                json_extract(s.snapshot,'$.episode')
            FROM native_outbox o JOIN native_tracking_events e ON e.event_id=o.event_id
            JOIN native_tracking_episodes s ON s.id=e.episode_id
            WHERE o.reason IN ('tracking_rss_identity_unresolved','legacy_blocked')
            AND e.generation=s.generation AND e.sequence=s.sequence AND s.scan_number=?
            AND NOT EXISTS (SELECT 1 FROM native_delivery_receipts r WHERE r.event_id=e.event_id)
            LIMIT 10001""",
            (state('native_inventory_scan'),)):
            if not re.fullmatch(r'[1-9][0-9]{0,18}(?::[1-9][0-9]{0,18}){2}',ident):
                raise ValueError('Invalid native episode ID')
            if scope[2] is not None and int(ident.split(':')[0]) != scope[2]:
                raise ValueError('Unexpected native user ID')
            if len(wanted) >= 10000:
                raise ValueError('Too many outstanding identities for a complete proof')
            wanted[ident] = digest([feed, old])
    return wanted


def required_from_container(container, upstream, pine_user, *, run=subprocess.run):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', container):
        raise ValueError('Invalid relay container name')
    # Send precisely the local read-only query, including to pre-upgrade images
    # that do not ship this helper. The normal container UID reads a fixed path.
    script = '\n'.join((
        'from contextlib import closing',
        'from pathlib import Path',
        'import hashlib, json, re, sqlite3, sys',
        inspect.getsource(digest),
        inspect.getsource(required),
        "print(json.dumps(required('/tracking/state/relay.sqlite', sys.argv[1], sys.argv[2])))",
    ))
    process = run(['docker', 'exec', '-i', container, 'python', '-I', '-B', '-',
                   upstream, pine_user], input=script.encode(), stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, timeout=20, check=False)
    if process.returncode or len(process.stdout) > 2 * 1024 * 1024:
        raise ValueError('Could not read the scoped relay candidates')
    wanted = json.loads(process.stdout)
    if (not isinstance(wanted, dict) or len(wanted) > 10000 or any(
            not re.fullmatch(r'[1-9][0-9]{0,18}(?::[1-9][0-9]{0,18}){2}', ident)
            or not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)
            for ident, value in wanted.items())):
        raise ValueError('Invalid response to the relay candidate query')
    return wanted


def query_db(container, id_batch, *, run=subprocess.run):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}',container):
        raise ValueError('Invalid database container name')
    # Only validated positive integers become SQL text. No names or private URLs
    # are injected. The process uses the database container's own local access.
    numbers=[]
    for ident in id_batch:
        if not re.fullmatch(r'[1-9][0-9]{0,18}(?::[1-9][0-9]{0,18}){2}',ident):
            raise ValueError('Invalid database identity')
        numbers.append('('+','.join(str(int(x)) for x in ident.split(':'))+')')
    sql='''SELECT json_build_array(u.userid,p.podcastid,e.episodeid,u.username,p.feedurl,
        e.episodeurl,e.episodeguid)::text FROM (VALUES '''+', '.join(numbers)+''') AS wanted(uid,pid,eid)
        JOIN public."Users" u ON u.userid=wanted.uid
        JOIN public."Podcasts" p ON p.podcastid=wanted.pid AND p.userid=u.userid
        JOIN public."Episodes" e ON e.episodeid=wanted.eid AND e.podcastid=p.podcastid
        WHERE e.episodeguid IS NOT NULL AND e.episodeguid<>'' AND NOT EXISTS (
          SELECT 1 FROM public."Episodes" duplicate WHERE duplicate.podcastid=e.podcastid
          AND duplicate.episodeid<>e.episodeid AND duplicate.episodeguid=e.episodeguid)'''
    proc=run(['docker','exec','-i','-e','PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=15000',
              container,'sh','-ec','exec psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'],
             input=sql.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25,check=False)
    if proc.returncode != 0 or len(proc.stdout)>8*1024*1024:
        raise ValueError('PinePods read-only query failed')
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def refresh(relay_db, db_container, upstream, pine_user, output, *, query=query_db, relay_container=None):
    output = Path(output)
    if bool(relay_db) == bool(relay_container):
        raise ValueError('Select one relay source')
    wanted = (required(relay_db, upstream, pine_user) if relay_db
              else required_from_container(relay_container, upstream, pine_user))
    ids = list(wanted)
    result = {}
    for start in range(0,len(ids),128):
        batch = ids[start:start+128]
        for uid, pid, eid, username, feed, old, guid in query(db_container,batch):
            ident=f'{uid}:{pid}:{eid}'
            if (username != pine_user or ident not in batch or wanted[ident] != digest([feed,old])
                    or not isinstance(guid,str) or not 0<len(guid)<=8192
                    or any(ord(c)<32 for c in guid)):
                continue
            if ident in result:
                raise ValueError('Ambiguous database identity')
            result[ident]={'source':wanted[ident],'guid':digest(guid)}
    summary={'schema':1,'scope':digest([upstream.rstrip('/'),pine_user]),
             'observed_at':int(time.time()),'rows':result}
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.guid-proof-', delete=False) as tmp:
        path=Path(tmp.name)
        try:
            body=json.dumps(summary,separators=(',',':')).encode()
            if len(body)>8*1024*1024:
                raise ValueError('Source evidence exceeds its size limit')
            tmp.write(body)
            tmp.flush()
            os.fsync(tmp.fileno())
            os.replace(path,output)
        finally:
            path.unlink(missing_ok=True)
    return len(result)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--relay-db')
    source.add_argument('--relay-container')
    for name in ('db-container','upstream','pine-user','output'):
        p.add_argument('--'+name,required=True)
    try:
        print('Bestätigte Folgenkennungen:',refresh(**vars(p.parse_args())))
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError, TypeError, KeyError):
        p.exit(1,'Der kontogebundene PinePods-Nachweis konnte nicht erstellt werden.\n')
