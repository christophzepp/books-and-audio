"""Exact source proofs for status-only synchronization of measured variants.

No progress or metadata writes. Only this helper's private proof-cache namespace
may be updated. A cached ready result permits read-only status observation;
admission and status mutations require refresh=True and a fresh receipt.
Returned URLs are private coordinator data and must never enter public logs.
"""
import copy
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

NAMESPACE='_pine_variant_proof:'
MAX_AGE=30*86400
FRESH_SECONDS=150
MAX_RECORD=128*1024
FULL_REASONS={'mpeg_audio_without_frame_count_tag','id3_exceeds_prefix','no_valid_frame_count_header'}


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def digest(value):return hashlib.sha256(encoded(value).encode()).hexdigest()
def finite(value):return type(value) in (int,float) and math.isfinite(value)
def refused(reason,**fields):return dict(ready=False,fresh=False,reason=reason,**fields)


def variant_probe_order(bridge,mappings):
    """Read-only fair ordering without claiming slots or approving evidence.

    Interrupted status journals are read back first, then existing enrolled
    status links ordered by their oldest proof attempt. Unchanged links only
    read their cached proof and consume no probe slot. New admissions cannot
    keep an existing status change waiting behind the initial inventory.
    Remaining candidates keep the never-attempted/oldest-attempt ordering.
    """
    from podcast_status_sync import pending
    def priority(mapping):
        try:
            if pending(bridge,mapping['id']):return (0,0)
            cache=bridge.load(NAMESPACE+mapping['id'])
            attempted=cache.get('last_attempt_at')
            status=bridge.load('_podcast_status:'+mapping['id'])
            if status.get('enrolled') is True:
                return (1,attempted if finite(attempted) and attempted>=0 else 0)
            metadata=bridge.load('_pine_metadata:'+mapping['id'])
            evidence=metadata.get('evidence') or {}
            measured=evidence.get('duration_seconds')
            candidate=(cache.get('stage')=='verified' or evidence.get('known') is True
                and finite(measured) and finite(mapping.get('duration'))
                and abs(measured-mapping['duration'])>2)
            if not candidate:return (4,0)
            if not finite(attempted) or attempted<0:return (2,0)
            return (3,attempted)
        except (AttributeError,KeyError,TypeError,ValueError):return (4,0)
    return sorted(mappings,key=priority)


def evidence_epoch(evidence):
    try:
        value=dt.datetime.fromisoformat(evidence['fetch']['fetched_at'])
        if value.tzinfo is None:raise ValueError()
        epoch=value.timestamp()
        if not finite(epoch):raise ValueError()
        return epoch
    except (KeyError,TypeError,ValueError,OverflowError):return None


def validated_audio(evidence,url,clock):
    from pine_media_probe import valid_result
    if not isinstance(evidence,dict) or evidence.get('known') is not True or not valid_result(evidence,url):return False
    duration=evidence.get('duration_seconds');epoch=evidence_epoch(evidence)
    if evidence.get('header',{}).get('source')=='validated_full_mp3_cache':
        # A fresh validator receipt must not make an indefinitely old full-file
        # measurement young again. Keep the original measurement age bounded.
        full_epoch=evidence_epoch(evidence.get('full_evidence',{}))
        if full_epoch is None or not 0<=clock-full_epoch<=MAX_AGE:return False
    return finite(duration) and 1<=duration<=172800 and epoch is not None and 0<=clock-epoch<=MAX_AGE


def validated_variant(evidence,url,abs_duration,clock):
    return (validated_audio(evidence,url,clock) and finite(abs_duration) and abs_duration>0
            and abs(evidence['duration_seconds']-abs_duration)>2)


