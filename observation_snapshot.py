"""Explicit, cycle-scoped API observations; live clients always stay fresh.

Snapshots are installed only around initial observation reads. They cannot send
mutations. Exact admission, recovery, CAS checks and readback use the original
clients after the scope exits. Every new cycle creates a new ObservationBatch.
No media files, subscriptions, playback states or persistent cursors are changed.
"""
import copy
from contextlib import contextmanager
import json
import re

from abs_client import ABSClient, RequestError as ABSError
from pinepods_client import PinepodsClient


def _key(args, kwargs):
    return json.dumps([args, kwargs], sort_keys=True, separators=(',', ':'), allow_nan=False)


def _readonly(method, path, *, pine=False, bookorbit=False):
    return method == 'GET' or method == 'POST' and (
        pine and path == '/api/data/get_episode_metadata'
        or bookorbit and path == '/api/v1/books/query')


class _MemoRequests:
    def _initialize(self, source, request_name='_request', *, pine=False, bookorbit=False):
        self.__dict__ = copy.copy(source.__dict__)
        self._observation_source = source
        self._observation_cache = {}
        self._observation_request_name = request_name
        self._observation_pine = pine
        self._observation_bookorbit = bookorbit
        self._observation_only = True
        self._observation_owner = None
        # A snapshot owns no transport or login. Closing it must not close its
        # underlying client; identities are checked by the original cycle guard.
        self._observation_request = getattr(source, request_name)
        setattr(self, request_name, self._memo_request)

    def _memo_value(self, method, path, *args, **kwargs):
        """Internal borrowed JSON. Public readers must return isolated copies."""
        if not _readonly(method, path, pine=self._observation_pine,
                         bookorbit=self._observation_bookorbit):
            raise RuntimeError('Observation snapshot cannot send mutations')
        key = _key([method, path, *args], kwargs)
        if key not in self._observation_cache:
            self._count('api_observation_requests')
            self._observation_cache[key] = self._observation_request(method, path, *args, **kwargs)
        else:self._count('api_observation_cache_hits')
        return self._observation_cache[key]

    def _memo_request(self, method, path, *args, **kwargs):
        return copy.deepcopy(self._memo_value(method,path,*args,**kwargs))

    def _fresh_request(self, method, path, *args, **kwargs):
        if not _readonly(method,path,pine=self._observation_pine,bookorbit=self._observation_bookorbit):
            raise RuntimeError('Observation snapshot cannot send mutations')
        self._count('api_observation_requests')
        return self._observation_request(method,path,*args,**kwargs)

    def _drop_requests(self, predicate):
        for key in list(self._observation_cache):
            args,kwargs=json.loads(key)
            if predicate(args,kwargs):self._observation_cache.pop(key,None)

    def _count(self, name):
        if self._observation_owner is not None:
            from io_metrics import increment
            increment(self._observation_owner, name)

    def close(self):
        self._observation_cache.clear()


