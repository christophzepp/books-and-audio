"""Episode-scoped podcast progress, independent of either book pairing provider.

Only downloaded ABS episodes with an exact shared-file Jellyfin binding qualify.
No media, feed subscriptions, playback sessions or BookOrbit data are mutated.
API reference: ABS v2.37.1 MeController and MediaProgress; Jellyfin UserItems API.
"""
from __future__ import annotations

import copy
from collections import Counter
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import uuid

from jellyfin_audio import TICKS, choose, rows_equal, same
from pinepods_client import action_epoch, EpisodeUnavailable

NAMESPACE = '_podcast:'
PINE_REASONS = {
    'feed_missing': 'PinePods has not subscribed to the exact podcast feed.',
    'feed_ambiguous': 'The exact podcast feed is ambiguous in PinePods.',
    'episode_missing': 'The exact episode is not available in PinePods.',
    'episode_ambiguous': 'The exact episode is ambiguous in PinePods.',
    'episode_identity_unresolved': 'The PinePods episode identity is unresolved: no exact URL matches and an inventory enclosure is empty.',
    'duration_mismatch': 'The PinePods episode duration differs from the shared audio file.',
    'media_identity_mismatch': 'The PinePods media identity differs from the shared episode.',
    'source_identity_missing': 'Audiobookshelf lacks an exact feed or episode URL for PinePods.',
    'source_identity_ambiguous': 'Several Audiobookshelf files use the same feed and episode URL.',
    'core_transfer_pending': 'The existing ABS/Jellyfin transfer must finish before PinePods can join.',
    'jellyfin_not_indexed': 'The shared episode must be indexed in Jellyfin before PinePods can join.',
    'metadata_repair_pending': 'A PinePods duration correction requires verified readback before progress synchronization.',
}


class JellyfinProbePending(ValueError):
    """A same-path, same-size item has no probed duration yet.

    Only the coordinator may defer this condition, and only before any saved
    state or operation exists for the episode. Other invalid metadata is fatal.
    """


def has_pine_history(value):
    """Enrollment is durable, including a journal whose baseline was not saved."""
    if not isinstance(value, dict):
        return False
    if (value.get('pine_enrolled') is True or value.get('pine_raw') is not None
            or 'pinepods' in value or 'gpodder' in value or value.get('pine_plan')):
        return True
    return any(has_pine_history(value.get(key)) for key in ('baseline', 'pending', 'before', 'after', 'last_verified'))


def pine_history_ids(bridge):
    ids = set()
    for key, value in bridge.db.execute("SELECT id,value FROM state WHERE id LIKE '_podcast:%'"):
        if key.startswith(NAMESPACE) and has_pine_history(json.loads(value)):
            ids.add(key)
    for key, value in bridge.db.execute("SELECT book,value FROM operations WHERE book LIKE '_podcast:%'"):
        if key.startswith(NAMESPACE) and has_pine_history(json.loads(value)):
            ids.add(key)
    return ids


def status_history_ids(bridge):
    from podcast_status_sync import history_ids
    return history_ids(bridge)


