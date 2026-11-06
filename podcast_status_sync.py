"""Status edges across independently verified podcast audio versions.

ABS/Jellyfin retain their ordinary shared-file position synchronization.  This
separate journal never converts or copies seconds between that file and Pine's
version.  Every mutation has an own-version marker and a checked raw preimage.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
import os
import uuid

from jellyfin_audio import TICKS, rows_equal
from pinepods_client import action_epoch
from podcast_policy import validate_primary, preferred_change

NAMESPACE = '_podcast_status:'
STATUSES = frozenset(('not_started', 'in_progress', 'finished'))
REASONS = {
    'variant_proof_required': 'Different audio versions require independently verified media evidence.',
    'variant_proof_stale': 'The different audio version must be revalidated before a status write.',
    'variant_identity_changed': 'The verified status-only participant identity changed.',
    'variant_initial_status_conflict': 'Existing started and completed states conflict; initial status needs review.',
    'variant_concurrent_status_change': 'Several participants changed to different statuses.',
    'variant_settle_wait': 'Waiting for a stable status and playback position.',
    'variant_active_player': 'An active player prevents the status transfer.',
    'variant_pending_review': 'An interrupted status transfer requires verified readback; it is not retried.',
    'variant_full_enrollment_review': 'An existing full-position PinePods enrollment requires an explicit mode migration.',
    'variant_status_disabled': 'Status-only synchronization is disabled; its history is retained.',
    'variant_local_position_conflict': 'The shared-file players retain different intermediate positions; review is required.',
    'variant_progress_missing': 'Previously observed progress disappeared; this is not a reset.',
    'variant_core_pending': 'The shared-file position transfer must finish first.',
}


class StatusConflict(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(REASONS[code])


def state_key(mapping_id):
    if not isinstance(mapping_id, str) or not mapping_id.startswith('_podcast:'):
        raise ValueError('Invalid status-only podcast mapping')
    return NAMESPACE + mapping_id


def history_ids(bridge):
    result = set()
    for key, value in bridge.db.execute('SELECT id,value FROM state WHERE id LIKE ?', (NAMESPACE+'%',)):
        if key.startswith(NAMESPACE) and (json.loads(value).get('baseline') or json.loads(value).get('pending')):
            result.add(key[len(NAMESPACE):])
    for (key,) in bridge.db.execute('SELECT DISTINCT book FROM operations WHERE book LIKE ?', (NAMESPACE+'%',)):
        if key.startswith(NAMESPACE): result.add(key[len(NAMESPACE):])
    return result


def pending(bridge, mapping_id):
    key = state_key(mapping_id)
    return bool(bridge.load(key).get('pending') or bridge.db.execute(
        "SELECT 1 FROM operations WHERE book=? AND status='pending' LIMIT 1", (key,)).fetchone())


def choose_status(states, baseline=None, raw=None, previous=None, *, primary_source='auto'):
    """Choose status edges only; never infer recency from different clocks."""
    validate_primary(primary_source)
    if set(states) != {'abs','jf','pinepods','gpodder'}:
        raise StatusConflict('variant_identity_changed')
    if any(value.get('status') not in STATUSES for value in states.values()):
        raise StatusConflict('variant_identity_changed')
    if states['abs']['status'] != states['jf']['status']:
        raise StatusConflict('variant_core_pending')
    if baseline:
        if set(baseline) != set(states): raise StatusConflict('variant_identity_changed')
        for name, old in baseline.items():
            if states[name].get('missing') and not old.get('missing') and old['status'] != 'not_started':
                raise StatusConflict('variant_progress_missing')
        if raw and previous and previous['pinepods'].get('native_position') is not None and raw['pinepods'].get('native_position') is None:
            raise StatusConflict('variant_progress_missing')
        baseline = copy.deepcopy(baseline)
        # A gpodder play has a documented native-position echo, while its
        # completion flag remains unchanged.  Ignore only this proven echo.
        if raw and previous:
            old, new = previous['pinepods'], raw['pinepods']
            if (new.get('gpodder_revision') != old.get('gpodder_revision')
                    and new.get('action') == 'play'
                    and new.get('native_completed') == old.get('native_completed')
                    and new.get('native_position') is not None
                    and new.get('gpodder_raw_position') is not None
                    and abs(new['native_position']-new['gpodder_raw_position']) <= 1):
                baseline['pinepods'] = states['pinepods']
    all_status = {value['status'] for value in states.values()}
    if len(all_status) == 1: return None
    changed = ([name for name in states if states[name]['status'] != baseline[name]['status']]
               if baseline else [name for name in states if states[name]['status'] != 'not_started'])
    if not changed or len({states[name]['status'] for name in changed}) != 1:
        preferred = preferred_change(states, changed, primary_source)
        if preferred is None:
            raise StatusConflict('variant_concurrent_status_change' if baseline else 'variant_initial_status_conflict')
        changed = [preferred]
    source = changed[0]
    if states[source]['status'] == 'not_started' and (not baseline or states[source].get('missing')):
        raise StatusConflict('variant_progress_missing')
    return source


def core_position(raw, duration):
    """One shared audio version needs one local restart position, never Pine's."""
    positions = [(raw.get('abs') or {}).get('currentTime', 0), raw['jf']['PlaybackPositionTicks']/TICKS]
    useful = [p for p in positions if 0 < p < duration-1]
    if useful and max(useful)-min(useful) > 1:
        raise StatusConflict('variant_local_position_conflict')
    return useful[0] if useful else 1