class ABSObservation(_MemoRequests, ABSClient):
    """One expanded item per show and one indexed personal progress inventory."""
    def __init__(self, source):
        self._initialize(source)
        self._items = {}
        self._episodes = {}
        self._progress_index = None
        self._dirty_progress=set()
        self._progress_overrides={}

    def _load_item(self,item_id):
        self._id(item_id)
        if item_id not in self._items:
            self._items[item_id]=self._memo_value('GET','/api/items/'+self._id(item_id),
                '/api/items/{item_id}',params={'expanded':1,'include':'progress'})
        return self._items[item_id]

    def item(self, item_id):
        return copy.deepcopy(self._load_item(item_id))

    def episode_item(self, item_id, episode_id):
        """Same validated source fields with exactly one indexed episode.

        This avoids re-copying/scanning the complete show for each source key.
        Duplicate/missing IDs fail closed just like the ordinary source reader.
        """
        self._id(item_id); self._id(episode_id)
        item = self._load_item(item_id)
        if item_id not in self._episodes:
            rows = item.get('media', {}).get('episodes')
            if not isinstance(rows, list):
                raise ABSError('GET /api/items/{item_id}: incomplete episode inventory')
            index = {}
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'] or row['id'] in index:
                    raise ABSError('GET /api/items/{item_id}: duplicate or missing episode ID')
                index[row['id']] = row
            self._episodes[item_id] = index
        row = self._episodes[item_id].get(episode_id)
        if row is None:
            raise ABSError('GET /api/items/{item_id}: episode is unavailable')
        result = {k: copy.deepcopy(v) for k, v in item.items() if k != 'media'}
        result['media'] = {k: copy.deepcopy(v) for k, v in item['media'].items() if k != 'episodes'}
        result['media']['episodes'] = [copy.deepcopy(row)]
        self._count('api_observation_index_hits')
        return result

    def _load_progress(self):
        if self._progress_index is None:
            data=self._memo_value('GET','/api/me/progress','/api/me/progress')
            self._index_progress(self._list(data,'mediaProgress','/api/me/progress'))

    def allprogress(self):
        self._load_progress()
        for key in list(self._dirty_progress):self._refresh_progress(*key)
        values=dict(self._progress_index)
        values.update(self._progress_overrides)
        return [copy.deepcopy(row) for row in values.values() if row is not None]

    def invalidate_progress(self,*,item_id=None,episode_id=None,**scope):
        if item_id is None:
            self._progress_index=None;self._dirty_progress.clear();self._progress_overrides.clear()
            self._drop_requests(lambda args,kwargs:args[1].startswith('/api/me/progress'))
            return
        key=(item_id,episode_id)
        self._progress_overrides.pop(key,None);self._dirty_progress.add(key)

    def _refresh_progress(self,item_id,episode_id):
        path='/api/me/progress/'+self._id(item_id)
        if episode_id is not None:path+='/'+self._id(episode_id)
        row=self._fresh_request('GET',path,'/api/me/progress/{item_id}/{episode_id}',missing_ok=True)
        if row is not None and (not isinstance(row,dict) or row.get('libraryItemId')!=item_id
                                or row.get('episodeId')!=episode_id):
            raise ABSError('GET /api/me/progress: invalid progress identity')
        key=(item_id,episode_id)
        self._progress_overrides[key]=copy.deepcopy(row);self._dirty_progress.discard(key)

    def _progress(self,item_id,episode_id):
        self._id(item_id)
        if episode_id is not None:self._id(episode_id)
        key=(item_id,episode_id)
        if key in self._dirty_progress:self._refresh_progress(*key)
        if key in self._progress_overrides:row=self._progress_overrides[key]
        else:
            self._load_progress();row=self._progress_index.get(key)
        self._count('api_observation_index_hits')
        return copy.deepcopy(row)

    def _index_progress(self, rows):
        if self._progress_index is not None:
            return
        index = {}
        for row in rows:
            item = row.get('libraryItemId')
            episode = row.get('episodeId')
            if not isinstance(item, str) or not item or episode is not None and (not isinstance(episode, str) or not episode):
                raise ABSError('GET /api/me/progress: invalid progress identity')
            key = (item, episode)
            if key in index:
                raise ABSError('GET /api/me/progress: duplicate progress identity')
            index[key] = row
        self._progress_index = index

    def episode_progress(self,item_id,episode_id):
        return self._progress(item_id,episode_id)

    def progress(self,item_id):
        return self._progress(item_id,None)