def native_binding(native,mapping,account):
    """Identity only: RSS duration and user progress cannot define an audio file."""
    from pine_identity_alias import account_identity
    account_identity(account)
    if not isinstance(native,dict) or native.get('account')!=account:raise ValueError('variant_account_changed')
    metadata=native.get('metadata')
    if (not isinstance(metadata,dict) or native.get('downloaded') is not False
            or metadata.get('is_downloaded') is not False or metadata.get('is_youtube') is not False):
        raise ValueError('variant_native_source_unverified')
    for top,key in (('episode_id','episodeid'),('podcast_id','podcastid')):
        if type(native.get(top)) is not int or native[top]<=0 or metadata.get(key)!=native[top]:
            raise ValueError('variant_native_identity_changed')
    if (type(native.get('user_id')) is not int or native['user_id']<=1
            or native['user_id']!=account.get('native_user_id')
            or metadata.get('feedurl')!=mapping['feed_url'] or metadata.get('episodeurl')!=mapping['enclosure_url']):
        raise ValueError('variant_native_identity_changed')
    return dict(account=copy.deepcopy(account),abs_guid=mapping.get('abs_guid'),native_episode_id=native['episode_id'],native_user_id=native['user_id'],
                native_podcast_id=native['podcast_id'],feed_url=mapping['feed_url'],enclosure_url=mapping['enclosure_url'])