def plan_fingerprint(plan):
    from podcast_sync import fingerprint
    stable=copy.deepcopy(plan)
    for step in stable:
        if step.get('service')=='pine':
            step['value'].pop('timestamp',None)
            step['value'].pop('_server_timestamp',None)
    return fingerprint(stable)


def version_receipt(proof):
    return {key:proof[key] for key in ('proof_fingerprint','own_duration')}


def measurement_receipt(proof):
    return {key:proof[key] for key in ('source_fingerprint','proof_fingerprint','own_duration','validated_at','evidence_sha256')}


def same_user_progress(left,right):
    """Only for a proven, no-pending audio-version renewal.

    These three reader fields are synthesized using the caller's measured
    duration. Every actual native/gpodder receipt, position, status and peer
    field remains subject to the ordinary exact raw comparison.
    """
    from podcast_sync import raw_equal
    if set(left)!=set(right) or 'pinepods' not in left:return False
    for raw in (left,right):
        observed=raw['pinepods'].get('native_observed_revision')
        if not isinstance(observed,str) or len(observed)!=64 or any(c not in '0123456789abcdef' for c in observed):return False
    left=copy.deepcopy(left);right=copy.deepcopy(right)
    for value in (left['pinepods'],right['pinepods']):
        for key in ('own_duration','native_revision','revision'):value.pop(key,None)
    return raw_equal(left,right)


