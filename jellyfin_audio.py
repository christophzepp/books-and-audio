"""Three-source audio coordination; each operation is journalled and verified."""
import copy
import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from folder_manager import translate

TICKS = 10000000


def encode(x): return json.dumps(x, sort_keys=True, separators=(',', ':'))
def same(a, b): return a['status'] == b['status'] and abs(a['position'] - b['position']) <= 1


def rows_equal(a, b):
    """Jellyfin returns timestamps in its configured timezone."""
    if len(a) != len(b): return False
    for left, right in zip(a, b):
        left, right = dict(left), dict(right)
        for row in (left, right):
            value = row.get('LastPlayedDate')
            if value:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None: raise ValueError('Jellyfin timestamp has no timezone')
                row['LastPlayedDate'] = parsed.astimezone(timezone.utc)
        if left != right: return False
    return True


def choose(states, baseline=None):
    """Never select the greatest position or the most recent wall clock."""
    if baseline:
        if set(baseline) != set(states): raise ValueError('Source set changed')
        for k, v in states.items():
            if v.get('fingerprint') != baseline[k].get('fingerprint'): raise ValueError('Source identity changed')
        changed = [k for k in states if not same(states[k], baseline[k])]
    else:
        changed = [k for k, v in states.items() if v['status'] != 'not_started']
    if all(same(next(iter(states.values())), x) for x in states.values()): return None
    if not changed: raise ValueError('Sources differ without a recorded change')
    if not all(same(states[changed[0]], states[k]) for k in changed): raise ValueError('Concurrent progress conflict')
    if states[changed[0]]['status'] == 'not_started': raise ValueError('Automatic progress deletion refused')
    return changed[0]


def bind(mapping, inventory, folder_config=None):
    paths = {}
    for item in inventory:
        p = item.get('Path')
        if p in paths: raise ValueError('Duplicate Jellyfin path')
        paths[p] = item
    result = []
    for f in mapping['files']:
        p = f['path']
        if folder_config and 'folder_mappings' in folder_config:
            target = translate(p, 'abs', 'jf', 'audiobooks', folder_config)
        else:
            if not p.startswith('/audiobooks/') or '/..' in p: raise ValueError('Audio path outside scope')
            target = '/data' + p
        item = paths.get(target)
        if not item: return None
        if item['Type'] != 'AudioBook': raise ValueError('Jellyfin item is not an audiobook')
        sources = [s for s in item.get('MediaSources', []) if s.get('Path') == item['Path']]
        size = sources[0].get('Size') if len(sources) == 1 else None
        duration = (item.get('RunTimeTicks') or 0) / TICKS
        if size != f['size'] or not math.isfinite(duration) or abs(duration - f['duration']) > 1:
            raise ValueError('Jellyfin audiobook file differs')
        result.append(dict(id=item['Id'], path=item['Path'], start=f['start'], duration=f['duration'], size=f['size']))
    if not result or len({x['id'] for x in result}) != len(result): raise ValueError('Invalid audio mapping')
    return result


def user_data(items, tracks):
    index = {i['Id']: i for i in items}
    if set(index) != {t['id'] for t in tracks}: raise ValueError('Incomplete audiobook progress')
    result = []
    for t in tracks:
        i = index[t['id']]
        if i.get('Path') != t['path'] or i.get('Type') != 'AudioBook' or abs(i.get('RunTimeTicks', 0)/TICKS-t['duration']) > 1:
            raise ValueError('Audiobook identity changed')
        d = i['UserData']
        tick = d.get('PlaybackPositionTicks')
        if type(tick) is not int or tick < 0 or tick/TICKS > t['duration']+1 or type(d.get('Played')) is not bool:
            raise ValueError('Invalid audiobook progress')
        result.append({k: d.get(k) for k in ('PlaybackPositionTicks', 'Played', 'LastPlayedDate', 'PlayCount')})
    return result


def normalize(rows, tracks, previous=None):
    active = [i for i, d in enumerate(rows) if d['PlaybackPositionTicks'] > 0]
    # Players can leave an older partial chapter behind. Only an unambiguous
    # single changed position may supersede that earlier partial chapter.
    if len(active) > 1 and previous:
        changed = [i for i in active if rows[i] != previous[i]]
        if len(changed) == 1: active = changed
    if len(active) > 1: raise ValueError('Several ambiguous Jellyfin chapter positions')
    finished = all(d['Played'] for d in rows)
    if active:
        i = active[0]; position = tracks[i]['start'] + rows[i]['PlaybackPositionTicks']/TICKS
    elif finished:
        position = tracks[-1]['start'] + tracks[-1]['duration']
    else:
        first = next((i for i,d in enumerate(rows) if not d['Played']), 0)
        if any(d['Played'] for d in rows[first:]): raise ValueError('Noncontiguous completed Jellyfin chapters')
        position = tracks[first]['start']
    status = 'finished' if finished else ('in_progress' if position > 0 or any(d.get('PlayCount') for d in rows) else 'not_started')
    return dict(position=position, status=status, fingerprint=hashlib.sha256(encode(tracks).encode()).hexdigest())


