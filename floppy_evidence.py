"""Read a recent, account-bound export of Floppy podcast history, or produce it inside stock Floppy.

Run this file via stdin inside the Floppy application container. Only the export
operation imports Django. The admin web process needs no Floppy credentials.
"""
import argparse
from datetime import datetime
import hashlib
import json
import math
import os
import stat
import sys
import time


MAX_ROWS = 25_000
MAX_BYTES = 8 * 1024 * 1024
MAX_AGE = 3 * 3600


def digest(values):
    data = json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def scope(upstream, username, floppy_user, relay_url):
    if not all(isinstance(v, str) and v.strip() for v in (upstream, username, floppy_user, relay_url)):
        raise ValueError('incomplete account scope')
    return digest([upstream.rstrip('/'), username, floppy_user, relay_url.rstrip('/')])


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def stamp(value):
    return value.timestamp() if isinstance(value, datetime) and value.tzinfo else None


def load_snapshot(path):
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError('invalid evidence file')
        raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('oversized evidence')
        return json.loads(raw)


def attach_floppy_evidence(data, path, *, upstream, username, floppy_user, relay_url, now=None, max_age=MAX_AGE):
    """Return public fields. Even disabled/invalid exports must strip the private match keys."""
    public = dict(data)
    match = public.pop('delivery_match', None)
    if data.get('state') != 'ok':
        return public
    public['target'] = {'state': 'unverified'}
    source, event = data['source'], data['event']
    if (not path or not match or match.get('unique') is not True or source.get('present_in_last_scan') is not True
            or event.get('stage') not in ('offered', 'acknowledged')):
        return public
    try:
        now = time.time() if now is None else now
        snapshot = load_snapshot(path)
        observed = snapshot['observed_at']
        synced = snapshot['synced_at']
        ready = event['ready_at']
        age = min(max_age, MAX_AGE)
        if (snapshot['version'] != 1 or snapshot['scope'] != scope(upstream, username, floppy_user, relay_url)
                or not all(number(v) for v in (observed, synced, ready, source['observed_at']))
                or not now-age <= observed <= now or not ready <= synced <= observed
                or not now-age <= source['observed_at'] <= now
                or event.get('cursor') is not None and snapshot['cursor'] < event['cursor']):
            return public
        # Prefer the exact URL, seconds and timestamp actually emitted after RSS alias resolution.
        identity = match.get('wire_hash') or match['lookup_hash']
        entry = snapshot['episodes'].get(identity)
        completed = source['completed']
        if type(completed) is not bool or not entry or entry['completed'] is not completed:
            return public
        total = source['total']
        position = source['position']
        if not all(number(value) for value in (total, position)) or total <= 0 or position < 0:
            return public
        expected = int(total if completed else min(position, total - 6))
        if expected <= 0 or type(entry['seconds']) is not int or entry['seconds'] != expected:
            return public
        if match.get('wire_hash') and (match.get('wire_unique') is not True
                or not number(match.get('wire_position')) or int(match['wire_position']) != expected):
            return public
        if not number(entry['updated_at']) or not ready <= entry['updated_at'] <= observed:
            return public
        if source.get('updated_at') is not None and source['updated_at'] > entry['updated_at']:
            return public
        if completed:
            action_time = match.get('wire_timestamp') or event.get('action_timestamp')
            action = stamp(datetime.fromisoformat(action_time.replace('Z', '+00:00')))
            if action is None or not number(entry['end_at']) or abs(entry['end_at'] - action) > 0.001:
                return public
        public['target'] = dict(state='verified', observed_at=observed, synced_at=synced,
                                position=expected, completed=completed)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
        pass
    return public