class PineObservation(_MemoRequests, PinepodsClient):
    """Complete per-feed identities/actions indexed once; no mutation methods."""
    def __init__(self, source):
        self._initialize(source, pine=True)
        self._native_user_id = None
        self._native_ids = {}
        self._catalogs = {}
        self._catalog_data = {}
        self._calibrated = {}
        self._metadata = {}
        self._actions_index = {}
        self._dirty_metadata=set()
        self._dirty_actions=set()

    def invalidate_progress(self,*,feed_url=None,enclosure_url=None,**scope):
        if feed_url is None or enclosure_url is None:
            self._metadata.clear();self._actions_index.clear()
            self._calibrated.clear();self._catalogs.clear();self._catalog_data.clear()
            self._observation_cache.clear()
            return
        key=(feed_url,enclosure_url)
        self._metadata.pop(key,None)
        self._dirty_metadata.add(key);self._dirty_actions.add(key)
        # Typed indexed readers retain unrelated actions. Explicit full-feed
        # readers (alias-history guards) must not receive the old raw envelope.
        self._drop_requests(lambda args,kwargs:args[1].startswith('/api/2/episodes/')
            and (kwargs.get('params') or {}).get('podcast') in (None,feed_url))

    def _native_catalog(self, feed_url, user_id):
        key = (feed_url, user_id)
        if key not in self._catalogs:
            self._catalogs[key] = PinepodsClient._native_catalog(self, feed_url, user_id)
        return self._catalogs[key]

    def _accept_catalog(self, feed_url, user_id, pod, rows):
        self._catalog_data[(feed_url, user_id)] = (copy.deepcopy(pod), {r['Episodeid']:r for r in rows})

    def _projection(self, feed_url, user_id, enclosure_url):
        # PinePods 0.9.0 exposes the same fields with legacy capitalized aliases
        # in its personal paginated feed API. Validate a representative full
        # metadata response for each feed in this cycle before using this shape.
        from pinepods_client import EpisodeUnavailable
        podcast_id, by_url, empty = self._native_catalog(feed_url, user_id)
        matches = by_url.get(enclosure_url, [])
        if not matches:
            raise EpisodeUnavailable('episode_identity_unresolved' if empty else 'episode_missing')
        if len(matches) != 1:raise EpisodeUnavailable('episode_ambiguous')
        episode_id = matches[0]
        pod, rows = self._catalog_data[(feed_url, user_id)]
        row = {key.lower():value for key,value in rows[episode_id].items()}
        keys = ('completed','episodeartwork','episodedescription','episodeduration','episodeid',
                'episodepubdate','episodetitle','episodeurl','is_youtube','listenduration')
        try:
            projected = {key:row[key] for key in keys}
            projected.update({key:pod[key] for key in ('artworkurl','feedurl','podcastid','podcastindexid','podcastname')})
            projected.update({target:row[source] for source,target in
                              [('downloaded','is_downloaded'),('queued','is_queued'),('saved','is_saved')]})
        except KeyError:return None, episode_id, podcast_id
        return projected, episode_id, podcast_id

    def inspect_episode_metadata(self, feed_url, enclosure_url, duration):
        from pinepods_client import _number, _url, RequestError
        _url(feed_url); _url(enclosure_url); _number(duration, minimum=0.001)
        user_id = self._ensure_native()
        key = (feed_url, enclosure_url)
        if key in self._dirty_metadata:
            # Catalog progress fields are old after a write. Recheck only this
            # episode with the original client's complete identity guards.
            self._metadata[key]=copy.deepcopy(self._observation_source.inspect_episode_metadata(
                feed_url,enclosure_url,duration))
            self._dirty_metadata.discard(key)
        if key not in self._metadata:
            projected, episode_id, podcast_id = self._projection(feed_url, user_id, enclosure_url)
            if (feed_url not in self._calibrated or projected is None
                    or self._calibrated.get(feed_url) is not True):
                native = PinepodsClient.inspect_episode_metadata(self, feed_url, enclosure_url, duration)
                if feed_url not in self._calibrated:
                    self._calibrated[feed_url] = projected is not None and native['metadata'] == projected
                self._metadata[key] = native
            else:
                self._metadata[key] = self._native_from_row(projected, feed_url, enclosure_url, duration,
                    episode_id, user_id, podcast_id, _inspection=True)
        result = copy.deepcopy(self._metadata[key])
        result['expected_duration'] = duration
        if result['user_id'] != self._ensure_native():
            raise RequestError('PinePods: personal native account changed during observation')
        self._count('api_observation_index_hits')
        return result

    def _episode_actions(self, feed_url, enclosure_url):
        key=(feed_url,enclosure_url)
        if key in self._dirty_actions:
            # gPodder exposes a per-feed history endpoint; read it live once
            # for the changed target, retaining all unrelated indexed actions.
            rows=self._observation_source._episode_actions(feed_url,enclosure_url)
            selected=[copy.deepcopy(row) for row in rows
                      if row['podcast']==feed_url and row['episode']==enclosure_url]
            if feed_url not in self._actions_index:
                index={}
                for row in rows:index.setdefault((row['podcast'],row['episode']),[]).append(copy.deepcopy(row))
                self._actions_index[feed_url]=index
            else:self._actions_index[feed_url][key]=selected
            self._dirty_actions.discard(key)
        if feed_url not in self._actions_index:
            data = PinepodsClient.actions(self, feed_url)
            index = {}
            for row in data['actions']:
                index.setdefault((row['podcast'], row['episode']), []).append(row)
            self._actions_index[feed_url] = index
        return copy.deepcopy(self._actions_index[feed_url].get((feed_url, enclosure_url), []))

    def _native_episode(self, feed_url, enclosure_url, duration, *, _inspection=False):
        if _inspection:
            return PinepodsClient._native_episode(self, feed_url, enclosure_url, duration, _inspection=True)
        from pinepods_client import EpisodeUnavailable, RequestError
        native = self.inspect_episode_metadata(feed_url, enclosure_url, duration)
        if native['duration'] == 0 or abs(native['duration'] - duration) > 2:
            raise EpisodeUnavailable('duration_mismatch')
        if not native['completed'] and (native['position'] or 0) > native['duration'] + 2:
            raise RequestError('PinePods: native episode metadata or progress is invalid')
        return {key: native[key] for key in ('episode_id', 'user_id', 'duration', 'position', 'completed')}