def desired(rows, tracks, goal, date=None):
    target = copy.deepcopy(rows)
    pos = goal['position']
    if pos < 0 or pos > tracks[-1]['start']+tracks[-1]['duration']+1: raise ValueError('Audio position outside book')
    current = next((i for i,t in enumerate(tracks) if pos < t['start']+t['duration']), len(tracks)-1)
    for i,t in enumerate(tracks):
        target[i]['Played'] = goal['status'] == 'finished' or i < current
        target[i]['PlaybackPositionTicks'] = round(min(max(pos-t['start'], 0),t['duration'])*TICKS) if i == current else 0
        # A completed book otherwise has no surviving chapter anchor at an
        # exact boundary. One tick retains it within the one-second tolerance.
        if i == current and goal['status'] == 'finished' and target[i]['PlaybackPositionTicks'] == 0:
            target[i]['PlaybackPositionTicks'] = 1
        if i <= current or goal['status'] == 'finished':
            target[i]['PlayCount'] = max(target[i].get('PlayCount') or 0, 1)
            if date and (i == current or (i < current and not rows[i]['Played'])):
                target[i]['LastPlayedDate'] = date
    return target


class AudioCoordinator:
    def __init__(self, bridge, client):
        self.b, self.jf = bridge, client
        self.inventory = None

    def read(self, tracks):
        return user_data(self.jf.items(ids=[x['id'] for x in tracks], types='AudioBook'), tracks)

    def handle(self, mapping, pair, raw, apply, settle):
        b, id = self.b, mapping['abs_id']; key = '_jellyfin_audio:' + id
        from contextlib import nullcontext
        batch=getattr(b,'_book_observations',None)
        if self.inventory is None:
            self.jf.guard()
            with batch.scope((self,'jf')) if batch is not None else nullcontext():
                self.inventory = self.jf.items(parent=b.config['jellyfin']['audio_library_id'], types='AudioBook')
        tracks = bind(mapping, self.inventory, b.config)
        if tracks is None:
            return None # Keep ABS/BookOrbit working for books Jellyfin cannot index.
        stored = mapping.get('_jellyfin_revision_preview', b.load(key)) if not apply else b.load(key)
        with batch.scope((self,'jf')) if batch is not None else nullcontext():
            rows = self.read(tracks)
        state = normalize(rows, tracks, stored.get('raw'))
        states = dict(pair, jf=state)
        baseline = stored.get('baseline')
        pending = stored.get('pending')
        if pending:
            pair,raw=b.read_pair(mapping)
            rows=self.read(tracks)
            states=dict(pair,jf=normalize(rows,tracks,stored.get('raw')))
            if all(same(s, pending['goal']) for s in states.values()):
                if apply:
                    if getattr(b,'verify_transfer_media',None):b.verify_transfer_media(mapping)
                    b.save(key, dict(baseline=states,raw=rows)); b.save(id, {'baseline': pair})
                return 'unchanged'
            raise RuntimeError('Interrupted Jellyfin transfer requires journal review')
        source = choose(states, baseline)
        if source is None:
            if apply: b.save(key,dict(baseline=states,raw=rows)); b.save(id, {'baseline':pair})
            return 'unchanged'
        if not apply: return 'proposed'
        signature = hashlib.sha256(encode([raw, rows]).encode()).hexdigest()
        candidate = stored.get('candidate', {})
        if candidate.get('signature') != signature:
            candidate = dict(signature=signature,since=b.clock()); stored['candidate']=candidate; b.save(key,stored)
        if b.clock()-candidate['since'] < settle: return 'waiting'
        if self.jf.active_ids() & {t['id'] for t in tracks}: return 'waiting'
        if batch is not None:batch.invalidate()
        b.guard(); self.jf.guard()
        if getattr(b,'verify_transfer_media',None):b.verify_transfer_media(mapping)
        _, fresh = b.read_pair(mapping)
        if encode(fresh) != encode(raw) or encode(self.read(tracks)) != encode(rows):
            raise RuntimeError('Progress changed before Jellyfin transfer')
        goal = states[source]
        finished = None
        if source == 'jf' and goal['status']=='finished':
            dates=[d['LastPlayedDate'] for d in rows if d.get('LastPlayedDate')]
            if not dates: raise RuntimeError('Jellyfin completion date missing')
            finished=int(max(datetime.fromisoformat(x.replace('Z','+00:00')) for x in dates).timestamp()*1000)
        record = dict(id=str(uuid.uuid4()),source=source,goal=goal,before=raw,jellyfin_before=rows,tracks=tracks,stage='prepared')
        stored['pending']=record; b.save(key,stored)
        def checkpoint(stage):
            record['stage']=stage; b.save(key,stored)
        def jf_unchanged():
            if encode(self.read(tracks)) != encode(rows): raise RuntimeError('Concurrent Jellyfin edit')
        # Existing ABS/BO transaction machinery still owns manifest checks and
        # all of its established status/date/CAS checks.
        if source == 'jf':
            total = mapping['abs_tracks'][-1]['start']+mapping['abs_tracks'][-1]['duration']
            latest = raw
            def abs_step(label, fn):
                nonlocal latest
                b.guard(); jf_unchanged()
                _, current = b.read_pair(mapping)
                if encode(current) != encode(latest): raise RuntimeError('Concurrent ABS/BookOrbit edit')
                checkpoint('sending_abs_'+label); fn()
                _, latest = b.read_pair(mapping)
                if latest['bo'] != raw['bo'] or latest['bo_status'] != raw['bo_status']: raise RuntimeError('Concurrent BookOrbit edit')
            if pair['abs']['status']=='finished' and goal['status']!='finished':
                abs_step('reopen',lambda:b.abs.write_finished(id,False))
            if not same(pair['abs'],goal):
                abs_step('position',lambda:b.abs.write_position(id,goal['position'],total))
                if goal['status']=='finished':
                    abs_step('finish',lambda:b.abs.write_finished(id,True,finished_at=finished))
            pair,raw=b.read_pair(mapping)
            if not same(pair['abs'],goal): raise RuntimeError('ABS transfer not confirmed')
        direction = 'bo_to_abs' if source == 'bo' else 'abs_to_bo'
        if not same(pair['abs'],pair['bo']):
            checkpoint('synchronizing_abs_bo')
            b.external_write_guard=jf_unchanged
            try: pair=b.apply(mapping,pair,raw,direction,(baseline or {}).get('abs') and baseline)
            finally: b.external_write_guard=None
        pair, raw = b.read_pair(mapping)
        if not all(same(s,goal) for s in pair.values()): raise RuntimeError('ABS/BookOrbit goal not confirmed')
        date_ms=(raw.get('abs') or {}).get('finishedAt' if goal['status']=='finished' else 'lastUpdate')
        date=datetime.fromtimestamp(date_ms/1000,timezone.utc).isoformat() if date_ms else None
        target=desired(rows,tracks,goal,date)
        for i,(before,after) in enumerate(zip(rows,target)):
            changes={k:v for k,v in after.items() if v!=before.get(k) and v is not None}
            if not changes: continue
            b.guard(); jf_unchanged()
            _, fresh=b.read_pair(mapping)
            if encode(fresh)!=encode(raw): raise RuntimeError('Concurrent source edit')
            checkpoint('sending_jellyfin_'+str(i))
            self.jf.update(tracks[i]['id'],changes)
            updated=self.read(tracks)
            expected=copy.deepcopy(rows); expected[i].update(changes)
            if not rows_equal(updated,expected): raise RuntimeError('Jellyfin write not confirmed')
            rows=updated; record['last_verified']=rows; checkpoint('verified_jellyfin_'+str(i))
        finalpair, finalraw=b.read_pair(mapping); rows=self.read(tracks)
        final=dict(finalpair,jf=normalize(rows,tracks))
        if not all(same(s,goal) for s in final.values()): raise RuntimeError('Three-source transfer not confirmed')
        if getattr(b,'verify_transfer_media',None):b.verify_transfer_media(mapping)
        record['stage']='complete'; record['after']=finalraw; record['jellyfin_after']=rows
        b.write_record(record['id'],key,record,'complete')
        b.save(key,dict(baseline=final,raw=rows)); b.save(id,{'baseline':finalpair})
        b.event(id,'jellyfin_synced',dict(title=mapping['title'],source=source,seconds=round(goal['position'],3),status=goal['status']))
        return 'synced'