class VariantProof:
    def __init__(self,bridge,probe=None):
        self.b=bridge;self.probe=probe;self.remaining=2;self.deadline=None;self.full_used=False

    def _source(self,mapping):
        # Preserve the metadata manager's evidence shape for existing proofs,
        # and additionally verify the current GUID, pinned in native binding.
        from podcast_sync import build_mapping,settings
        cfg=settings(self.b)
        if mapping.get('_pine_unavailable'):raise ValueError('variant_source_unavailable')
        item=self.b.abs.item(mapping['podcast_id']);episodes=item.get('media',{}).get('episodes')
        if not isinstance(episodes,list):raise ValueError('variant_source_incomplete')
        matches=[row for row in episodes if isinstance(row,dict) and row.get('id')==mapping['episode_id']]
        if len(matches)!=1:raise ValueError('variant_source_changed')
        fresh=build_mapping(item,matches[0],cfg)
        keys=('id','podcast_id','episode_id','abs_library','path','worker_path','size','duration','mtime',
              'fingerprint','feed_url','enclosure_url')
        if (fresh is None or any(fresh.get(key)!=mapping.get(key) for key in keys)
                or fresh.get('abs_guid')!=mapping.get('abs_guid')):raise ValueError('variant_source_changed')
        worker=Path(mapping['worker_path']);root=Path(cfg['worker_root'])
        if (not worker.is_absolute() or not root.is_absolute() or worker.resolve()!=worker
                or root.resolve()!=root or not worker.is_relative_to(root)):raise ValueError('variant_source_path_changed')
        info=worker.stat(follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_size!=mapping['size']
                or abs(info.st_mtime_ns/1_000_000-mapping['mtime'])>2):raise ValueError('variant_source_file_changed')
        return dict(mapping={key:mapping[key] for key in keys},
                    file=dict(device=info.st_dev,inode=info.st_ino,size=info.st_size,
                              mtime_ns=info.st_mtime_ns,ctime_ns=info.st_ctime_ns),jellyfin=mapping.get('jf'))

    def _pending(self,mapping):
        keys=(mapping['id'],'_pine_metadata:'+mapping['id'])
        # Existing progress journals are owned by the coordinator, which may
        # refresh proof while completing that journal. Metadata intents are a
        # different writer and always require explicit reconciliation first.
        metadata=self.b.load(keys[1])
        return (metadata.get('stage')=='intent' or self.b.db.execute(
            "SELECT 1 FROM operations WHERE book=? AND status='pending' LIMIT 1",(keys[1],)).fetchone() is not None)

    def _progress_pending(self,mapping):
        keys=(mapping['id'],'_podcast_status:'+mapping['id'],'_pine_metadata:'+mapping['id'])
        if any(self.b.load(key).get('pending') for key in keys) or self._pending(mapping):return True
        return self.b.db.execute("SELECT 1 FROM operations WHERE book IN (?,?,?) AND status='pending' LIMIT 1",keys).fetchone() is not None

    def _save(self,key,record,apply,*,previous_proof=None):
        if not apply:return
        raw=encoded(record)
        if len(raw.encode())>MAX_RECORD:raise ValueError('variant_cache_too_large')
        if self.b.db.execute('PRAGMA synchronous').fetchone()[0] not in (2,3):raise ValueError('variant_cache_not_durable')
        if previous_proof is None:self.b.save(key,record)
        else:
            # The old complete evidence remains privately auditable even when
            # the same publisher URL now serves a different measured cut.
            # State and audit commit together; progress journals are untouched.
            # Event logs must not copy private enclosure/feed URLs from the
            # binding. Its fingerprint plus full sanitized measurement are
            # sufficient to associate and audit the previous version.
            previous_measurement={name:copy.deepcopy(previous_proof[name]) for name in
                ('source_fingerprint','proof_fingerprint','own_duration','validated_at','evidence')}
            detail=encoded(dict(previous_measurement=previous_measurement,
                next_proof_fingerprint=record['proof_fingerprint'],next_own_duration=record['own_duration'],
                next_evidence_sha256=record['evidence']['evidence_sha256'],renewed_at=self.b.clock()))
            if len(detail.encode())>MAX_RECORD+2048:raise ValueError('variant_audit_too_large')
            with self.b.db:
                self.b.db.execute('INSERT OR REPLACE INTO state(id,value) VALUES(?,?)',(key,raw))
                self.b.db.execute('INSERT INTO events(at,book,kind,detail) VALUES(?,?,?,?)',
                    (dt.datetime.fromtimestamp(self.b.clock(),dt.timezone.utc).isoformat(),key,
                     'pine_variant_measurement_renewed',detail))
        for path in (Path(self.b.root)/'state.sqlite',Path(self.b.root)):
            fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            try:os.fsync(fd)
            finally:os.close(fd)

    def _seed(self,mapping,source,clock):
        record=self.b.load('_pine_metadata:'+mapping['id'])
        evidence=record.get('candidate_evidence',record.get('evidence'))
        evidence_sha=record.get('candidate_evidence_sha256',record.get('evidence_sha256'))
        if (record.get('schema')!=1 or record.get('stage')!='deferred' or record.get('source')!=source
                or not isinstance(evidence,dict) or evidence_sha!=digest(evidence)
                or evidence_epoch(evidence) is None or evidence_epoch(evidence)>clock
                or not validated_variant(evidence,mapping['enclosure_url'],mapping['duration'],evidence_epoch(evidence))):
            return None
        return copy.deepcopy(evidence)

    def _cache(self,record,source,clock):
        if not record:return None
        if (not isinstance(record,dict) or record.get('schema')!=1 or record.get('stage') not in ('verified','deferred')
                or len(encoded(record).encode())>MAX_RECORD):raise ValueError('variant_cache_requires_review')
        if record.get('stage')=='deferred':return None
        binding=record.get('binding');own=record.get('own_duration')
        if (not isinstance(binding,dict) or binding.get('source')!=source
                or record.get('source_fingerprint')!=digest(binding)
                or not finite(own) or not 1<=own<=172800
                or record.get('proof_fingerprint')!=digest({'binding':binding,'own_duration':own})
                or not finite(record.get('validated_at')) or clock<record['validated_at']
                or record['validated_at']!=evidence_epoch(record.get('evidence',{}))
                or not validated_audio(record.get('evidence'),source['mapping']['enclosure_url'],record['validated_at'])
                or abs(record['evidence']['duration_seconds']-own)>2):
            raise ValueError('variant_cached_source_or_evidence_changed')
        origin=record.get('origin',dict(evidence=record['evidence'],validated_at=record['validated_at'],
                                     source_fingerprint=record['source_fingerprint']))
        if (not isinstance(origin,dict) or set(origin)!={'evidence','validated_at','source_fingerprint'}
                or origin['source_fingerprint']!=record['source_fingerprint']
                or not finite(origin['validated_at']) or not 0<=origin['validated_at']<=clock
                or not validated_variant(origin['evidence'],source['mapping']['enclosure_url'],
                                         source['mapping']['duration'],origin['validated_at'])):
            raise ValueError('variant_original_evidence_changed')
        return record

    def candidate(self,mapping):
        """Historical exact-source classification, never admission/readiness.

        A publisher cut can later have the same length as the local file. That
        coincidence cannot return an already proven variant to position sync.
        Native identity and fresh current endpoints remain inspect's work.
        """
        try:
            if self.b.load('_pine_alias:'+mapping['id']) or mapping.get('_pine_alias_fingerprint'):return False
            source=self._source(mapping);clock=self.b.clock()
            cached=self._cache(self.b.load(NAMESPACE+mapping['id']),source,clock)
            return cached is not None or self._seed(mapping,source,clock) is not None
        except Exception as exc:
            from podcast_sync import service_unavailable
            if service_unavailable(exc):raise
            return False

    def _measure(self,url,previous):
        if self.remaining<=0:return {'known':False,'reason':'variant_cycle_budget'}
        if self.deadline is None:self.deadline=time.monotonic()+45
        remaining=self.deadline-time.monotonic()
        if remaining<1:return {'known':False,'reason':'variant_cycle_budget'}
        self.remaining-=1;probe=self.probe;kwargs={}
        if probe is None:
            from pine_media_probe import measure,private_temp_root
            root=Path(self.b.root)
            if not root.is_absolute() or root.resolve(strict=True)!=root:raise ValueError('variant_probe_storage_unavailable')
            directory=root/'pine-variant-probes';directory.mkdir(mode=0o700,exist_ok=True)
            kwargs['temp_root']=private_temp_root(directory);probe=measure
        proof=previous
        if (proof.get('header') or {}).get('source')=='validated_full_mp3_cache':proof=proof.get('full_evidence')
        if not isinstance(proof,dict) or (proof.get('header') or {}).get('source')!='full_mp3_packets':proof=None
        result=probe(url,timeout_seconds=min(10,remaining),previous_evidence=proof,**kwargs)
        if isinstance(result,dict) and result.get('known') is False and result.get('reason') in FULL_REASONS:
            remaining=self.deadline-time.monotonic()
            if self.full_used or remaining<1:return {'known':False,'reason':'variant_cycle_budget'}
            self.full_used=True
            result=probe(url,timeout_seconds=min(45,remaining),full_probe=True,**kwargs)
        return result

    def _response(self,record,*,fresh=False):
        binding=record['binding'];native=binding['native']
        return dict(ready=True,reason='variant_proof_refreshed' if fresh else 'variant_cached_identity',fresh=fresh,
            validated_at=record['validated_at'],own_duration=record['own_duration'],source_fingerprint=record['source_fingerprint'],
            proof_fingerprint=record['proof_fingerprint'],evidence_sha256=record['evidence']['evidence_sha256'],
            feed_url=native['feed_url'],enclosure_url=native['enclosure_url'],native_episode_id=native['native_episode_id'],
            native_user_id=native['native_user_id'])

    def _failed_attempt(self,key,cached,source_fingerprint,reason,apply):
        # Retain a known identity for read-only observation; never replace its
        # duration, source or progress history with failed fresh evidence.
        failed=copy.deepcopy(cached) if cached else {'schema':1,'stage':'deferred','source_fingerprint':source_fingerprint}
        failed.update(last_attempt_at=self.b.clock(),next_attempt_at=self.b.clock()+900,last_failure=reason)
        self._save(key,failed,apply)
        return refused(reason)

    def inspect(self,mapping,*,native=None,refresh=False,apply=False):
        if type(refresh) is not bool or type(apply) is not bool:return refused('variant_invalid_mode')
        key=NAMESPACE+mapping['id'];clock=self.b.clock();attempted=False;cached=None;source_fingerprint=None
        try:
            if self.b.load('_pine_alias:'+mapping['id']) or mapping.get('_pine_alias_fingerprint'):
                return refused('variant_alias_requires_review')
            if self._pending(mapping):return refused('variant_metadata_intent_pending')
            source=self._source(mapping);stored=self.b.load(key);cached=self._cache(stored,source,clock)
            evidence=cached['evidence'] if cached else self._seed(mapping,source,clock)
            if evidence is None:return refused('variant_measured_evidence_required')
            current=self.b.pinepods.inspect_episode_metadata(mapping['feed_url'],mapping['enclosure_url'],mapping['duration'])
            account=dict(self.b.pinepods.identity);identity=native_binding(current,mapping,account)
            old_account=self.b.load('_podcast_accounts').get('pinepods')
            if old_account is not None and old_account!=account:return refused('variant_account_changed')
            if native is not None and native_binding(native,mapping,account)!=identity:return refused('variant_native_identity_changed')
            binding={'source':source,'native':identity};source_fingerprint=digest(binding)
            if cached and cached['binding']!=binding:return refused('variant_native_identity_changed')
            if not refresh:
                if cached:
                    if not validated_audio(cached['evidence'],mapping['enclosure_url'],clock):
                        return refused('variant_cached_evidence_expired')
                    return self._response(cached)
                return refused('fresh_variant_proof_required',own_duration=evidence['duration_seconds'],source_fingerprint=source_fingerprint)
            if cached is None and self._progress_pending(mapping):return refused('variant_pending_proof_pin')
            # Retry throttling only applies to this same exact source/account.
            if (stored.get('source_fingerprint')==source_fingerprint and finite(stored.get('next_attempt_at'))
                    and stored['next_attempt_at']>clock):return refused('variant_refresh_backoff')
            attempted=True
            measured=self._measure(mapping['enclosure_url'],evidence)
            if not validated_audio(measured,mapping['enclosure_url'],self.b.clock()):
                if isinstance(measured,dict) and measured.get('reason')=='variant_cycle_budget':return refused('variant_cycle_budget')
                return self._failed_attempt(key,cached,source_fingerprint,'variant_fresh_evidence_unavailable',apply)
            measured_at=evidence_epoch(measured)
            if measured_at is None or not 0<=self.b.clock()-measured_at<=60:
                return self._failed_attempt(key,cached,source_fingerprint,'variant_probe_receipt_stale',apply)
            # A saved seed only identifies a candidate. Admission uses the
            # actual fresh measurement, including dynamic publisher cuts.
            renewal=bool(cached and abs(measured['duration_seconds']-cached['own_duration'])>2)
            own=cached['own_duration'] if cached and not renewal else measured['duration_seconds']
            if renewal and self._progress_pending(mapping):
                return self._failed_attempt(key,cached,source_fingerprint,'variant_pending_proof_pin',apply)
            if self._source(mapping)!=source or self._pending(mapping):
                return self._failed_attempt(key,cached,source_fingerprint,'variant_source_changed_during_probe',apply)
            after=self.b.pinepods.inspect_episode_metadata(mapping['feed_url'],mapping['enclosure_url'],mapping['duration'])
            if native_binding(after,mapping,dict(self.b.pinepods.identity))!=identity:
                return self._failed_attempt(key,cached,source_fingerprint,'variant_native_changed_during_probe',apply)
            if self._source(mapping)!=source:
                return self._failed_attempt(key,cached,source_fingerprint,'variant_source_changed_during_probe',apply)
            if not 0<=self.b.clock()-measured_at<=FRESH_SECONDS:
                return self._failed_attempt(key,cached,source_fingerprint,'variant_probe_receipt_stale',apply)
            if (renewal or cached is None) and self._progress_pending(mapping):
                return self._failed_attempt(key,cached,source_fingerprint,'variant_pending_proof_pin',apply)
            origin=copy.deepcopy(cached.get('origin')) if cached and 'origin' in cached else dict(
                evidence=copy.deepcopy(cached['evidence'] if cached else evidence),
                validated_at=cached['validated_at'] if cached else evidence_epoch(evidence),source_fingerprint=source_fingerprint)
            record={'schema':1,'stage':'verified','binding':binding,'source_fingerprint':source_fingerprint,
                    'proof_fingerprint':digest({'binding':binding,'own_duration':own}),'own_duration':own,
                    'validated_at':measured_at,'evidence':measured,'last_attempt_at':self.b.clock(),'origin':origin}
            self._save(key,record,apply,previous_proof=cached if renewal else None);return self._response(record,fresh=True)
        except Exception as exc:
            # Preserve the coordinator's existing bounded service-outage stop
            # instead of trying the same unavailable service for every title.
            from podcast_sync import service_unavailable
            if service_unavailable(exc):raise
            if attempted and source_fingerprint is not None:
                try:return self._failed_attempt(key,cached,source_fingerprint,'variant_proof_requires_review',apply)
                except Exception:pass
            return refused('variant_proof_requires_review')