class JellyfinObservation(_MemoRequests):
    """Index already validated personal inventories for initial per-item reads."""
    def __init__(self, source):
        self._initialize(source, request_name='request')
        self._inventories = []
        self._dirty_ids=set()

    def invalidate_progress(self,*,jf_ids=None,**scope):
        if jf_ids is None:
            self._inventories.clear();self._observation_cache.clear();self._dirty_ids.clear()
        else:self._dirty_ids.update(jf_ids)

    def _refresh_items(self,ids,parent,types):
        from jellyfin_client import JellyfinClient
        self._drop_requests(lambda args,kwargs:args[1]=='/Items'
            and bool(set(((args[2] if len(args)>2 else kwargs.get('params',{})) or {}).get('ids','').split(',')) & set(ids)))
        rows=JellyfinClient.items(self,parent,ids,types)
        for group in self._inventories:
            if group['types']!=types:continue
            for row in rows:
                ident=row['Id']
                if ident in group['index']:
                    order,_=group['index'][ident]
                    group['index'][ident]=(order,copy.deepcopy(row))
        self._dirty_ids.difference_update(ids)

    def items(self, parent=None, ids=None, types='AudioBook,Book'):
        from jellyfin_client import JellyfinClient
        dirty=[ident for ident in ids or [] if ident in self._dirty_ids]
        if not ids:
            dirty=list({ident for group in self._inventories
                        if group['types']==types and group['parent']==parent
                        for ident in group['index'] if ident in self._dirty_ids})
        if dirty:self._refresh_items(dirty,parent,types)
        if ids:
            if (len(set(ids)) != len(ids)
                    or any(not isinstance(value,str) or not re.fullmatch('[a-f0-9]{32}',value) for value in ids)):
                raise ValueError('Invalid requested Jellyfin identifiers')
            for group in self._inventories:
                if (group['types']==types and (parent is None or group['parent']==parent)
                        and all(value in group['index'] for value in ids)):
                    ordered=sorted(group['index'][value] for value in ids)
                    self._count('api_observation_index_hits')
                    return [copy.deepcopy(row) for _,row in ordered]
        else:
            for group in self._inventories:
                if group['types']==types and group['parent']==parent and group['complete']:
                    self._count('api_observation_index_hits')
                    return [copy.deepcopy(row) for _,row in group['index'].values()]
        rows=JellyfinClient.items(self,parent,ids,types)
        self._inventories.append(dict(types=types,parent=parent,complete=not ids,
            index={row['Id']:(i,copy.deepcopy(row)) for i,row in enumerate(rows)}))
        return rows

    def __getattr__(self, name):
        # This facade intentionally delegates methods through the validated
        # client implementation, with this object's guarded request transport.
        from jellyfin_client import JellyfinClient
        method=getattr(JellyfinClient,name,None)
        if callable(method):return method.__get__(self,type(self))
        raise AttributeError(name)


class ObservationBatch:
    """Install read-only snapshots only for a chosen initial-read call chain.

    Usage: ``with batch.scope((bridge, 'abs'), (coordinator, 'pine')): read()``.
    The same live client receives the same snapshot throughout one batch. Nested
    scopes restore prior attributes on every exit, including failures. Unsupported
    fake/test clients remain unchanged. Never wrap a transaction or entire cycle.
    """
    def __init__(self, owner=None):
        self.owner = owner
        self._clients = {}

    def client(self, source):
        if source is None:
            return None
        if getattr(source, '_observation_only', False) is True:
            source = source._observation_source
        key = id(source)
        if key not in self._clients:
            if type(source) is ABSClient:
                snapshot = ABSObservation(source)
            elif type(source) is PinepodsClient:
                snapshot = PineObservation(source)
            else:
                from bo_client import BOClient
                from jellyfin_client import JellyfinClient
                if type(source) is JellyfinClient:
                    snapshot=JellyfinObservation(source)
                elif type(source) is BOClient:
                    class GenericObservation(_MemoRequests, BOClient):
                        pass
                    snapshot=object.__new__(GenericObservation)
                    snapshot._initialize(source,bookorbit=True)
                else:return source
            snapshot._observation_owner = self.owner
            self._clients[key] = snapshot
        return self._clients[key]

    @contextmanager
    def scope(self, *bindings):
        previous = []
        try:
            for owner, attribute in bindings:
                value = getattr(owner, attribute, None)
                replacement = self.client(value)
                previous.append((owner, attribute, value))
                setattr(owner, attribute, replacement)
            yield
        finally:
            for owner, attribute, value in reversed(previous):
                setattr(owner, attribute, value)

    @contextmanager
    def live(self,*bindings):
        """Temporarily expose original clients without discarding observations."""
        previous=[]
        try:
            for owner,attribute in bindings:
                value=getattr(owner,attribute,None)
                source=value._observation_source if getattr(value,'_observation_only',False) is True else value
                previous.append((owner,attribute,value));setattr(owner,attribute,source)
            yield
        finally:
            for owner,attribute,value in reversed(previous):setattr(owner,attribute,value)

    def invalidate(self,*sources,**scope):
        if not sources:
            self._clients.clear()
            return
        for source in sources:
            if getattr(source,'_observation_only',False) is True:source=source._observation_source
            key=id(source);snapshot=self._clients.get(key)
            if snapshot is None:continue
            invalidate=getattr(snapshot,'invalidate_progress',None)
            if scope and callable(invalidate):invalidate(**scope)
            else:self._clients.pop(key,None)