def same_pine_account(previous, current, bridge):
    if previous == current:
        return True
    # A connection can be saved before its personal native ID is first probed.
    # Enrich that one field only before ANY episode has ever used PinePods.
    return (isinstance(previous, dict) and isinstance(current, dict)
        and previous.get('native_user_id') is None
        and type(current.get('native_user_id')) is int and current['native_user_id'] > 1
        and {k:v for k,v in previous.items() if k != 'native_user_id'} == {k:v for k,v in current.items() if k != 'native_user_id'}
        and not pine_history_ids(bridge) and not status_history_ids(bridge))


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def number(value, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError('Invalid podcast number')
    return value


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 200 or any(c in value for c in ('/', '\\', '\x00', ':')):
        raise ValueError('Invalid podcast identifier')
    return value


def path(value):
    if (not isinstance(value, str) or not value.startswith('/') or '\\' in value
            or any(ord(c) < 32 for c in value) or '..' in value.split('/')
            or str(PurePosixPath(value)) != value or value == '/'):
        raise ValueError('Invalid podcast media path')
    return PurePosixPath(value)


def title(value):
    if not isinstance(value, str) or not value.strip():
        return 'Podcastfolge'
    return ''.join(c if c.isprintable() else ' ' for c in value)[:500]


def settings(bridge):
    cfg = bridge.config.get('podcasts', {})
    if not isinstance(cfg, dict) or type(cfg.get('enabled', False)) is not bool:
        raise ValueError('Invalid podcast configuration')
    if not cfg.get('enabled'):
        return cfg
    for name in ('abs_library_id', 'jellyfin_library_id'):
        identifier(cfg.get(name))
    for name in ('abs_root', 'jellyfin_root', 'worker_root'):
        path(cfg.get(name))
    if not isinstance(cfg.get('expected_abs_username'), str) or not cfg['expected_abs_username']:
        raise ValueError('Podcast Audiobookshelf account must be pinned')
    if cfg.get('bookorbit_enabled', False) is not False:
        raise ValueError('BookOrbit podcasts are prepared only; no verified podcast API is enabled')
    if type(cfg.get('pinepods_enabled', False)) is not bool:
        raise ValueError('Invalid PinePods podcast switch')
    if type(cfg.get('pinepods_optional_enrollment', False)) is not bool:
        raise ValueError('Invalid optional PinePods enrollment switch')
    if type(cfg.get('pinepods_variant_status_only', False)) is not bool:
        raise ValueError('Invalid PinePods variant status switch')
    return cfg


def guard(bridge):
    """The host watcher proves these three mounts without requiring BookOrbit."""
    cfg = settings(bridge)
    if not cfg.get('enabled'):
        raise RuntimeError('Podcast synchronization is disabled')
    if getattr(bridge, 'external_write_guard', None):
        bridge.external_write_guard()
    gate = json.loads((bridge.root / 'deployment.json').read_text())
    from watch_deployment import validate_podcast_aliases
    validate_podcast_aliases(bridge.config,gate,now=bridge.clock())
    if bridge.abs.version() != bridge.config['abs_version']:
        raise RuntimeError('Audiobookshelf version changed')
    me = bridge.abs._request('GET', '/api/me', '/api/me')
    if (me.get('username') != cfg['expected_abs_username']
            or (cfg.get('expected_abs_user_id') and me.get('id') != cfg['expected_abs_user_id'])):
        raise RuntimeError('Audiobookshelf podcast account changed')
    if bridge.jellyfin is None:
        raise RuntimeError('Jellyfin podcast connection is unavailable')
    user=bridge.jellyfin.guard()
    policy=user.get('Policy') if isinstance(user,dict) else None
    if not isinstance(policy,dict):
        raise RuntimeError('Jellyfin personal podcast library permissions are unavailable')
    if policy.get('EnableAllFolders') is not True:
        enabled=policy.get('EnabledFolders')
        # /Items can list an indexed file even when the user cannot access its
        # direct item or UserData endpoint. Check personal library visibility
        # before creating a transfer journal, not after a failed POST.
        selected=cfg['jellyfin_library_id'].replace('-','').lower()
        if (policy.get('EnableAllFolders') is not False or not isinstance(enabled,list)
                or any(not isinstance(value,str) for value in enabled)
                or selected not in {value.replace('-','').lower() for value in enabled}):
            raise RuntimeError('Jellyfin personal user cannot access the selected podcast library; grant this library in Jellyfin first')
    if cfg.get('pinepods_enabled'):
        if gate.get('pinepods_ok') is not True:
            raise RuntimeError('PinePods deployment/version verification is unavailable')
        if getattr(bridge, 'pinepods', None) is None:
            raise RuntimeError('PinePods connection is unavailable')
        evidence=bridge.pinepods.probe()
        if evidence.get('device_exists') is not True:
            raise RuntimeError('PinePods sync device must be created before activation')
        if evidence.get('progress_sync_supported') is not True:
            raise RuntimeError('PinePods native progress semantics are not verified for this server')
        previous=bridge.load('_podcast_accounts').get('pinepods')
        if previous is not None and not same_pine_account(previous, bridge.pinepods.identity, bridge):
            raise RuntimeError('PinePods personal account differs from the saved podcast identity')
    return cfg


class EpisodeABS:
    """Wrap the verified ABS client while preserving episode-scoped API paths."""
    def __init__(self, client): self.client = client

    def _path(self, podcast, episode):
        return '/api/me/progress/' + self.client._id(podcast) + '/' + self.client._id(episode)

    def read(self, mapping):
        return self.client._request('GET', self._path(mapping['podcast_id'], mapping['episode_id']),
                                    '/api/me/progress/{podcast}/{episode}', missing_ok=True)

    def position(self, mapping, position):
        duration = mapping['duration']
        number(position)
        if position > duration: raise ValueError('Podcast position exceeds duration')
        return self.client._request('PATCH', self._path(mapping['podcast_id'], mapping['episode_id']),
            '/api/me/progress/{podcast}/{episode}', expect_json=False,
            payload={'currentTime': position, 'duration': duration, 'progress': position / duration,
                     'markAsFinishedTimeRemaining': 0})

    def finished(self, mapping, finished, finished_at=None):
        if type(finished) is not bool: raise ValueError('Invalid podcast completion')
        payload = {'isFinished': finished, 'markAsFinishedTimeRemaining': 0}
        if finished_at is not None:
            if not finished or type(finished_at) is not int or finished_at <= 0:
                raise ValueError('Invalid podcast completion timestamp')
            payload['finishedAt'] = finished_at
        return self.client._request('PATCH', self._path(mapping['podcast_id'], mapping['episode_id']),
            '/api/me/progress/{podcast}/{episode}', expect_json=False, payload=payload)


def build_mapping(item, episode, cfg):
    if item.get('mediaType') != 'podcast' or item.get('libraryId') != cfg['abs_library_id']:
        raise ValueError('Item is outside the selected ABS podcast library')
    podcast, ep = identifier(item.get('id')), identifier(episode.get('id'))
    if episode.get('libraryItemId') not in (None, podcast):
        raise ValueError('Episode belongs to another podcast')
    audio = episode.get('audioFile')
    if not isinstance(audio, dict):
        return None  # Remote/unavailable episodes are not files in Jellyfin.
    if audio.get('error') or audio.get('invalid') or item.get('isMissing') or item.get('isInvalid'):
        raise ValueError('Podcast media is unavailable or invalid')
    metadata = audio.get('metadata', {})
    original = path(metadata.get('path'))
    parent, abs_root = path(item.get('path')), path(cfg['abs_root'])
    if not original.is_relative_to(parent) or original == parent or not parent.is_relative_to(abs_root):
        raise ValueError('Podcast episode lies outside its verified folder')
    relative = original.relative_to(abs_root)
    if not relative.parts: raise ValueError('Podcast has no relative media file')
    size = metadata.get('size')
    if type(size) is not int or size <= 0: raise ValueError('Podcast media size is invalid')
    duration = number(audio.get('duration'), positive=True)
    if episode.get('duration') is not None and abs(number(episode['duration']) - duration) > .001:
        raise ValueError('Podcast duration metadata disagrees')
    if episode.get('size') is not None and episode['size'] != size:
        raise ValueError('Podcast size metadata disagrees')
    mtime = number(metadata.get('mtimeMs'))
    feed = item.get('media', {}).get('metadata', {}).get('feedUrl')
    enclosure = (episode.get('enclosure') or {}).get('url')
    identity = dict(podcast_id=podcast, episode_id=ep, abs_library=cfg['abs_library_id'],
                    path=str(original), size=size, duration=duration, mtime=mtime)
    return dict(identity, id=NAMESPACE + podcast + ':' + ep, title=title(episode.get('title')),
                show=title(item.get('media', {}).get('metadata', {}).get('title')),
                abs_guid=episode.get('guid'),
                jf_path=str(path(cfg['jellyfin_root']) / relative),
                worker_path=str(path(cfg['worker_root']) / relative),
                feed_url=feed, enclosure_url=enclosure, fingerprint=fingerprint(identity))


def bind(mapping, inventory):
    candidates = [row for row in inventory if row.get('Path') == mapping['jf_path']]
    if not candidates: return None
    if len(candidates) != 1: raise ValueError('Duplicate Jellyfin podcast file')
    item = candidates[0]
    sources = [s for s in item.get('MediaSources', []) if s.get('Path') == mapping['jf_path']]
    if (item.get('Type') != 'AudioBook' or len(sources) != 1
            or sources[0].get('Size') != mapping['size']):
        raise ValueError('Jellyfin podcast file differs from Audiobookshelf')
    ident = identifier(item.get('Id'))
    ticks=item.get('RunTimeTicks')
    if ticks is None or type(ticks) in (int,float) and ticks == 0:
        raise JellyfinProbePending('Jellyfin has not finished indexing this episode duration')
    if abs(number(ticks, positive=True) / TICKS - mapping['duration']) > 1:
        raise ValueError('Jellyfin podcast file differs from Audiobookshelf')
    return dict(id=ident, path=mapping['jf_path'], duration=mapping['duration'], size=mapping['size'],
                fingerprint=fingerprint([ident, mapping['jf_path'], mapping['duration'], mapping['size']]))


def abs_state(raw, mapping):
    if raw is None:
        return dict(position=0, status='not_started', missing=True, fingerprint=mapping['fingerprint'])
    if (raw.get('libraryItemId') != mapping['podcast_id'] or raw.get('episodeId') != mapping['episode_id']
            or raw.get('mediaItemType', 'podcastEpisode') != 'podcastEpisode'):
        raise ValueError('Audiobookshelf returned another podcast progress record')
    pos = number(raw.get('currentTime'))
    if type(raw.get('isFinished')) is not bool or pos > mapping['duration'] + 1:
        raise ValueError('Invalid ABS podcast progress')
    # A manually marked completion may carry zero seconds. Completion is the
    # canonical endpoint; do not invent a partial position from that zero.
    status = 'finished' if raw['isFinished'] else ('in_progress' if pos > 0 else 'not_started')
    return dict(position=mapping['duration'] if raw['isFinished'] else min(pos, mapping['duration']),
                status=status, missing=False, fingerprint=mapping['fingerprint'])


def jf_raw(item, target):
    if (item.get('Id') != target['id'] or item.get('Path') != target['path']
            or item.get('Type') != 'AudioBook' or
            abs(number(item.get('RunTimeTicks'), positive=True) / TICKS - target['duration']) > 1):
        raise ValueError('Jellyfin podcast identity changed')
    sources = [s for s in item.get('MediaSources', []) if s.get('Path') == target['path']]
    if len(sources) != 1 or sources[0].get('Size') != target['size']:
        raise ValueError('Jellyfin podcast source changed')
    data = item.get('UserData')
    if not isinstance(data, dict): raise ValueError('Jellyfin podcast user data missing')
    ticks, played, count = data.get('PlaybackPositionTicks'), data.get('Played'), data.get('PlayCount')
    if type(ticks) is not int or ticks < 0 or ticks / TICKS > target['duration'] + 1 or type(played) is not bool:
        raise ValueError('Invalid Jellyfin podcast position')
    if type(count) is not int or count < 0: raise ValueError('Invalid Jellyfin podcast play count')
    return {key: data.get(key) for key in ('PlaybackPositionTicks', 'Played', 'LastPlayedDate', 'PlayCount')}


def jf_state(raw, target):
    position = target['duration'] if raw['Played'] else min(raw['PlaybackPositionTicks'] / TICKS, target['duration'])
    status = 'finished' if raw['Played'] else ('in_progress' if position or raw['PlayCount'] else 'not_started')
    return dict(position=position, status=status, fingerprint=target['fingerprint'])


def raw_equal(left, right):
    if set(left) != set(right): return False
    return all(rows_equal([left[k]], [right[k]]) if k == 'jf' else left[k] == right[k] for k in left)


def service_unavailable(error):
    """Avoid hundreds of sequential timeouts after a service drops mid-cycle."""
    message=str(error)
    if type(error).__name__=='RequestError':
        status=getattr(error,'status',None)
        return (message.endswith('transport failure') or status in (401,403,429)
                or isinstance(status,int) and status>=500)
    if isinstance(error,RuntimeError):
        if message=='Jellyfin transport failure': return True
        if message.startswith('Jellyfin ') and ': HTTP ' in message:
            status=message.rsplit(': HTTP ',1)[-1]
            return status.isdigit() and (int(status) in (401,403,429) or int(status)>=500)
    return False


def choose_source(states, baseline, raw=None, previous_raw=None):
    """Choose changes against a baseline, including explicit zero-position resets.

    An absent record is never a reset. A Go gpodder play updates native seconds
    but not the completion flag; recognize precisely that documented echo only
    after we already observed the previous native and action-log states.
    """
    if baseline:
        for name, old in baseline.items():
            if name in states and states[name].get('missing') and old['status'] != 'not_started':
                raise ValueError('Previously active podcast progress disappeared')
            if name in states and states[name].get('fingerprint') != old.get('fingerprint'):
                raise ValueError('Podcast source identity changed')
        if set(baseline) != set(states):
            common = set(baseline) & set(states)
            if not common or any(not same(states[k], baseline[k]) for k in common):
                raise ValueError('Podcast participants changed while progress was changing')
            baseline = None
    if baseline and raw and previous_raw and 'gpodder' in states:
        old, current = previous_raw, raw.get('pinepods', {})
        if (current.get('gpodder_revision') != old.get('gpodder_revision')
                and current.get('action') == 'play'
                and current.get('native_completed') == old.get('native_completed')
                and current.get('native_position') is not None
                and current.get('gpodder_raw_position') is not None
                and abs(current['native_position'] - current['gpodder_raw_position']) <= 1):
            baseline=copy.deepcopy(baseline)
            baseline['pinepods']=states['pinepods']
    if all(same(next(iter(states.values())),value) for value in states.values()): return None
    changed=([k for k in states if not same(states[k],baseline[k])] if baseline else
             [k for k,value in states.items() if value['status']!='not_started'])
    if not changed: raise ValueError('Podcast sources differ without a recorded change')
    if not all(same(states[changed[0]],states[k]) for k in changed):
        raise ValueError('Concurrent podcast progress conflict')
    if states[changed[0]]['status']=='not_started' and (not baseline or states[changed[0]].get('missing')):
        raise ValueError('Missing podcast progress cannot request a reset')
    return changed[0]


class PodcastCoordinator:
    def __init__(self, bridge):
        self.b, self.cfg = bridge, settings(bridge)
        self.abs, self.jf = EpisodeABS(bridge.abs), bridge.jellyfin
        self.pine = getattr(bridge, 'pinepods', None) if self.cfg.get('pinepods_enabled') else None
        self.accounts = dict(abs={'url':bridge.config.get('abs_url'), 'username':self.cfg['expected_abs_username'],
                                  'user_id':self.cfg.get('expected_abs_user_id')},
                             jf={k:bridge.config.get('jellyfin',{}).get(k) for k in ('url','username','user_id')})
        if self.pine: self.accounts['pinepods']=self.pine.identity
        self.history = pine_history_ids(bridge)
        self.metadata_repair = None
        self.variant_checker = None

    def variant_proof(self, mapping, *, refresh=False, apply=False):
        from podcast_review import PREFIX, reported_proof
        reviewed = self.b.load(PREFIX + mapping['id'])
        if reviewed.get('mode')=='personal_route':
            from episode_editor import route_proof
            from podcast_status_sync import StatusCoordinator,StatusConflict,version_receipt
            proof=route_proof(self,mapping,reviewed['route'])
            if StatusCoordinator(self).binding(mapping,proof)!=reviewed['binding'] or version_receipt(proof)!=reviewed['version']:
                raise StatusConflict('variant_identity_changed')
            return proof
        if reviewed.get('mode') == 'reported':
            from podcast_status_sync import StatusCoordinator, StatusConflict, version_receipt
            proof = reported_proof(self, mapping)
            if (StatusCoordinator(self).binding(mapping, proof) != reviewed['binding']
                    or version_receipt(proof) != reviewed['version']):
                raise StatusConflict('variant_identity_changed')
            return proof
        from podcast_variant_proof import VariantProof
        if self.variant_checker is None:
            self.variant_checker=VariantProof(self.b)
        result=self.variant_checker.inspect(mapping,refresh=refresh,apply=apply)
        if result.get('ready'):
            mapping['_variant_proof']=result
        return result

    def variant_candidate(self,mapping):
        from podcast_variant_proof import VariantProof
        if self.variant_checker is None:
            self.variant_checker=VariantProof(self.b)
        return self.variant_checker.candidate(mapping) is True

    def select_variant(self,mapping,proof):
        mapping.update(_variant_selected=True,_pine_selected=False,_pine_mode='status_only',
                       _variant_proof=proof,_pine_status='joining')

    def repair_metadata(self, mapping, apply):
        # The existing metadata repair probes the ABS enclosure. An alias can
        # route Pine to a different publisher representation; never probe the
        # wrong URL or change its reviewed duration implicitly.
        if self.b.db.execute('SELECT 1 FROM state WHERE id=?', ('_pine_alias:'+mapping['id'],)).fetchone():
            outcome={'outcome':'waiting','reason':'reviewed_alias_duration_requires_review'}
            mapping['_pine_metadata_repair']=outcome
            return outcome
        from pine_metadata_repair import PineMetadataRepair
        if self.metadata_repair is None:
            self.metadata_repair = PineMetadataRepair(self.b)
        outcome = self.metadata_repair.repair_if_needed(mapping, apply)
        mapping['_pine_metadata_repair'] = outcome
        return outcome

    def alias_record(self, mapping):
        """Load exact reviewed state; removal or revision drift cannot rebind it."""
        from pine_identity_alias import state_key, candidate_route, AliasError
        row=self.b.db.execute('SELECT value FROM state WHERE id=?',(state_key(mapping['id']),)).fetchone()
        saved=self.b.load(mapping['id']); pending=saved.get('pending') or {}
        old=saved.get('pine_alias_fingerprint') or pending.get('pine_alias_fingerprint')
        if row is None:
            if old or mapping.get('_pine_alias_fingerprint'):
                raise AliasError('Previously used PinePods alias is missing; review required')
            return None
        try:record=json.loads(row[0])
        except (TypeError,ValueError):raise AliasError('PinePods alias state is invalid') from None
        route=candidate_route(record,mapping,abs_guid=mapping.get('abs_guid'),account=self.pine.identity)
        current=route['alias_fingerprint']
        if ((old is not None and old!=current)
                or (has_pine_history(saved) and old!=current)
                or (pending and pending.get('pine_alias_fingerprint')!=current)
                or (mapping.get('_pine_alias_fingerprint') is not None and mapping['_pine_alias_fingerprint']!=current)):
            raise AliasError('PinePods alias binding changed; review required')
        mapping['_pine_alias_fingerprint']=current
        return record

    def pine_route(self, mapping):
        """Resolve only Pine's route; original ABS/Jellyfin mapping stays intact."""
        from pine_identity_alias import candidate_route, resolve_alias, verify_source_history, AliasError
        record=self.alias_record(mapping)
        if record is None:
            return {'feed_url':mapping['feed_url'],'enclosure_url':mapping['enclosure_url'],
                    'duration':mapping['duration'],'alias_fingerprint':None}
        candidate=candidate_route(record,mapping,abs_guid=mapping.get('abs_guid'),account=self.pine.identity)
        # The old URL namespace is immutable after approval. New/reset/deleted
        # actions there are a conflict to review, never silently ignored.
        actions=self.pine.actions(mapping['feed_url'])
        verify_source_history(record,actions['actions'])
        try:
            native=self.pine.inspect_episode_metadata(candidate['feed_url'],candidate['enclosure_url'],mapping['duration'])
        except EpisodeUnavailable:
            raise AliasError('Reviewed PinePods alias native identity is unavailable') from None
        route=resolve_alias(record,mapping,abs_guid=mapping.get('abs_guid'),account=self.pine.identity,native=native)
        mapping['_pine_route']=route
        return route

    def account_guard(self, apply):
        previous=self.b.load('_podcast_accounts')
        if any(k in previous and not (same_pine_account(previous[k], value, self.b) if k == 'pinepods'
               else previous[k] == value) for k,value in self.accounts.items()):
            raise RuntimeError('A podcast participant account changed; previous progress cannot be reused')
        if apply:
            previous.update(self.accounts);self.b.save('_podcast_accounts',previous)

    def mappings(self):
        libraries = self.b.abs.libraries()
        matched = [r for r in libraries if r.get('id') == self.cfg['abs_library_id']]
        if len(matched) != 1 or matched[0].get('mediaType') != 'podcast':
            raise ValueError('Selected ABS library is not an accessible podcast library')
        results, issues, seen, files, urls = [], [], set(), set(), Counter()
        inventory = self.jf.items(parent=self.cfg['jellyfin_library_id'], types='AudioBook')
        for entry in self.b.abs.inventory(self.cfg['abs_library_id']):
            item = self.b.abs.item(identifier(entry.get('id')))
            episodes = item.get('media', {}).get('episodes')
            if not isinstance(episodes, list): raise ValueError('Incomplete ABS podcast inventory')
            for episode in episodes:
                if not isinstance(episode, dict): raise ValueError('Invalid ABS podcast episode')
                mapping = build_mapping(item, episode, self.cfg)
                if mapping is None: continue
                if mapping['id'] in seen or mapping['path'] in files:
                    raise ValueError('Duplicate ABS podcast episode identity')
                seen.add(mapping['id']); files.add(mapping['path'])
                if self.pine:
                    key = (mapping['feed_url'], mapping['enclosure_url'])
                    if not all(isinstance(v, str) and v for v in key):
                        mapping['_pine_unavailable'] = 'source_identity_missing'
                    else:
                        urls[key] += 1
                    if mapping.get('_pine_unavailable') and not self.cfg.get('pinepods_optional_enrollment'):
                        raise ValueError('PinePods requires unique podcast feed and episode URLs')
                index_reason='Episode is not indexed in the selected Jellyfin library'
                try:
                    target = bind(mapping, inventory)
                except JellyfinProbePending:
                    # Never turn a known participant or uncertain operation
                    # into an absent newcomer. No history is read or rewritten.
                    known=(self.b.db.execute('SELECT 1 FROM state WHERE id=? LIMIT 1',(mapping['id'],)).fetchone()
                        or self.b.db.execute('SELECT 1 FROM operations WHERE book=? LIMIT 1',(mapping['id'],)).fetchone())
                    if known:
                        raise ValueError('A previously mapped Jellyfin episode has lost its verified duration')
                    target=None
                    index_reason='Jellyfin has not finished indexing this new episode duration; synchronization waits for verification'
                if target is None:
                    issues.append(dict(id=mapping['id'], title=mapping['title'], show=mapping['show'],
                        progress_status='waiting', reason=index_reason,
                        participants=['abs','jf'], pine_status='waiting' if self.pine else 'disabled',
                        pine_reason_code='jellyfin_not_indexed' if self.pine else None))
                    continue
                mapping['jf'] = target
                results.append(mapping)
        for mapping in results:
            key = (mapping['feed_url'], mapping['enclosure_url'])
            if self.pine and not mapping.get('_pine_unavailable') and urls.get(key, 0) > 1:
                if not self.cfg.get('pinepods_optional_enrollment'):
                    raise ValueError('PinePods requires unique podcast feed and episode URLs')
                mapping['_pine_unavailable'] = 'source_identity_ambiguous'
        return results, issues

    def verify_mapping(self, mapping):
        item = self.b.abs.item(mapping['podcast_id'])
        found = [e for e in item.get('media', {}).get('episodes', []) if e.get('id') == mapping['episode_id']]
        if len(found) != 1: raise ValueError('Podcast episode disappeared or became ambiguous')
        fresh = build_mapping(item, found[0], self.cfg)
        if fresh is None or fresh['fingerprint'] != mapping['fingerprint']:
            raise ValueError('Podcast episode media changed')
        if mapping.get('_pine_selected', bool(self.pine)) and (fresh['feed_url'], fresh['enclosure_url']) != (mapping['feed_url'], mapping['enclosure_url']):
            raise ValueError('Podcast feed or episode URL changed')
        if self.pine and self.alias_record(mapping) is not None and fresh.get('abs_guid')!=mapping.get('abs_guid'):
            raise ValueError('Reviewed PinePods alias RSS GUID changed')

    def read(self, mapping):
        raw_abs = self.abs.read(mapping)
        items = self.jf.items(ids=[mapping['jf']['id']], types='AudioBook')
        if len(items) != 1: raise ValueError('Jellyfin podcast episode disappeared')
        raw_jf = jf_raw(items[0], mapping['jf'])
        states = dict(abs=abs_state(raw_abs, mapping), jf=jf_state(raw_jf, mapping['jf']))
        for endpoint in states:
            states[endpoint]['fingerprint']=fingerprint([states[endpoint]['fingerprint'],self.accounts[endpoint]])
        raw = dict(abs=raw_abs, jf=raw_jf)
        if mapping.get('_pine_selected', bool(self.pine)):
            route=self.pine_route(mapping)
            snapshot = self.pine.read_episode(route['feed_url'], route['enclosure_url'], mapping['duration'])
            if route['alias_fingerprint'] is not None:
                from pine_identity_alias import AliasError
                if (snapshot.get('native_user_id')!=route['native_user_id']
                        or snapshot.get('native_episode_id')!=route['native_episode_id']):
                    raise AliasError('Reviewed PinePods alias progress identity changed')
                snapshot=dict(snapshot,alias_fingerprint=route['alias_fingerprint'])
            states['pinepods'] = {k:snapshot[k] for k in ('position','status','missing')}
            pine_identity=[route['feed_url'],route['enclosure_url'],mapping['duration'],self.accounts['pinepods']]
            if route['alias_fingerprint'] is not None:pine_identity.append(route['alias_fingerprint'])
            states['pinepods']['fingerprint'] = fingerprint(pine_identity)
            raw['pinepods'] = snapshot
            states['gpodder'] = dict(position=snapshot['gpodder_position'],status=snapshot['gpodder_status'],
                missing=snapshot['gpodder_missing'],fingerprint=fingerprint([states['pinepods']['fingerprint'],'gpodder']))
            raw['gpodder'] = {k:snapshot.get(k) for k in ('gpodder_position','gpodder_status','gpodder_missing',
                'gpodder_revision','gpodder_raw_position','updated_at','device','action')}
        return states, raw

    def select_participants(self, mapping, stored, apply=False):
        enrolled = (mapping['id'] in self.history or has_pine_history(stored)) and not mapping.get('_transitioned')
        mapping['_pine_enrolled'] = enrolled
        mapping['_pine_selected'] = bool(self.pine)
        mapping['_pine_status'] = 'enrolled' if enrolled else 'joining' if self.pine else 'disabled'
        if not self.pine:
            if enrolled:
                raise ValueError('Previously enrolled PinePods participant is disabled; its history cannot be dropped')
            return self.read(mapping)
        if mapping.get('_variant_selected') or mapping.get('_variant_disabled'):
            mapping['_pine_selected']=False
            mapping['_pine_mode']='status_only'
            if enrolled:
                from podcast_status_sync import StatusConflict
                raise StatusConflict('variant_full_enrollment_review')
            return self.read(mapping)
        optional = self.cfg.get('pinepods_optional_enrollment') is True
        pending = stored.get('pending')
        code = mapping.get('_pine_unavailable')
        if pending and not enrolled and not has_pine_history(pending):
            code = 'core_transfer_pending'
        if self.alias_record(mapping) is not None and code:
            raise ValueError('Reviewed PinePods alias source or existing transfer is unavailable')
        # A successful HTTP reply may have been lost after a metadata write.
        # Reconcile its journal before allowing even an otherwise matching
        # PinePods duration to participate. Preview cannot clear this intent.
        metadata_key = '_pine_metadata:' + mapping['id']
        metadata_pending = (self.b.load(metadata_key).get('stage') == 'intent'
            or self.b.db.execute("SELECT 1 FROM operations WHERE book=? AND status='pending' LIMIT 1",
                                 (metadata_key,)).fetchone() is not None)
        if metadata_pending:
            outcome = self.repair_metadata(mapping, apply)
            if outcome['outcome'] != 'corrected':
                code = 'metadata_repair_pending'
        if code:
            if enrolled or not optional:
                raise ValueError(PINE_REASONS[code])
            mapping.update(_pine_selected=False, _pine_status='waiting', _pine_reason_code=code)
            return self.read(mapping)
        # A verified historical variant remains status-only even if today's
        # RSS value happens to match the ABS file length. Neither a normal
        # full-position read nor metadata repair may erase that classification.
        if (not enrolled and not pending and self.cfg.get('pinepods_variant_status_only')
                and self.variant_candidate(mapping)):
            proof=self.variant_proof(mapping,refresh=False,apply=apply)
            if not proof.get('ready'):
                proof=self.variant_proof(mapping,refresh=True,apply=apply)
            if proof.get('ready'):
                self.select_variant(mapping,proof)
                return self.read(mapping)
            if not optional:
                from podcast_status_sync import StatusConflict
                raise StatusConflict('variant_proof_required')
            mapping.update(_pine_selected=False,_pine_status='waiting',_pine_mode='status_only',
                           _pine_status_reason_code='variant_proof_required')
            return self.read(mapping)
        try:
            return self.read(mapping)
        except EpisodeUnavailable as exc:
            if self.alias_record(mapping) is not None:
                raise ValueError('Reviewed PinePods alias normal media guard failed') from None
            if exc.reason == 'duration_mismatch' and apply and not pending:
                outcome = self.repair_metadata(mapping, apply)
                if outcome['outcome'] in ('corrected', 'nochange'):
                    # All normal duration, receipt and conflict checks run
                    # again; a metadata repair is never a progress approval.
                    try:
                        return self.read(mapping)
                    except EpisodeUnavailable as retried:
                        exc = retried
            # A typed media mismatch/unresolved identity after a complete ID
            # inventory may defer admission. Auth, malformed/incomplete page
            # responses and uncertain writes never do.
            if enrolled and self.cfg.get('pinepods_variant_status_only') and exc.reason=='duration_mismatch':
                mapping['_pine_status_reason_code']='variant_full_enrollment_review'
                raise
            if enrolled or not optional or pending:
                raise
            if exc.reason=='duration_mismatch' and self.cfg.get('pinepods_variant_status_only'):
                proof=self.variant_proof(mapping,refresh=False,apply=apply)
                if not proof.get('ready'):
                    proof=self.variant_proof(mapping,refresh=True,apply=apply)
                if proof.get('ready'):
                    self.select_variant(mapping,proof)
                    return self.read(mapping)
                mapping['_pine_status_reason_code']='variant_proof_required'
            if exc.reason not in PINE_REASONS:
                raise
            mapping.update(_pine_selected=False, _pine_status='waiting', _pine_reason_code=exc.reason)
            return self.read(mapping)

    def save_baseline(self, key, identity, states, raw):
        enrolled = 'pinepods' in states or (key in self.history and not self.b.load('_pine_transition:'+key))
        value = dict(mapping=identity, baseline=states, pine_raw=raw.get('pinepods'))
        if enrolled:
            value['pine_enrolled'] = True
            self.history.add(key)
        if (raw.get('pinepods') or {}).get('alias_fingerprint'):
            value['pine_alias_fingerprint']=raw['pinepods']['alias_fingerprint']
        self.b.save(key, value)

    def handle(self, mapping, apply, settle):
        from podcast_status_sync import StatusCoordinator,StatusConflict,state_key,pending
        transition = self.b.load('_pine_transition:' + mapping['id'])
        mapping['_transitioned'] = bool(transition)
        saved=self.b.load(state_key(mapping['id']))
        if transition and not saved.get('binding'):
            raise StatusConflict('variant_pending_review')
        status_enabled = self.cfg.get('pinepods_variant_status_only') or transition.get('approved_by') == 'user'
        status_pending=pending(self.b,mapping['id'])
        has_status=bool(saved.get('binding') or saved.get('baseline') or status_pending)
        if has_status:
            mapping['_pine_mode']='status_only'
            if not self.pine or not status_enabled:
                if status_pending:raise StatusConflict('variant_pending_review')
                mapping.update(_variant_disabled=True,_pine_status_reason_code='variant_status_disabled')
            else:
                proof=self.variant_proof(mapping,refresh=False,apply=apply)
                if not proof.get('ready'):
                    proof=self.variant_proof(mapping,refresh=apply,apply=apply)
                if proof.get('ready'):
                    self.select_variant(mapping,proof)
                else:
                    mapping.update(_variant_disabled=True,_pine_status_reason_code='variant_proof_stale')
                    if status_pending:
                        mapping.update(_pine_selected=False,_pine_status='waiting')
                        return 'waiting',self.read(mapping)[0]
        if status_pending or (mapping.get('_variant_selected') and saved.get('reviewed_source',{}).get('target')):
            if self.b.load(mapping['id']).get('pending'):
                raise StatusConflict('variant_core_pending')
            return StatusCoordinator(self).handle(mapping,apply,settle)
        try:
            action,states=self.handle_position(mapping,apply,settle)
        except EpisodeUnavailable as mismatch:
            if (not apply or mismatch.reason != 'duration_mismatch' or not status_enabled
                    or transition or not mapping.get('_pine_enrolled') or not self.variant_candidate(mapping)):
                raise
            from podcast_review import inspect, commit, ReviewError
            try:
                public, snapshot = inspect(self, mapping, automatic=True)
            except ReviewError:
                raise mismatch from None
            states = commit(self, mapping, public, snapshot, 'abs', True)
            self.select_variant(mapping, snapshot[5])
            mapping.update(_pine_status='enrolled', _pine_enrolled=False)
            mapping.pop('_pine_status_reason_code', None)
            return 'unchanged',states
        if mapping.get('_variant_selected'):
            if action in ('waiting','proposed'):
                mapping['_pine_status_reason_code']='variant_core_pending'
                return action,states
            status_action,combined=StatusCoordinator(self).handle(mapping,apply,settle)
            return ('synced' if status_action=='unchanged' and action=='synced' else status_action),combined
        if mapping.get('_variant_disabled'):
            mapping['_pine_status']='disabled' if not status_enabled else 'waiting'
        return action,states

    def handle_position(self, mapping, apply, settle):
        b, key = self.b, mapping['id']
        stored = b.load(key)
        expected_identity = fingerprint([mapping['fingerprint'], mapping['jf']['fingerprint']])
        if stored.get('mapping') and stored['mapping'] != expected_identity:
            raise ValueError('Existing podcast mapping identity changed')
        states, raw = self.select_participants(mapping, stored, apply)
        pending = stored.get('pending')
        if pending:
            if pending.get('pine_plan'):
                return self.resume_pine(mapping, stored, states, raw, apply)
            if pending.get('mapping') != expected_identity:
                raise ValueError('Interrupted podcast transfer belongs to another media file')
            if set(states) == set(pending['before']) and all(same(v, pending['goal']) for v in states.values()):
                if apply:
                    pending.update(stage='reconciled', after=raw)
                    b.write_record(pending['id'], key, pending, 'complete')
                    self.save_baseline(key, expected_identity, states, raw)
                return 'unchanged', states
            raise RuntimeError('Interrupted podcast transfer requires journal review; it is not retried')
        manual=stored.get('manual_choice')
        if manual:
            if manual.get('snapshot')!=fingerprint(raw):raise ValueError('Hörstand seit der manuellen Auswahl geändert. Bitte erneut prüfen.')
            source=None if all(same(v,manual['goal']) for v in states.values()) else 'user'
        else:source = choose_source(states, stored.get('baseline'), raw, stored.get('pine_raw'))
        if source is None:
            if apply: self.save_baseline(key, expected_identity, states, raw)
            return 'unchanged', states
        if not apply: return 'proposed', states
        signature = fingerprint(raw)
        candidate = stored.get('candidate', {})
        if candidate.get('signature') != signature:
            candidate = dict(signature=signature, since=b.clock())
            stored['candidate'] = candidate; b.save(key, stored)
        if (not manual and b.clock() - candidate['since'] < settle) or mapping['jf']['id'] in self.jf.active_ids():
            return 'waiting', states
        goal = copy.deepcopy(manual['goal'] if manual else states[source])
        pine_plan=None
        if mapping['_pine_selected'] and any(not same(states[k],goal) for k in ('pinepods','gpodder')):
            latest=raw['pinepods'].get('updated_at')
            planned=max(int(b.clock()),int(latest)+1 if latest is not None else 0)
            route=self.pine_route(mapping)
            pine_plan=self.pine.prepare_actions(route['feed_url'],route['enclosure_url'],goal,
                mapping['duration'],timestamp=planned,previous=states['pinepods'])
            if b.clock()<action_epoch(pine_plan[0]): return 'waiting',states
        completion = None
        if goal['status'] == 'finished':
            if source == 'abs': completion = (raw['abs'] or {}).get('finishedAt')
            elif source == 'jf':
                value = raw['jf'].get('LastPlayedDate')
                if value:
                    stamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
                    if stamp.tzinfo is None: raise ValueError('Podcast completion date lacks a timezone')
                    completion = int(stamp.timestamp()*1000)
            elif source=='gpodder': completion=int(raw['pinepods']['updated_at']*1000)
            else: completion = int(b.clock()*1000)
            if type(completion) is not int or completion <= 0:
                raise ValueError('Podcast completion date is unavailable')

        def unchanged():
            guard(b); self.verify_mapping(mapping)
            _, fresh = self.read(mapping)
            if not raw_equal(fresh, raw): raise RuntimeError('Concurrent podcast progress changed before transfer')
            if mapping['jf']['id'] in self.jf.active_ids(): raise RuntimeError('Jellyfin podcast playback began during transfer')

        unchanged()
        record = dict(id=str(uuid.uuid4()), source=source, goal=goal, before=copy.deepcopy(raw),
                      mapping=expected_identity, stage='prepared')
        if mapping.get('_pine_alias_fingerprint'):
            record['pine_alias_fingerprint']=mapping['_pine_alias_fingerprint']
            stored['pine_alias_fingerprint']=mapping['_pine_alias_fingerprint']
        stored.update(mapping=expected_identity, pending=record)
        if mapping['_pine_selected']:
            stored['pine_enrolled'] = True
            self.history.add(key)
        b.save(key, stored)
        b.write_record(record['id'], key, record, 'pending')

        def checkpoint(label):
            record.update(stage=label, last_verified=copy.deepcopy(raw))
            b.save(key, stored); b.write_record(record['id'], key, record, 'pending')

        def abs_step(label, action, expected_position, statuses):
            nonlocal states, raw
            unchanged(); checkpoint('sending_abs_' + label); action()
            current, fresh = self.read(mapping)
            if any(not raw_equal({k: fresh[k]}, {k: raw[k]}) for k in raw if k != 'abs'):
                raise RuntimeError('Another podcast participant changed during ABS transfer')
            if abs(current['abs']['position'] - expected_position) > 1 or current['abs']['status'] not in statuses:
                raise RuntimeError('ABS podcast step was not confirmed')
            states, raw = current, fresh; checkpoint('verified_abs_' + label)

        if not same(states['abs'], goal):
            if states['abs']['status'] == 'finished' and goal['status'] != 'finished':
                abs_step('reopen', lambda:self.abs.finished(mapping, False), 0, {'not_started', 'in_progress'})
            if abs(states['abs']['position'] - goal['position']) > 1 or states['abs'].get('missing'):
                abs_step('position', lambda:self.abs.position(mapping, goal['position']), goal['position'], {'not_started' if goal['position']==0 else 'in_progress'})
            if states['abs']['status'] != goal['status']:
                abs_step('status', lambda:self.abs.finished(mapping, goal['status'] == 'finished', completion),
                         goal['position'], {goal['status']})
        if not same(states['jf'], goal):
            unchanged()
            values = {'PlaybackPositionTicks': round(goal['position'] * TICKS), 'Played': goal['status'] == 'finished',
                      'PlayCount': 0 if goal['status']=='not_started' else max(raw['jf']['PlayCount'], 1)}
            source_stamp = (raw['abs'] or {}).get('lastUpdate')
            if goal['status'] == 'finished': source_stamp = completion
            if source_stamp:
                values['LastPlayedDate'] = dt.datetime.fromtimestamp(source_stamp / 1000, dt.timezone.utc).isoformat()
            checkpoint('sending_jellyfin'); self.jf.update(mapping['jf']['id'], values)
            current, fresh = self.read(mapping)
            expected = dict(raw['jf'], **values)
            if (not rows_equal([fresh['jf']], [expected]) or
                    any(fresh[k] != raw[k] for k in raw if k != 'jf')):
                raise RuntimeError('Jellyfin podcast transfer was not confirmed')
            states, raw = current, fresh; checkpoint('verified_jellyfin')
        if mapping['_pine_selected'] and any(not same(states[k],goal) for k in ('pinepods','gpodder')):
            unchanged()
            record['pine_plan'] = pine_plan
            record['pine_next'] = 0
            checkpoint('waiting_pinepods')
            return self.resume_pine(mapping, stored, states, raw, apply)
        self.verify_mapping(mapping)
        states, raw = self.read(mapping)
        if not all(same(v, goal) for v in states.values()): raise RuntimeError('Podcast transfer result differs')
        record.update(stage='complete', after=raw)
        b.write_record(record['id'], key, record, 'complete')
        self.save_baseline(key, expected_identity, states, raw)
        b.event(key, 'podcast_synced', dict(title=mapping['title'], show=mapping['show'], source=source,
                seconds=round(goal['position'], 3), status=goal['status']))
        return 'synced', states

    def resume_pine(self, mapping, stored, states, raw, apply):
        """Continue only confirmed stages, never replay an uncertain POST.

        PinePods needs a separately confirmed reset before reopening. Its wire
        clock has one-second precision, so the second action is sent by the
        next normal cycle rather than sleeping or posting a future timestamp.
        """
        b, key, record = self.b, mapping['id'], stored['pending']
        identity=fingerprint([mapping['fingerprint'],mapping['jf']['fingerprint']])
        if (not self.pine or set(states)!=set(record['before']) or record['mapping']!=identity):
            raise ValueError('Interrupted PinePods transfer participants or media changed')
        if record.get('pine_alias_fingerprint')!=mapping.get('_pine_alias_fingerprint'):
            raise ValueError('Interrupted PinePods transfer alias changed')
        plan=record['pine_plan']; index=record['pine_next']
        if not isinstance(plan,list) or not plan or type(index)is not int or not 0<=index<=len(plan):
            raise ValueError('Invalid PinePods transfer journal')
        # Check every persisted route BEFORE readback recovery can advance an
        # uncertain sending stage. A matching progress snapshot alone carries
        # no URL evidence and must not certify a journal for another URL pair.
        if record.get('pine_alias_fingerprint'):
            route=mapping.get('_pine_route')
            if not route:raise ValueError('Interrupted PinePods alias route is unavailable')
            for action in plan:
                if (not isinstance(action,dict) or action.get('podcast')!=route['feed_url']
                        or action.get('episode')!=route['enclosure_url']):
                    raise ValueError('Interrupted PinePods alias action target changed')
                if '_native_action' in action:
                    if (action['_native_action'] not in ('completed','uncompleted')
                            or action.get('_episode_id')!=route['native_episode_id']
                            or action.get('_user_id')!=route['native_user_id']):
                        raise ValueError('Interrupted PinePods alias native action changed')
                elif '_episode_id' in action or '_user_id' in action:
                    raise ValueError('Interrupted PinePods alias action has unexpected native IDs')
        def matches(snapshot, action, before):
            if hasattr(self.pine,'action_confirmed'):
                if not self.pine.action_confirmed(snapshot,action,mapping['duration']): return False
                if any(snapshot.get(k)!=before.get(k) for k in ('native_episode_id','native_user_id')):
                    return False
                if action.get('_native_action'):
                    # Native completion/reset cannot replace a concurrent
                    # incoming gpodder action that arrived during the POST.
                    return snapshot.get('gpodder_revision')==before.get('gpodder_revision')
                # The verified Go 0.9 wire endpoint changes position only on
                # play; it never modifies the native completion flag.
                if snapshot.get('native_completed')!=before.get('native_completed'): return False
                return action['action']!='new' or snapshot.get('native_position')==before.get('native_position')
            expected_status='not_started' if action['action']=='new' else ('finished' if action['position']>=action['total'] else 'in_progress')
            expected_pos=0 if expected_status=='not_started' else mapping['duration'] if expected_status=='finished' else action['position']
            return (snapshot.get('updated_at')==action_epoch(action) and
                    snapshot.get('action')==action['action'] and snapshot.get('device')==action['device'] and
                    same(snapshot,dict(position=expected_pos,status=expected_status)))
        def peers_equal(fresh, previous):
            return all(raw_equal({k:fresh[k]},{k:previous[k]}) for k in previous if k not in ('pinepods','gpodder'))
        def checkpoint(stage):
            record.update(stage=stage,last_verified=copy.deepcopy(raw),pine_next=index)
            b.save(key,stored);b.write_record(record['id'],key,record,'pending')
        def advance_schedule(action, snapshot):
            if action.get('_native_action'): return
            # Go ignores the client's timestamp. A delayed cycle may make all
            # originally planned steps overdue: schedule the next wire action
            # against the ACTUAL server receipt to prevent a same-second tie.
            stamp=snapshot.get('updated_at')
            if type(stamp) is not int: raise ValueError('PinePods action lacks a server receipt timestamp')
            for future in plan[index+1:]:
                if future.get('_native_action'): continue
                stamp=max(stamp+1,action_epoch(future))
                future['timestamp']=dt.datetime.fromtimestamp(stamp,dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

        previous=record.get('last_verified')
        if not isinstance(previous,dict): raise ValueError('PinePods journal lacks verified state')
        if record['stage'].startswith('sending_pinepods_'):
            if index>=len(plan) or not matches(raw['pinepods'],plan[index],previous['pinepods']) or not peers_equal(raw,previous):
                raise RuntimeError('Uncertain PinePods action requires review; no retry was sent')
            advance_schedule(plan[index],raw['pinepods']);index+=1
            if apply: checkpoint('verified_pinepods_'+str(index-1))
        elif not raw_equal(raw,previous):
            raise RuntimeError('Podcast progress changed while a PinePods transfer was waiting')
        if not apply: return 'waiting',states
        while index<len(plan):
            action=plan[index]
            if b.clock()<action_epoch(action):
                checkpoint('waiting_pinepods');return 'waiting',states
            guard(b);self.verify_mapping(mapping)
            _,fresh=self.read(mapping)
            if not raw_equal(fresh,raw): raise RuntimeError('Concurrent podcast edit before PinePods action')
            route=mapping.get('_pine_route')
            if record.get('pine_alias_fingerprint'):
                if (not route or action.get('podcast')!=route['feed_url'] or action.get('episode')!=route['enclosure_url']
                        or action.get('_native_action') and (action.get('_episode_id')!=route['native_episode_id']
                            or action.get('_user_id')!=route['native_user_id'])):
                    raise ValueError('Interrupted PinePods alias action target changed')
            if mapping['jf']['id'] in self.jf.active_ids(): return 'waiting',states
            checkpoint('sending_pinepods_'+str(index))
            receipt=self.pine.write_action(action)
            if isinstance(receipt,dict) and receipt.get('timestamp') is not None:
                action['_server_timestamp']=action_epoch(receipt['timestamp'])
                checkpoint('sending_pinepods_'+str(index))
            current,fresh=self.read(mapping)
            if not peers_equal(fresh,raw) or not matches(fresh['pinepods'],action,raw['pinepods']):
                raise RuntimeError('PinePods action was not confirmed')
            states,raw=current,fresh;advance_schedule(action,raw['pinepods']);index+=1;checkpoint('verified_pinepods_'+str(index-1))
        if not all(same(v,record['goal']) for v in states.values()): raise RuntimeError('Podcast transfer result differs')
        record.update(stage='complete',after=raw)
        b.write_record(record['id'],key,record,'complete')
        self.save_baseline(key, identity, states, raw)
        b.event(key,'podcast_synced',dict(title=mapping['title'],show=mapping['show'],source=record['source'],
                seconds=round(record['goal']['position'],3),status=record['goal']['status']))
        return 'synced',states

    def cycle(self, apply=False, settle=30):
        self.metadata_repair = None
        self.variant_checker = None
        guard(self.b)
        if self.pine: self.accounts['pinepods']=self.pine.identity
        self.account_guard(apply)
        mappings, issues = self.mappings()
        if apply and self.pine:
            from pine_metadata_repair import metadata_probe_order
            mappings = metadata_probe_order(self.b, mappings)
            if self.cfg.get('pinepods_variant_status_only'):
                from podcast_variant_proof import variant_probe_order
                mappings = variant_probe_order(self.b,mappings)
        live = {m['id'] for m in mappings}
        # A deleted/rotated podcast file must not hide a partial operation.
        orphaned = []
        for key, value in self.b.db.execute("SELECT id,value FROM state WHERE id LIKE '_podcast:%'"):
            if key not in live and json.loads(value).get('pending'):
                orphaned.append(key)
        from podcast_status_sync import NAMESPACE as STATUS_NAMESPACE
        for key,value in self.b.db.execute('SELECT id,value FROM state WHERE id LIKE ?', (STATUS_NAMESPACE+'%',)):
            if key.startswith(STATUS_NAMESPACE) and key[len(STATUS_NAMESPACE):] not in live and json.loads(value).get('pending'):
                orphaned.append(key)
        for (key,) in self.b.db.execute("SELECT DISTINCT book FROM operations WHERE book LIKE ? AND status='pending'", (STATUS_NAMESPACE+'%',)):
            if key.startswith(STATUS_NAMESPACE) and key[len(STATUS_NAMESPACE):] not in live:orphaned.append(key)
        if orphaned: raise RuntimeError('An unavailable podcast episode has an unresolved transfer')
        result = dict(at=dt.datetime.now(dt.timezone.utc).isoformat(), enabled=True, mode='apply' if apply else 'dry-run',
            scope_fingerprint=fingerprint(sorted((m['id'],m['fingerprint'],m['jf']['fingerprint'],m.get('abs_guid'),
                m.get('feed_url'),m.get('enclosure_url')) for m in mappings)),
            mapped=len(mappings), synced=0, unchanged=0, waiting=len(issues), blocked=[], proposed=0,
            episodes=list(issues), bookorbit_prepared=True, bookorbit_enabled=False,
            pine_optional_enrollment=self.cfg.get('pinepods_optional_enrollment') is True)
        result['pine_metadata_orphaned'] = [key.removeprefix('_pine_metadata:')
            for (key,) in self.b.db.execute("SELECT DISTINCT book FROM operations WHERE book LIKE '_pine_metadata:%' AND status='pending'")
            if key.removeprefix('_pine_metadata:') not in live]
        for key in result['pine_metadata_orphaned']:
            result['blocked'].append(dict(id=key, title='Podcast metadata correction',
                reason='An unavailable podcast episode has an unresolved PinePods metadata correction'))
        for index,mapping in enumerate(mappings):
            if getattr(self.b,"serve_preview",None):self.b.serve_preview()
            row = dict(id=mapping['id'], title=mapping['title'], show=mapping['show'], duration=mapping['duration'],
                       participants=['abs','jf'] + (['pinepods','gpodder'] if self.pine else []),
                       pine_status='joining' if self.pine else 'disabled',reviewed_route=self.b.load('_pine_transition:'+mapping['id']).get('mode')=='personal_route')
            try:
                action, states = self.handle(mapping, apply, settle)
                result[action] += 1
                base = states['abs']
                row.update(position=base['position'], status=base['status'], progress_status=action,
                           states={k:dict(position=v['position'],status=v['status']) for k,v in states.items()},
                           participants=list(states))
                if (mapping.get('_pine_selected') and apply
                        and has_pine_history(self.b.load(mapping['id']).get('baseline'))):
                    mapping['_pine_status'] = 'enrolled'
            except Exception as exc:
                reason = str(exc) if isinstance(exc, (ValueError, RuntimeError, EpisodeUnavailable)) or type(exc).__name__ == 'RequestError' else type(exc).__name__
                row.update(progress_status='blocked', reason=reason)
                if mapping.get('_pine_selected', bool(self.pine)) or mapping.get('_pine_enrolled') or mapping.get('_variant_selected'):
                    mapping['_pine_status'] = 'blocked'
                if hasattr(exc,'code') and type(exc).__name__=='StatusConflict':
                    mapping['_pine_status_reason_code']=exc.code
                    mapping['_pine_status']='blocked'
                if isinstance(exc, EpisodeUnavailable):
                    mapping['_pine_reason_code'] = exc.reason
                result['blocked'].append(dict(id=row['id'],title=row['title'],reason=reason))
                if apply:
                    stored=self.b.load(row['id'])
                    if stored.get('last_error') != reason:
                        stored['last_error']=reason;self.b.save(row['id'],stored)
                        self.b.event(row['id'],'podcast_blocked',dict(title=row['title'],reason=reason))
                        self.b.alert(row['id'],'Podcast-Sync pausiert: '+row['title']+' — '+reason)
                self.pine_row(mapping, row)
                if service_unavailable(exc):
                    result['episodes'].append(row)
                    for deferred in mappings[index+1:]:
                        result['episodes'].append(dict(id=deferred['id'],title=deferred['title'],show=deferred['show'],
                            progress_status='waiting',reason='A podcast service is unavailable; retry next cycle',
                            participants=['abs','jf']+(['pinepods','gpodder'] if self.pine else []),
                            pine_status='blocked' if self.pine else 'disabled'))
                    result['waiting']+=len(mappings)-index-1
                    break
            self.pine_row(mapping, row)
            result['episodes'].append(row)
        for status in ('waiting','enrolled','joining','blocked'):
            result['pine_'+status] = sum(row.get('pine_status') == status for row in result['episodes'])
        result['ok'] = not result['blocked']
        return result

    def pine_row(self, mapping, row):
        row['pine_mode']=mapping.get('_pine_mode','position')
        if mapping.get('_pine_status_reason_code'):
            row['pine_status_reason_code']=mapping['_pine_status_reason_code']
        if mapping.get('_pine_metadata_repair'):
            row['pine_metadata_repair'] = mapping['_pine_metadata_repair']
        row['pine_status'] = mapping.get('_pine_status', row['pine_status'])
        enrolled = mapping.get('_pine_enrolled', mapping['id'] in self.history)
        if enrolled:
            row['participants'] = ['abs','jf','pinepods','gpodder']
        elif mapping.get('_variant_selected'):
            row['participants']=['abs','jf','pinepods','gpodder']
        elif mapping.get('_pine_selected') is False:
            row['participants'] = ['abs','jf']
        code = mapping.get('_pine_reason_code')
        if code:
            row['pine_reason_code'] = code
            row['pine_reason'] = PINE_REASONS.get(code, 'PinePods admission requires review.')


def preview(bridge):
    """No application writes, no baseline insertion, no files modified."""
    return PodcastCoordinator(bridge).cycle(apply=False)


def run(bridge, apply=False, settle=30):
    cfg=settings(bridge)
    if not cfg.get('enabled'):
        return dict(enabled=False, ok=True, bookorbit_prepared=True, bookorbit_enabled=False, episodes=[], mapped=0, blocked=[], synced=0, unchanged=0, waiting=0, proposed=0)
    try:
        result=PodcastCoordinator(bridge).cycle(apply=apply, settle=settle)
        if apply: bridge.save('_podcast_global_error',{})
    except Exception as exc:
        reason=str(exc) if isinstance(exc,(ValueError,RuntimeError,EpisodeUnavailable)) or type(exc).__name__=='RequestError' else type(exc).__name__
        result=dict(at=dt.datetime.now(dt.timezone.utc).isoformat(),enabled=True,ok=False,mapped=0,synced=0,waiting=0,
                    blocked=[dict(reason=reason)],episodes=[],bookorbit_prepared=True,bookorbit_enabled=False)
        if apply and bridge.load('_podcast_global_error').get('error')!=reason:
            bridge.alert('podcasts','Podcast-Sync gesperrt: '+reason)
            bridge.save('_podcast_global_error',{'error':reason})
    if apply:
        destination=bridge.root/'last-podcast-run.json';temporary=destination.with_suffix('.tmp')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as stream:
            stream.write(encode(result));stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,destination)
    return result