class StatusCoordinator:
    def __init__(self, coordinator):
        self.c, self.b, self.pine = coordinator, coordinator.b, coordinator.pine

    @staticmethod
    def mark(mapping, code=None, status=None):
        mapping['_pine_mode'] = 'status_only'
        if code: mapping['_pine_status_reason_code'] = code
        else: mapping.pop('_pine_status_reason_code', None)
        if status: mapping['_pine_status'] = status

    def binding(self, mapping, proof):
        from podcast_sync import fingerprint
        return fingerprint(dict(mapping=mapping['fingerprint'], jf=mapping['jf']['fingerprint'],
            accounts=self.c.accounts, source=proof['source_fingerprint'],
            feed_url=proof['feed_url'], enclosure_url=proof['enclosure_url'],
            native_episode_id=proof['native_episode_id'], native_user_id=proof['native_user_id']))

    def read(self, mapping, proof):
        states, raw = self.c.read(mapping)
        if set(states) != {'abs','jf'}: raise StatusConflict('variant_identity_changed')
        snapshot = self.pine.read_variant_status(proof['feed_url'], proof['enclosure_url'], proof['own_duration'],
            expected_episode_id=proof['native_episode_id'], expected_user_id=proof['native_user_id'])
        observed=snapshot.get('native_observed_revision')
        if not isinstance(observed,str) or len(observed)!=64 or any(c not in '0123456789abcdef' for c in observed):
            raise StatusConflict('variant_identity_changed')
        states['pinepods'] = {key:snapshot[key] for key in ('position','status','missing')}
        states['gpodder'] = dict(position=snapshot.get('gpodder_raw_position') or 0,
            status=snapshot['gpodder_status'], missing=snapshot['gpodder_missing'])
        raw['pinepods'] = snapshot
        return states, raw

    def baseline(self, key, binding, states, raw, proof):
        self.b.save(key, dict(schema=2, binding=binding, version=version_receipt(proof),baseline=states,raw=raw,enrolled=True))

    def persist(self, key, stored, record, *, complete=False, core=None):
        from podcast_sync import encode
        if self.b.db.execute('PRAGMA synchronous').fetchone()[0] not in (2,3):
            raise RuntimeError('Status transfer requires durable SQLite settings')
        with self.b.db:
            self.b.db.execute('INSERT OR REPLACE INTO state(id,value) VALUES(?,?)', (key,encode(stored)))
            self.b.db.execute('INSERT OR REPLACE INTO operations(id,book,value,status) VALUES(?,?,?,?)',
                (record['id'],key,encode(record),'complete' if complete else 'pending'))
            if core is not None:
                core_key,core_value=core
                self.b.db.execute('INSERT OR REPLACE INTO state(id,value) VALUES(?,?)',(core_key,encode(core_value)))
        for path in (self.b.root/'state.sqlite',self.b.root):
            fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            try:os.fsync(fd)
            finally:os.close(fd)

    def refreshed(self, mapping, binding=None, apply=True, *, version=None, receipt=None):
        proof=mapping.get('_variant_proof',{})
        # A journal may cross a normal cycle boundary while the original
        # measured receipt is still fresh. Reuse that exact receipt only; this
        # does not renew its timestamp or authorize a changed representation.
        if (receipt is not None and proof.get('ready') is True
                and measurement_receipt(proof)==receipt and version_receipt(proof)==version
                and self.binding(mapping,proof)==binding
                and 0<=self.b.clock()-receipt['validated_at']<=150):
            return proof
        if not (proof.get('fresh') is True and 0<=self.b.clock()-proof.get('validated_at',-1)<=150):
            proof = self.c.variant_proof(mapping, refresh=True, apply=apply)
        if not proof.get('ready') or proof.get('fresh') is not True:
            self.mark(mapping,'variant_proof_stale','waiting')
            return None
        if (binding is not None and self.binding(mapping,proof) != binding
                or version is not None and version_receipt(proof)!=version):
            raise StatusConflict('variant_identity_changed')
        return proof

    def read_renewed(self,mapping,previous_proof,proof,previous_raw):
        from podcast_sync import raw_equal
        current,fresh_raw=self.read(mapping,proof)
        changed=version_receipt(previous_proof)!=version_receipt(proof)
        if (changed and not same_user_progress(previous_raw,fresh_raw)
                or not changed and not raw_equal(previous_raw,fresh_raw)):
            raise StatusConflict('variant_concurrent_status_change')
        return current,fresh_raw

    def handle(self, mapping, apply, settle):
        from podcast_sync import fingerprint
        key=state_key(mapping['id']); stored=self.b.load(key)
        proof=mapping['_variant_proof']; binding=self.binding(mapping,proof)
        self.mark(mapping,status='enrolled' if stored.get('enrolled') else 'joining')
        if stored.get('binding') and stored['binding'] != binding:
            raise StatusConflict('variant_identity_changed')
        states,raw=self.read(mapping,proof)
        if stored.get('pending'):
            return self.resume(mapping,key,stored,states,raw,proof,apply)
        decision = stored.get('reviewed_source')
        if decision:
            if decision['snapshot'] != fingerprint(raw):
                raise StatusConflict('variant_concurrent_status_change')
            source = 'user' if 'target' in decision else decision['peer']
        else:
            source=choose_status(states,stored.get('baseline'),raw,stored.get('raw'),
                                 primary_source=self.c.cfg.get('primary_source', 'auto'))
        if source is None:
            if apply:
                if not stored.get('enrolled'):
                    fresh=self.refreshed(mapping,binding)
                    if fresh is None:return 'waiting',states
                    states,raw=self.read_renewed(mapping,proof,fresh,raw)
                    proof=fresh
                self.baseline(key,binding,states,raw,proof)
                self.mark(mapping,status='enrolled')
            return 'unchanged',states
        if not apply:return 'proposed',states
        signature=fingerprint(raw); candidate=stored.get('candidate',{})
        if candidate.get('signature')!=signature:
            candidate=dict(signature=signature,since=self.b.clock())
            stored.update(binding=binding,candidate=candidate);self.b.save(key,stored)
        if not decision and self.b.clock()-candidate['since']<settle:
            self.mark(mapping,'variant_settle_wait');return 'waiting',states
        if mapping['jf']['id'] in self.c.jf.active_ids():
            self.mark(mapping,'variant_active_player');return 'waiting',states
        fresh=self.refreshed(mapping,binding)
        if fresh is None:return 'waiting',states
        states,raw=self.read_renewed(mapping,proof,fresh,raw)
        target=decision['target'] if decision and 'target' in decision else states[source]['status']
        plan=self.plan(mapping,fresh,states,raw,target)
        if not plan:raise StatusConflict('variant_pending_review')
        record=dict(schema=2,id='podcast-status-'+uuid.uuid4().hex,binding=binding,version=version_receipt(fresh),
            media_receipt=measurement_receipt(fresh),
            source=source,target_status=target,before=copy.deepcopy(raw),last_verified=copy.deepcopy(raw),
            plan=plan,plan_fingerprint=plan_fingerprint(plan),next=0,stage='prepared')
        stored.update(binding=binding,pending=record)
        self.persist(key,stored,record)
        return self.resume(mapping,key,stored,states,raw,fresh,apply,already_fresh=True)

    def plan(self,mapping,proof,states,raw,target):
        plan=[]; local=core_position(raw,mapping['duration']) if target=='in_progress' else 0
        a=raw['abs']; current=(a or {}).get('currentTime',0); finished=(a or {}).get('isFinished',False)
        if states['abs']['status']!=target:
            if finished and target!='finished':
                plan.append(dict(service='abs',kind='finished',value=False,expected_position=0,expected_finished=False))
                current=0;finished=False
            if target!='finished' and (current!=local or a is None):
                plan.append(dict(service='abs',kind='position',value=local,expected_position=local,expected_finished=False))
            if target=='finished':
                plan.append(dict(service='abs',kind='finished',value=True,expected_position=current,expected_finished=True))
        if states['jf']['status']!=target:
            j=raw['jf'];values=dict(PlaybackPositionTicks=j['PlaybackPositionTicks'] if target=='finished' else round(local*TICKS),
                Played=target=='finished',PlayCount=0 if target=='not_started' else max(j['PlayCount'],1))
            plan.append(dict(service='jf',kind='update',value=values))
        if any(states[name]['status']!=target for name in ('pinepods','gpodder')):
            latest=raw['pinepods'].get('updated_at')
            stamp=max(int(self.b.clock()),int(latest)+1 if latest is not None else 0)
            actions=self.pine.prepare_status_actions(proof['feed_url'],proof['enclosure_url'],target,
                proof['own_duration'],stamp,previous=raw['pinepods'],
                expected_episode_id=proof['native_episode_id'],expected_user_id=proof['native_user_id'])
            plan.extend(dict(service='pine',kind='status',value=action) for action in actions)
        return plan

    def validate_plan(self,record,mapping,proof):
        plan=record.get('plan');index=record.get('next');receipt=record.get('media_receipt')
        if (not isinstance(receipt,dict) or set(receipt)!={'source_fingerprint','proof_fingerprint','own_duration','validated_at','evidence_sha256'}
                or type(receipt.get('validated_at')) not in (int,float) or not math.isfinite(receipt['validated_at'])
                or receipt['validated_at']<0 or type(receipt.get('evidence_sha256'))is not str
                or len(receipt['evidence_sha256'])!=64 or any(c not in '0123456789abcdef' for c in receipt['evidence_sha256'])
                or receipt['source_fingerprint']!=proof['source_fingerprint'] or version_receipt(receipt)!=record.get('version')):
            raise StatusConflict('variant_pending_review')
        if (record.get('schema')!=2 or record.get('version')!=version_receipt(proof)
                or record.get('target_status') not in STATUSES
                or not isinstance(plan,list) or not plan or type(index)is not int or not 0<=index<=len(plan)):
            raise StatusConflict('variant_pending_review')
        if record.get('plan_fingerprint')!=plan_fingerprint(plan):
            raise StatusConflict('variant_pending_review')
        if record.get('stage') not in ('prepared','waiting','sending','verified'):
            raise StatusConflict('variant_pending_review')
        for step in plan:
            if not isinstance(step,dict):raise StatusConflict('variant_pending_review')
            if step.get('service')=='pine':
                if set(step)!={'service','kind','value'} or step.get('kind')!='status':
                    raise StatusConflict('variant_pending_review')
                a=step['value'];self.pine._status_payload(a)
                if (a.get('podcast')!=proof['feed_url'] or a.get('episode')!=proof['enclosure_url']
                        or a.get('_episode_id')!=proof['native_episode_id'] or a.get('_user_id')!=proof['native_user_id']
                        or a.get('_own_duration')!=proof['own_duration']):raise StatusConflict('variant_identity_changed')
            elif step.get('service')=='abs':
                if (set(step)!={'service','kind','value','expected_position','expected_finished'}
                        or step.get('kind') not in ('position','finished')
                        or type(step.get('expected_position')) not in (int,float)
                        or not math.isfinite(step['expected_position'])
                        or not 0<=step['expected_position']<=mapping['duration']
                        or type(step.get('expected_finished')) is not bool):raise StatusConflict('variant_pending_review')
                if step['kind']=='finished':
                    if (type(step['value'])is not bool or step['value'] is not step['expected_finished']
                            or not step['value'] and step['expected_position']!=0):raise StatusConflict('variant_pending_review')
                elif (type(step['value'])not in(int,float) or not math.isfinite(step['value'])
                        or step['value']!=step['expected_position'] or step['expected_finished'] is not False):
                    raise StatusConflict('variant_pending_review')
            elif step.get('service')=='jf':
                v=step.get('value',{})
                if (set(step)!={'service','kind','value'} or step.get('kind')!='update'
                        or set(v)!={'PlaybackPositionTicks','Played','PlayCount'}
                        or type(v['PlaybackPositionTicks'])is not int or not 0<=v['PlaybackPositionTicks']<=round(mapping['duration']*TICKS)
                        or type(v['Played'])is not bool or type(v['PlayCount'])is not int or v['PlayCount']<0):
                    raise StatusConflict('variant_pending_review')
            else:raise StatusConflict('variant_pending_review')

    def confirmed(self,step,after,before,proof):
        from podcast_sync import raw_equal
        target='pinepods' if step['service']=='pine' else step['service']
        if not raw_equal({k:v for k,v in after.items() if k!=target},{k:v for k,v in before.items() if k!=target}):return False
        if target=='pinepods':
            return self.pine.status_action_confirmed(after[target],step['value'],proof['own_duration'],previous=before[target])
        if target=='jf':return rows_equal([after['jf']],[dict(before['jf'],**step['value'])])
        a=after['abs']
        return (isinstance(a,dict) and a.get('isFinished') is step['expected_finished']
            and type(a.get('currentTime'))in(int,float) and abs(a['currentTime']-step['expected_position'])<=.001)

    def resume(self,mapping,key,stored,states,raw,proof,apply,already_fresh=False):
        from podcast_sync import guard,raw_equal,fingerprint
        record=stored['pending'];binding=self.binding(mapping,proof)
        if record.get('binding')!=binding:raise StatusConflict('variant_identity_changed')
        self.validate_plan(record,mapping,proof)
        index=record['next'];plan=record['plan'];previous=record.get('last_verified')
        if not isinstance(previous,dict):raise StatusConflict('variant_pending_review')
        def checkpoint(stage):
            record.update(stage=stage,next=index,last_verified=copy.deepcopy(raw))
            self.persist(key,stored,record)
        def advance():
            nonlocal index
            action=plan[index]
            if action['service']=='pine' and not action['value'].get('_native_action'):
                stamp=raw['pinepods'].get('updated_at')
                if type(stamp)is not int:raise StatusConflict('variant_pending_review')
                for later in plan[index+1:]:
                    if later['service']!='pine' or later['value'].get('_native_action'):continue
                    stamp=max(stamp+1,action_epoch(later['value']))
                    later['value']['timestamp']=dt.datetime.fromtimestamp(stamp,dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
            index+=1
        if record['stage']=='sending':
            if index>=len(plan) or not self.confirmed(plan[index],raw,previous,proof):
                raise StatusConflict('variant_pending_review')
            advance()
            if apply:checkpoint('verified')
        elif not raw_equal(raw,previous):raise StatusConflict('variant_concurrent_status_change')
        if not apply:return 'waiting',states
        if index<len(plan):
            upcoming=plan[index]
            if (upcoming['service']=='pine' and self.b.clock()<action_epoch(upcoming['value'])):
                checkpoint('waiting');self.mark(mapping,'variant_settle_wait');return 'waiting',states
            if mapping['jf']['id'] in self.c.jf.active_ids():
                self.mark(mapping,'variant_active_player');return 'waiting',states
        if not already_fresh:
            fresh=self.refreshed(mapping,binding,version=record['version'],receipt=record['media_receipt'])
            if fresh is None:return 'waiting',states
            proof=fresh
        while index<len(plan):
            manual = self.b.load('_pine_transition:' + mapping['id'])
            if (self.c.cfg.get('pinepods_enabled') is not True or
                    (self.c.cfg.get('pinepods_variant_status_only') is not True and not
                     (manual.get('approved_by') == 'user' and manual.get('binding') == binding))):
                raise StatusConflict('variant_pending_review')
            step=plan[index]
            if step['service']=='pine' and self.b.clock()<action_epoch(step['value']):
                checkpoint('waiting');self.mark(mapping,'variant_settle_wait');return 'waiting',states
            if mapping['jf']['id'] in self.c.jf.active_ids():
                self.mark(mapping,'variant_active_player');return 'waiting',states
            guard(self.b);self.c.verify_mapping(mapping)
            pinned=self.c.variant_proof(mapping,refresh=False,apply=False)
            if (not pinned.get('ready') or self.binding(mapping,pinned)!=binding
                    or version_receipt(pinned)!=record['version']):
                raise StatusConflict('variant_identity_changed')
            current,fresh_raw=self.read(mapping,proof)
            if not raw_equal(raw,fresh_raw):raise StatusConflict('variant_concurrent_status_change')
            if not 0<=self.b.clock()-proof.get('validated_at',-1)<=150:
                self.mark(mapping,'variant_proof_stale');return 'waiting',states
            checkpoint('sending')
            if step['service']=='abs':
                if step['kind']=='position':self.c.abs.position(mapping,step['value'])
                else:self.c.abs.finished(mapping,step['value'],int(self.b.clock()*1000) if step['value'] else None)
            elif step['service']=='jf':self.c.jf.update(mapping['jf']['id'],step['value'])
            else:
                receipt=self.pine.write_status_action(step['value'],expected=raw['pinepods'])
                if isinstance(receipt,dict) and receipt.get('timestamp')is not None:
                    step['value']['_server_timestamp']=action_epoch(receipt['timestamp']);checkpoint('sending')
            current,fresh_raw=self.read(mapping,proof)
            if not self.confirmed(step,fresh_raw,raw,proof):raise StatusConflict('variant_pending_review')
            states,raw=current,fresh_raw;advance();checkpoint('verified')
        if any(v['status']!=record['target_status'] for v in states.values()):raise StatusConflict('variant_pending_review')
        record.update(stage='complete',after=raw)
        final=dict(schema=2,binding=binding,version=record['version'],baseline=states,raw=raw,enrolled=True)
        # The status transfer intentionally changed the shared-file pair; its
        # next full-position cycle starts from this checked local state.
        core={k:v for k,v in states.items() if k in ('abs','jf')}
        core_raw={k:v for k,v in raw.items() if k in ('abs','jf')}
        core_saved=dict(mapping=fingerprint([mapping['fingerprint'],mapping['jf']['fingerprint']]),baseline=core,pine_raw=None)
        self.persist(key,final,record,complete=True,core=(mapping['id'],core_saved))
        self.b.event(key,'podcast_status_synced',dict(title=mapping['title'],source=record['source'],status=record['target_status']))
        self.mark(mapping,status='enrolled')
        return 'synced',states
