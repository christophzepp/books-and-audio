"""Episode-scoped review of waiting participants, using the personal catalog."""
import copy
import math
from podcast_review import ReviewError, check_pending, PREFIX
from podcast_sync import PodcastCoordinator, fingerprint, encode, guard, has_pine_history
from podcast_status_sync import StatusCoordinator, state_key, version_receipt
from podcast_variant_proof import VariantProof, native_binding
from pine_identity_alias import source_history_evidence
from pinepods_client import EpisodeUnavailable

TARGETS = ('finished','not_started')
AUDIT_PREFIX = '_pine_transition_audit:'
PREVIEW_AGE = 150


def candidate(pine,m,previous=None):
    """Search one personal subscription. Never accept a title across accounts/shows."""
    user=pine._ensure_native()
    pods=pine._request('GET','/api/data/return_pods/'+str(user),native=True)['pods']
    if not isinstance(pods,list) or len(pods)>10000:raise ReviewError('Podcastliste unvollständig.')
    shows=[r for r in pods if r.get('feedurl')==m['feed_url']]
    if not shows:shows=[r for r in pods if r.get('podcastname')==m['show']]
    if len(shows)!=1:return None
    pod=shows[0]; seen=set(); offset=0; total=None; hits=[]
    while True:
        page=pine._request('GET','/api/data/podcast_episodes',native=True,params=dict(user_id=user,
            podcast_id=pod['podcastid'],limit=200,offset=offset,sort_by='date',sort_order='desc',filter='all'))
        rows,n=page['episodes'],page['total']
        if (type(n)!=int or n<0 or n>100000 or total not in (None,n) or not isinstance(rows,list)
                or len(rows)>200 or offset+len(rows)>n or not rows and offset!=n):
            raise ReviewError('Folgenliste unvollständig. Bitte erneut prüfen.')
        total=n
        for r in rows:
            i=r['Episodeid']
            if type(i)!=int or i<=0 or i in seen:raise ReviewError('PinePods liefert mehrdeutige Folgen.')
            seen.add(i)
            if (r.get('Episodeurl') and r.get('Episodetitle')==m['title']
                    and (not previous or i==previous)
                    and (i==previous or r['Episodeurl']==m['enclosure_url'] or pod['feedurl']==m['feed_url'])):hits.append(r)
        offset+=len(rows)
        if offset==n:break
    if len(hits)!=1:return None
    r=hits[0]
    return dict(feed_url=pod['feedurl'],enclosure_url=r['Episodeurl'],native_episode_id=r['Episodeid'],
                native_user_id=user,native_podcast_id=pod['podcastid'])


def route_proof(c,m,route,*,refresh=True,force_source=False):
    """Recheck local file, personal owner, exact native route and retired action namespace."""
    if ((m['feed_url'],m['enclosure_url'])!=(route['feed_url'],route['enclosure_url'])
            and 'old_actions' not in route):
        raise ReviewError('Der Nachweis für die frühere Audio-Adresse fehlt. Bitte die Zuordnung prüfen lassen.')
    dest=dict(m,feed_url=route['feed_url'],enclosure_url=route['enclosure_url'])
    n=c.pine.inspect_episode_metadata(dest['feed_url'],dest['enclosure_url'],m['duration'])
    identity=native_binding(n,dest,c.pine.identity)
    if (any(identity[k]!=route[k] for k in ('native_episode_id','native_user_id','native_podcast_id'))
            or n['metadata'].get('episodetitle')!=m['title']):
        raise ReviewError('Der persönliche PinePods-Eintrag passt nicht mehr zur Folge.')
    source=VariantProof(c.b).source_snapshot(m,allow_cached=not refresh and not force_source,identity=identity)
    d=n['duration']
    if type(d) not in (int,float) or not math.isfinite(d) or not 1<=d<=172800:
        raise ReviewError('PinePods meldet keine verwendbare Laufzeit.')
    if 'old_actions' in route and source_history_evidence(m,c.pine.actions(m['feed_url'])['actions'])!=route['old_actions']:
        raise ReviewError('Unter der früheren Audio-Adresse hat sich der Hörstand geändert. Bitte erneut prüfen.')
    binding=fingerprint([source,identity]);version=fingerprint([binding,d])
    return dict(ready=True,fresh=refresh,validated_at=c.b.clock(),source_fingerprint=binding,proof_fingerprint=version,
                evidence_sha256=version,own_duration=d,source_snapshot=source,native_identity=identity,
                **{k:identity[k] for k in ('feed_url','enclosure_url','native_episode_id','native_user_id')})


