"""Optional BookBridge-owned pairing with an independent ABS/Jellyfin audio edge.

BookBridge owns its ebook readers and alignment. This module never writes a
BookBridge progress record, estimates text positions from percentages, or calls
BookOrbit. Its state namespace cannot overwrite the existing BookOrbit graph.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
from pathlib import PurePosixPath
import uuid

from core import validate_tracks
from folder_manager import translate
from jellyfin_audio import (TICKS, choose, desired, encode, normalize, rows_equal,
                            same, user_data)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def number(value, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError('Invalid audiobook metadata')
    return value


def safe_title(value):
    if not isinstance(value, str) or not value or len(value) > 500 or not value.isprintable():
        raise ValueError('Invalid audiobook title')
    return value


def pair_identity(pair):
    """Only stable identity, never BookBridge's changing reader positions."""
    return {k: pair.get(k) for k in ('id', 'abs_id', 'audio_source', 'audio_source_id',
                                    'ebook_path', 'ebook_source', 'ebook_source_id')}


def build_audio_mapping(pair, item, cfg):
    """Bind a BookBridge ABS pair to the exact ABS player file order."""
    ident = pair.get('abs_id')
    if not isinstance(ident, str) or not ident or pair.get('audio_source') != 'ABS':
        raise ValueError('BookBridge pair does not use Audiobookshelf')
    if item.get('id') != ident or item.get('libraryId') != cfg['auto_discovery']['abs_library_id']:
        raise ValueError('BookBridge audiobook is outside the selected ABS library')
    folder = item.get('path')
    # This also proves that the folder has an unambiguous managed alias.
    translate(folder, 'abs', 'jf', 'audiobooks', cfg)
    files, tracks, seen = [], [], set()
    for row in item.get('media', {}).get('tracks', []):
        metadata = row.get('metadata', {})
        path = metadata.get('path')
        if not isinstance(path, str) or PurePosixPath(path) == PurePosixPath(folder) or not PurePosixPath(path).is_relative_to(PurePosixPath(folder)):
            raise ValueError('Audiobook track is outside its folder')
        target = translate(path, 'abs', 'jf', 'audiobooks', cfg)
        if path in seen: raise ValueError('Duplicate audiobook track')
        seen.add(path)
        size = metadata.get('size')
        if type(size) is not int or size <= 0: raise ValueError('Invalid audiobook size')
        duration, start = number(row.get('duration'), positive=True), number(row.get('startOffset'))
        mtime = number(metadata.get('mtimeMs'))
        tracks.append(dict(id=path, start=start, duration=duration))
        files.append(dict(path=path, jf_path=target, size=size, duration=duration, start=start, mtimeMs=mtime))
    tracks = validate_tracks(tracks)
    duration = tracks[-1]['start'] + tracks[-1]['duration']
    if abs(number(item.get('media', {}).get('duration'), positive=True) - duration) > .001:
        raise ValueError('Audiobook duration does not match its tracks')
    identity = dict(pair=pair_identity(pair), abs_id=ident, library_id=item['libraryId'], files=files)
    return dict(abs_id=ident, bb_id=pair['id'], title=safe_title(pair.get('title') or item['media']['metadata']['title']),
                folder=folder, files=files, abs_tracks=tracks, abs_duration=duration,
                fingerprint=digest(identity), pair=identity['pair'], provider='bookbridge')


def bind_audio(mapping, inventory):
    by_path = {}
    for row in inventory:
        path = row.get('Path')
        if path in by_path: raise ValueError('Duplicate Jellyfin audiobook path')
        by_path[path] = row
    tracks = []
    for file in mapping['files']:
        item = by_path.get(file['jf_path'])
        if item is None: return None
        sources = [s for s in item.get('MediaSources', []) if s.get('Path') == file['jf_path']]
        if (item.get('Type') != 'AudioBook' or len(sources) != 1 or sources[0].get('Size') != file['size']
                or abs(number(item.get('RunTimeTicks'), positive=True) / TICKS - file['duration']) > 1):
            raise ValueError('Jellyfin audiobook file differs from Audiobookshelf')
        tracks.append(dict(id=item['Id'], path=item['Path'], start=file['start'], duration=file['duration'], size=file['size']))
    if len({r['id'] for r in tracks}) != len(tracks): raise ValueError('Duplicate Jellyfin track identity')
    return tracks


def provider_preview(bridge):
    """Read-only provider validation for the activation dialog."""
    bridge.guard()
    coordinator = BookBridgeCoordinator(bridge)
    mappings, unsupported = coordinator.mappings()
    matched, awaiting = 0, []
    if bridge.jellyfin:
        bridge.jellyfin.guard()
        inventory = bridge.jellyfin.items(parent=bridge.config['jellyfin']['audio_library_id'], types='AudioBook')
        for mapping in mappings:
            tracks = bind_audio(mapping, inventory)
            if tracks is None: awaiting.append(mapping['title'])
            else:
                coordinator.read(mapping, tracks)
                matched += 1
    return dict(provider='bookbridge', mapped=len(mappings), matched_jellyfin=matched,
                awaiting_index=awaiting, unsupported=unsupported,
                ebook_progress_owner='BookBridge', jellyfin_ebook_supported=False,
                mappings=[{'abs_id': m['abs_id'], 'bb_id': m['bb_id'], 'title': m['title']} for m in mappings])