def export_snapshot(*, floppy_user, relay_url, pine_user, upstream):
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    from django.db.backends.signals import connection_created

    def read_only(sender, connection, **kwargs):
        with connection.cursor() as cursor:
            if connection.vendor == 'sqlite':
                cursor.execute('PRAGMA query_only=ON')
            elif connection.vendor == 'postgresql':
                cursor.execute('SET default_transaction_read_only=on')
            else:
                raise ValueError('unsupported database')

    connection_created.connect(read_only, weak=False)
    django.setup()
    from django.db import connection, transaction
    from django.db.models import Count
    from app.models import Podcast, PodcastEpisode, Sources, Status
    from integrations.imports.helpers import decrypt_or_raise
    from integrations.models import GPodderAccount

    with transaction.atomic():
        with connection.cursor() as cursor:
            if connection.vendor == 'postgresql':
                cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            elif connection.vendor == 'sqlite':
                cursor.execute('PRAGMA query_only')
                if cursor.fetchone()[0] != 1:
                    raise ValueError('read-only transaction required')
            else:
                raise ValueError('unsupported database')
        account = GPodderAccount.objects.only('user_id','username','server_url','episode_actions_since',
            'last_sync_at','connection_broken').get(user__username=floppy_user)
        if (account.connection_broken or decrypt_or_raise(account.server_url).rstrip('/') != relay_url.rstrip('/')
                or decrypt_or_raise(account.username) != pine_user or not account.last_sync_at):
            raise ValueError('Floppy GPodder connection scope or state differs')
        if type(account.episode_actions_since) is not int or account.episode_actions_since < 0:
            raise ValueError('invalid Floppy GPodder cursor')
        observed = time.time()
        rows = Podcast.objects.filter(user_id=account.user_id, episode_id__isnull=False).order_by('-created_at','-pk').values(
            'episode__show__rss_feed_url','episode__audio_url','episode__show_id','show_id','episode_id',
            'item__source','status','played_up_to_seconds','position_updated_at','created_at','end_date')[:MAX_ROWS]
        entries, feeds = {}, set()
        for row in rows.iterator(chunk_size=500):
            feed, audio = row['episode__show__rss_feed_url'], row['episode__audio_url']
            if not feed or not audio:
                continue
            identity = digest([feed, audio])
            if identity in entries:
                continue  # Floppy uses newest-created history rows for subsequent listens.
            entries[identity] = None  # a more recent unsupported row must shadow previous listens
            when = max(filter(None, (stamp(row['created_at']), stamp(row['position_updated_at']))), default=None)
            seconds = row['played_up_to_seconds']
            completed = row['status'] == Status.COMPLETED.value
            if (row['item__source'] != Sources.GPODDER.value or row['episode__show_id'] != row['show_id']
                    or type(seconds) is not int or seconds <= 0 or when is None
                    or completed != (row['end_date'] is not None)
                    or not completed and row['status'] != Status.IN_PROGRESS.value):
                continue
            entries[identity] = dict(seconds=seconds, completed=completed, updated_at=when, end_at=stamp(row['end_date']))
            feeds.add(feed)
        # A feed can exist under more than one show ID (e.g. an iTunes copy).
        # Never use progress from a duplicate identity as evidence for the selected entry.
        feed_list = sorted(feeds)
        for start in range(0, len(feed_list), 300):
            duplicate = PodcastEpisode.objects.filter(show__rss_feed_url__in=feed_list[start:start+300]).order_by().values(
                'show__rss_feed_url','audio_url').annotate(matches=Count('pk')).filter(matches__gt=1)
            for row in duplicate.iterator(chunk_size=500):
                entries.pop(digest([row['show__rss_feed_url'], row['audio_url']]), None)
        return dict(version=1, observed_at=observed, synced_at=stamp(account.last_sync_at),
                    cursor=account.episode_actions_since, scope=scope(upstream, pine_user, floppy_user, relay_url),
                    episodes={key:value for key,value in entries.items() if value is not None})


def main():
    parser = argparse.ArgumentParser(description='Export read-only evidence for one Floppy GPodder connection.')
    for option in ('floppy-user','relay-url','pine-user','upstream'):
        parser.add_argument('--'+option, required=True)
    args = parser.parse_args()
    try:
        # Framework startup output goes to stderr so stdout is always a single JSON document.
        from contextlib import redirect_stdout
        with redirect_stdout(sys.stderr):
            snapshot = export_snapshot(**vars(args))
            payload = json.dumps(snapshot, separators=(',', ':'))
            if len(payload.encode('utf-8')) > MAX_BYTES:
                raise ValueError('evidence file too large')
        print(payload)
    except Exception:
        # ORM or decryption errors may contain connection details.
        sys.exit('Floppy evidence unavailable: check the configured user, relay and database.')


if __name__ == '__main__':
    main()