def check_route_available(b,key,mappings,route):
    if any(x['id']!=key and (
            (x.get('feed_url'),x.get('enclosure_url'))==(route['feed_url'],route['enclosure_url'])
            or (b.load(x['id']).get('pine_raw') or {}).get('native_episode_id')==route['native_episode_id']
            or (b.load(state_key(x['id'])).get('raw',{}).get('pinepods') or {}).get('native_episode_id')==route['native_episode_id']
            or (b.load(PREFIX+x['id']).get('route') or {}).get('native_episode_id')==route['native_episode_id']) for x in mappings):
        raise ReviewError('Dieser PinePods-Eintrag ist bereits einer anderen lokalen Folge zugeordnet.')


def check_saved_identity(prior,proof):
    """New records also retain readable identity evidence. Legacy hashes cannot supply it."""
    old=prior.get('source_snapshot')
    if old and any(old.get(k)!=proof['source_snapshot'].get(k) for k in ('mapping','jellyfin')):
        raise ReviewError('Die frühere lokale Zuordnung stimmt nicht mehr überein.')
    old=prior.get('native_identity')
    if old and old!=proof['native_identity']:
        raise ReviewError('Die früher bestätigte PinePods-Identität oder GUID hat sich geändert.')


def check_review_accounts(c):
    # account_guard rejects changed entries. A missing historical registry is
    # also insufficient evidence when replacing a previous identity approval.
    if set(c.accounts)-set(c.b.load('_podcast_accounts')):
        raise ReviewError('Die früheren Dienstkonten sind nicht vollständig bestätigt.')


def recheck_measured(c,m,core,status,prior,cache):
    """An explicit catalog approval is separate from renewing a measurement.

    Validate the old complete receipt against its own historical source first.
    No old evidence is attached to the new file and no media probe is launched.
    """
    checker=VariantProof(c.b);sc=StatusCoordinator(c)
    try:
        binding=cache['binding'];old_source=binding['source'];old_native=binding['native']
        if checker._cache(cache,old_source,c.b.clock()) is None:raise ValueError()
        old_proof=checker._response(cache)
        if (status.get('binding') and (sc.binding(m,old_proof)!=status['binding']
                or version_receipt(old_proof)!=status.get('version'))):raise ValueError()
        if prior and (prior.get('mode')!='measured' or prior.get('binding')!=sc.binding(m,old_proof)
                or prior.get('version')!=version_receipt(old_proof)):raise ValueError()
        route={k:old_native[k] for k in ('feed_url','enclosure_url','native_episode_id','native_user_id','native_podcast_id')}
        if (route['feed_url'],route['enclosure_url'])!=(m['feed_url'],m['enclosure_url']):raise ValueError()
        proof=route_proof(c,m,route)
        if proof['native_identity']!=old_native:raise ValueError()
        if any(old_source.get(k)!=proof['source_snapshot'].get(k) for k in ('mapping','jellyfin')):raise ValueError()
        # The normal reported-proof verifier requires a real catalog variant.
        if abs(proof['own_duration']-m['duration'])<=2:raise ValueError()
        old=core.get('pine_raw') or {}
        if any(k in old and old[k]!=route[k] for k in ('native_episode_id','native_user_id')):raise ValueError()
        return proof,route
    except (KeyError,TypeError,ValueError):
        raise ReviewError('Der frühere Messnachweis oder die persönliche PinePods-Identität ist nicht eindeutig bestätigt.') from None