class BookBridgeCoordinator:
    def __init__(self, bridge):
        self.b, self.jf = bridge, bridge.jellyfin

    def mappings(self):
        pairs = self.b.bookbridge.inventory()
        mappings, unsupported, seen = [], [], set()
        for pair in pairs:
            if pair.get('audio_source') != 'ABS' or not pair.get('abs_id'):
                unsupported.append(dict(title=safe_title(pair['title']), reason='Diese BookBridge-Zuordnung verwendet kein Audiobookshelf-Hörbuch.'))
                continue
            if pair['abs_id'] in seen: raise ValueError('Duplicate BookBridge audiobook mapping')
            seen.add(pair['abs_id'])
            mappings.append(build_audio_mapping(pair, self.b.abs.item(pair['abs_id']), self.b.config))
        return mappings, unsupported

    def verify_mapping(self, expected):
        pairs = [p for p in self.b.bookbridge.inventory() if p.get('id') == expected['bb_id']]
        if len(pairs) != 1: raise ValueError('BookBridge mapping disappeared or became ambiguous')
        current = build_audio_mapping(pairs[0], self.b.abs.item(expected['abs_id']), self.b.config)
        if current['fingerprint'] != expected['fingerprint']: raise ValueError('BookBridge audiobook identity changed')

    def read(self, mapping, tracks, previous=None):
        from sync import normalize_abs
        raw = self.b.abs.progress(mapping['abs_id'])
        if raw is not None and raw.get('libraryItemId') != mapping['abs_id']:
            raise ValueError('ABS returned another audiobook progress record')
        rows = user_data(self.jf.items(ids=[t['id'] for t in tracks], types='AudioBook'), tracks)
        states = dict(abs=normalize_abs(raw, mapping), jf=normalize(rows, tracks, previous))
        return states, dict(abs=raw, jf=rows)

    def handle(self, mapping, tracks, apply, settle):
        b, ident = self.b, mapping['abs_id']
        key = '_bookbridge_audio:' + ident
        stored = b.load(key)
        states, raw = self.read(mapping, tracks, stored.get('raw'))
        if stored.get('pending'):
            pending = stored['pending']
            if pending.get('mapping') != mapping['fingerprint']:
                raise ValueError('Interrupted transfer belongs to a changed BookBridge mapping')
            if all(same(s, pending['goal']) for s in states.values()):
                if apply:
                    pending.update(stage='reconciled', after=raw)
                    b.write_record(pending['id'], key, pending, 'complete')
                    b.save(key, dict(baseline=states, raw=raw['jf']))
                return 'unchanged'
            raise RuntimeError('Interrupted BookBridge audio transfer requires journal review')
        source = choose(states, stored.get('baseline'))
        if source is None:
            if apply: b.save(key, dict(baseline=states, raw=raw['jf']))
            return 'unchanged'
        if not apply: return 'proposed'
        signature = digest(raw)
        candidate = stored.get('candidate', {})
        if candidate.get('signature') != signature:
            candidate = dict(signature=signature, since=b.clock()); stored['candidate']=candidate; b.save(key, stored)
        if b.clock() - candidate['since'] < settle or self.jf.active_ids() & {t['id'] for t in tracks}:
            return 'waiting'
        goal = states[source]
        finished = None
        if source == 'jf' and goal['status'] == 'finished':
            dates = [r['LastPlayedDate'] for r in raw['jf'] if r.get('LastPlayedDate')]
            if not dates: raise ValueError('Jellyfin completion date is missing')
            stamps = [dt.datetime.fromisoformat(value.replace('Z', '+00:00')) for value in dates]
            if any(value.tzinfo is None for value in stamps): raise ValueError('Jellyfin completion date has no timezone')
            finished = int(max(stamps).timestamp() * 1000)
        def unchanged():
            b.guard(); self.jf.guard(); self.verify_mapping(mapping)
            _, fresh = self.read(mapping, tracks, raw['jf'])
            if encode(fresh) != encode(raw): raise RuntimeError('Concurrent ABS/Jellyfin progress change')
        unchanged()
        record = dict(id=str(uuid.uuid4()), source=source, goal=goal, before=copy.deepcopy(raw),
                      mapping=mapping['fingerprint'], stage='prepared')
        stored['pending'] = record; b.save(key, stored)
        def stage(label):
            record['stage'] = label; record['last_verified'] = copy.deepcopy(raw); b.save(key, stored)
        if source == 'jf':
            def step(label, action, expected_position, allowed_statuses):
                nonlocal raw, states
                unchanged(); stage('sending_abs_' + label); action()
                current, fresh = self.read(mapping, tracks, raw['jf'])
                if not rows_equal(fresh['jf'], raw['jf']): raise RuntimeError('Jellyfin changed during ABS transfer')
                if abs(current['abs']['position'] - expected_position) > 1 or current['abs']['status'] not in allowed_statuses:
                    raise RuntimeError('ABS step effect was not confirmed')
                raw, states = fresh, current
                stage('verified_abs_' + label)
            if states['abs']['status'] == 'finished' and goal['status'] != 'finished':
                step('reopen', lambda: b.abs.write_finished(ident, False), 0, {'in_progress'})
            if states['abs'].get('missing') or abs(states['abs']['position'] - goal['position']) > 1:
                step('position', lambda: b.abs.write_position(ident, goal['position'], mapping['abs_duration']), goal['position'], {'in_progress'})
            if states['abs']['status'] != goal['status']:
                step('status', lambda: b.abs.write_finished(ident, goal['status'] == 'finished', finished_at=finished), goal['position'], {goal['status']})
        else:
            date_ms = (raw['abs'] or {}).get('finishedAt' if goal['status'] == 'finished' else 'lastUpdate')
            date = dt.datetime.fromtimestamp(date_ms / 1000, dt.timezone.utc).isoformat() if date_ms else None
            target = desired(raw['jf'], tracks, goal, date)
            for index, (before, after) in enumerate(zip(copy.deepcopy(raw['jf']), target)):
                values = {k: v for k, v in after.items() if v != before.get(k) and v is not None}
                if not values: continue
                unchanged(); stage('sending_jellyfin_' + str(index)); self.jf.update(tracks[index]['id'], values)
                current, fresh = self.read(mapping, tracks, raw['jf'])
                expected = copy.deepcopy(raw['jf']); expected[index].update(values)
                if fresh['abs'] != raw['abs'] or not rows_equal(fresh['jf'], expected):
                    raise RuntimeError('Jellyfin transfer or unchanged ABS source was not confirmed')
                raw, states = fresh, current; stage('verified_jellyfin_' + str(index))
        self.verify_mapping(mapping)
        states, raw = self.read(mapping, tracks, raw['jf'])
        if not all(same(state, goal) for state in states.values()): raise RuntimeError('BookBridge audio edge transfer not confirmed')
        record.update(stage='complete', after=raw)
        b.write_record(record['id'], key, record, 'complete')
        b.save(key, dict(baseline=states, raw=raw['jf']))
        b.event(ident, 'bookbridge_audio_synced', dict(title=mapping['title'], source=source,
                seconds=round(goal['position'], 3), status=goal['status']))
        return 'synced'

    def cycle(self, apply=False, settle=30):
        from sync import now, dump
        import os
        b = self.b
        b.guard()
        mappings, unsupported = self.mappings()
        present = {m['abs_id'] for m in mappings}
        for state_id, value in b.db.execute("SELECT id,value FROM state WHERE id LIKE '_bookbridge_audio:%'"):
            if json.loads(value).get('pending') and state_id.split(':', 1)[1] not in present:
                raise RuntimeError('Eine BookBridge-Zuordnung mit ungeklärtem Abgleich ist verschwunden')
        if apply: b.save('_bookbridge_mapping_inventory', {'mappings': mappings, 'unsupported': unsupported})
        result = dict(at=now(), provider='bookbridge', mode='apply' if apply else 'dry-run', mapped=len(mappings),
                      synced=0, unchanged=0, waiting=0, blocked=[], paused=[], proposed=[],
                      bookbridge=dict(pairs=len(mappings), unsupported=unsupported,
                                      ebook_progress_owner='BookBridge', jellyfin_ebook_supported=False))
        result['jellyfin_audio'] = dict(matched=0, awaiting_index=[])
        inventory = None
        if self.jf:
            self.jf.guard()
            inventory = self.jf.items(parent=b.config['jellyfin']['audio_library_id'], types='AudioBook')
        for mapping in mappings:
            try:
                if not self.jf:
                    result['waiting'] += 1
                    continue
                tracks = bind_audio(mapping, inventory)
                if tracks is None:
                    result['waiting'] += 1; result['jellyfin_audio']['awaiting_index'].append(mapping['title']); continue
                result['jellyfin_audio']['matched'] += 1
                action = self.handle(mapping, tracks, apply, settle)
                if action == 'proposed': result['proposed'].append(dict(title=mapping['title'], direction='abs_jellyfin'))
                else: result[action] += 1
            except Exception as exc:
                reason = str(exc) if isinstance(exc, (RuntimeError, ValueError)) or type(exc).__name__ == 'RequestError' else type(exc).__name__
                issue = dict(title=mapping['title'], abs_id=mapping['abs_id'], reason=reason)
                result['blocked'].append(issue)
                key = '_bookbridge_error:' + mapping['abs_id']
                if apply and b.load(key).get('reason') != reason:
                    b.save(key, dict(reason=reason)); b.event(mapping['abs_id'], 'blocked', issue)
                    b.alert(key, 'BookBridge-Hörbuchabgleich zurückgestellt: ' + mapping['title'] + ' — ' + reason)
        (b.root / 'last-run.json.tmp').write_text(dump(result))
        os.replace(b.root / 'last-run.json.tmp', b.root / 'last-run.json')
        print(dump({k: v for k, v in result.items() if k != 'proposed'}), flush=True)
        if apply: b.deliver_notifications()
        return result