def review(b,key,*,record=None,source=None):
    if not isinstance(key,str) or not key.startswith('_podcast:') or len(key)>1200:raise ReviewError('Ungültige Folge.')
    guard(b);c=PodcastCoordinator(b);c.account_guard(False);check_pending(b,key)
    mappings,_=c.mappings();m=next((x for x in mappings if x['id']==key),None)
    if not m:raise ReviewError('Keine eindeutige lokale Folge in Audiobookshelf und Jellyfin.')
    if m['jf']['id'] in c.jf.active_ids():
        raise ReviewError('Die Folge läuft gerade in Jellyfin. Nach der Wiedergabe erneut prüfen.')
    core=b.load(key);ss=b.load(state_key(key));prior=b.load(PREFIX+key)
    local=fingerprint([m['fingerprint'],m['jf']['fingerprint']])
    if core.get('mapping',local)!=local:raise ReviewError('Die lokale Datei hat sich geändert.')
    route=prior.get('route');proof=None;sc=StatusCoordinator(c);needs_identity_review=False;measured_recheck=False
    cache_key='_pine_variant_proof:'+key
    cache=b.load(cache_key) if not prior or prior.get('mode')=='measured' else {}
    if (prior.get('mode')=='personal_route' or prior.get('mode')=='reported'
            and prior.get('source_snapshot') and prior.get('native_identity')):
        check_review_accounts(c)
        if prior['mode']=='reported':
            native=prior['native_identity']
            try:route={k:native[k] for k in ('feed_url','enclosure_url','native_episode_id','native_user_id','native_podcast_id')}
            except (KeyError,TypeError):raise ReviewError('Die frühere Katalogfreigabe ist unvollständig.') from None
        if (core.get('mapping')!=local or not ss.get('binding') or prior.get('binding')!=ss['binding']
                or prior.get('version')!=ss.get('version') or not isinstance(route,dict)):
            raise ReviewError('Die frühere Freigabe ist unvollständig. Bitte die Zuordnung prüfen lassen.')
        check_route_available(b,key,mappings,route)
        # This is an explicit, read-only preview. Normal sync still rejects a
        # changed opaque binding; only a fresh user approval can replace it.
        proof=route_proof(c,m,route)
        check_saved_identity(prior,proof)
        if prior['mode']=='reported' and abs(proof['own_duration']-m['duration'])<=2:
            raise ReviewError('Die frühere abweichende Kataloglaufzeit ist nicht mehr bestätigt. Bitte die Audiofassung prüfen lassen.')
        needs_identity_review=(sc.binding(m,proof)!=ss['binding'] or version_receipt(proof)!=ss['version'])
    elif cache.get('stage')=='verified' and (cache.get('binding') or {}).get('source')!=VariantProof(b)._source(m):
        check_review_accounts(c)
        if ss.get('binding') and core.get('mapping')!=local:
            raise ReviewError('Die frühere lokale Zuordnung ist nicht vollständig bestätigt.')
        proof,route=recheck_measured(c,m,core,ss,prior,cache)
        check_route_available(b,key,mappings,route)
        needs_identity_review=True;measured_recheck=True
    elif ss.get('binding'):
        proof=c.variant_proof(m,refresh=True,apply=False)
        if not proof.get('ready') or sc.binding(m,proof)!=ss['binding']:raise ReviewError('Frühere Zuordnung nicht bestätigt.')
    elif c.pine and not m.get('_pine_unavailable') and c.alias_record(m) is None:
        try:
            c.pine.read_episode(m['feed_url'],m['enclosure_url'],m['duration'])
        except EpisodeUnavailable as exc:
            if exc.reason not in ('duration_mismatch','feed_missing','episode_missing','episode_identity_unresolved','media_identity_mismatch'):raise
            old=(core.get('pine_raw') or {}).get('native_episode_id')
            route=candidate(c.pine,m,old)
            if route:
                check_route_available(b,key,mappings,route)
                if (m['feed_url'],m['enclosure_url'])!=(route['feed_url'],route['enclosure_url']):
                    route['old_actions']=source_history_evidence(m,c.pine.actions(m['feed_url'])['actions'])
                proof=route_proof(c,m,route)
            elif old or has_pine_history(core):raise ReviewError('Der bisherige persönliche PinePods-Eintrag ist nicht eindeutig auffindbar.')
    if proof:
        m['_pine_selected']=False
        states,raw=sc.read(m,proof)
    else:
        states,raw=c.select_participants(m,core,False)
        if m.get('_variant_selected'):raise ReviewError('Die Audiofassung benötigt zunächst eine Laufzeitfreigabe.')
    native=raw.get('pinepods') or {}
    ident=native.get('native_episode_id')
    scope=' · '.join({'abs':'Audiobookshelf','jf':'Jellyfin','pinepods':'PinePods','gpodder':'GPodder'}[k] for k in states)
    explanation=('Freigabe für den persönlichen PinePods-Eintrag #'+str(ident)+'. Bei unterschiedlichen Fassungen wird nur der Hörstatus übertragen.' if proof else
        'Die Auswahl gilt für '+scope+'.'+(' PinePods ist noch nicht zugeordnet und wird nicht verändert.' if not ident else ''))
    for_peer={k:v['status'] for k,v in states.items()}
    previous=ss.get('baseline') or core.get('baseline',{})
    if any(v.get('status')!='not_started' and not v.get('missing') and states[k].get('missing')
           for k,v in previous.items() if k in states):
        raise ReviewError('Ein früher vorhandener Hörstand ist verschwunden. Bitte den betroffenen Dienst prüfen.')
    if needs_identity_review:
        explanation=('Die frühere Freigabe passt nicht mehr zum aktuellen Dateinachweis. '
            'Persönlicher PinePods-Eintrag, Konto, Audio-Adresse und lokale Zuordnung wurden erneut geprüft. '
            +('Die alte Freigabe enthält keinen lesbaren Quellen-/GUID-Nachweis; ihre frühere Gleichheit ist daher nicht vollständig belegbar. '
              if not prior.get('source_snapshot') or not prior.get('native_identity') else '')
            +'Bestätige diese Zuordnung bewusst neu und wähle den gültigen Hörstatus. Die alte Freigabe bleibt im Prüfverlauf erhalten. '
            'Es wird nur der Hörstatus zwischen den Fassungen abgeglichen.')
        if measured_recheck:
            explanation=('Der lokale Dateinachweis der früher gemessenen Fassung hat sich geändert. '
                'Die gespeicherte Messung und die unveränderte persönliche PinePods-Identität wurden geprüft. '
                'Mit dieser Neuprüfung bestätigst du ausdrücklich die aktuell von PinePods gemeldete Kataloglaufzeit; '
                'sie ist keine neue Messung der Audiodatei. Die alte Messung bleibt unverändert im Prüfverlauf. '
                'Wähle den gültigen Hörstatus. Zwischen den Fassungen wird nur der Hörstatus übertragen.')
    sig=fingerprint([key,local,c.accounts,m.get('abs_guid'),route,version_receipt(proof) if proof else None,raw,
                     {k:v for k,v in core.items() if k!='last_error'},ss,prior,needs_identity_review,cache])
    public=dict(editable=True,episode_id=key,title=m['title'],show=m['show'],duration=m['duration'],pine_id=ident,
                pine_duration=proof['own_duration'] if proof else (m['duration'] if ident else None),
                states=for_peer,missing=[k for k,v in states.items() if v.get('missing')],sources=[k for k in states if not states[k].get('missing')],
                digest=sig,explanation=explanation,needs_identity_review=needs_identity_review,preview_at=b.clock())
    if record is None:return public
    if record.get('digest')!=sig:raise ReviewError('Die Zuordnung oder der Hörstand hat sich geändert. Bitte erneut prüfen.')
    if needs_identity_review and (type(record.get('preview_at')) not in (int,float)
            or not 0<=b.clock()-record['preview_at']<=PREVIEW_AGE):
        raise ReviewError('Die Neuprüfung ist abgelaufen. Bitte erneut prüfen.')
    if source not in TARGETS and source not in public['sources']:raise ReviewError('Bitte einen Hörstatus auswählen.')
    target=source if source in TARGETS else for_peer[source]
    if not proof and target=='in_progress' and source not in states:raise ReviewError('Bitte eine Quelle wählen.')
    if proof:
        binding=sc.binding(m,proof)
        mode='personal_route' if route else 'measured'
        note=dict(prior or dict(mode=mode,previous_core=core,route=route),
                  approved_by='user',binding=binding,version=version_receipt(proof))
        if measured_recheck:note.update(mode='reported',route=None)
        if proof.get('source_snapshot') is not None:
            note.update(source_snapshot=copy.deepcopy(proof['source_snapshot']),native_identity=copy.deepcopy(proof['native_identity']))
        elif cache.get('source_fingerprint')==proof.get('source_fingerprint') and version_receipt(cache)==version_receipt(proof):
            note.update(source_snapshot=copy.deepcopy(cache['binding']['source']),native_identity=copy.deepcopy(cache['binding']['native']))
        updated=dict(schema=2,binding=binding,version=version_receipt(proof),baseline=states,raw=raw,enrolled=True)
        if any(v!=target for v in for_peer.values()):
            updated['reviewed_source']=dict(target=target,snapshot=fingerprint(raw))
        changes={PREFIX+key:note,state_key(key):updated,key:dict(mapping=local,baseline={k:states[k] for k in ('abs','jf')},pine_raw=None)}
        if needs_identity_review:
            audit_key=AUDIT_PREFIX+fingerprint([key,prior,cache])
            changes[audit_key]=dict(schema=1,episode_id=key,previous_approval=prior,
                previous_status_binding=ss.get('binding'),previous_status_version=ss.get('version'),
                next_binding=binding,next_version=version_receipt(proof),reviewed_at=b.clock())
            if measured_recheck:changes[audit_key]['previous_variant_proof']=copy.deepcopy(cache)
            note['previous_approval_audit']=audit_key
    else:
        goal=dict(status=target,position=m['duration'] if target=='finished' else 0 if target=='not_started' else states[source]['position'])
        changes={key:dict(core,manual_choice=dict(goal=goal,snapshot=fingerprint(raw)))}
    if m['jf']['id'] in c.jf.active_ids():
        raise ReviewError('Die Folge läuft inzwischen in Jellyfin. Nach der Wiedergabe erneut prüfen.')
    with b.db:
        b.db.execute('BEGIN IMMEDIATE');check_pending(b,key)
        if (core,ss,prior)!=(b.load(key),b.load(state_key(key)),b.load(PREFIX+key)):raise ReviewError('Hörstand inzwischen geändert.')
        if cache and b.load(cache_key)!=cache:raise ReviewError('Der Messnachweis wurde inzwischen geändert. Bitte erneut prüfen.')
        for k,v in changes.items():
            if k.startswith(AUDIT_PREFIX):
                if b.load(k):raise ReviewError('Diese frühere Freigabe wurde bereits geprüft. Bitte neu laden.')
                b.db.execute('INSERT INTO state(id,value) VALUES(?,?)',(k,encode(v)))
            else:b.db.execute('INSERT OR REPLACE INTO state(id,value) VALUES(?,?)',(k,encode(v)))
    return public
