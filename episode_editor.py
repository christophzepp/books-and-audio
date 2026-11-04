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


def route_proof(c,m,route):
    """Recheck local file, personal owner, exact native route and retired action namespace."""
    source=VariantProof(c.b)._source(m)
    dest=dict(m,feed_url=route['feed_url'],enclosure_url=route['enclosure_url'])
    n=c.pine.inspect_episode_metadata(dest['feed_url'],dest['enclosure_url'],m['duration'])
    identity=native_binding(n,dest,c.pine.identity)
    if (any(identity[k]!=route[k] for k in ('native_episode_id','native_user_id','native_podcast_id'))
            or n['metadata'].get('episodetitle')!=m['title']):
        raise ReviewError('Der persönliche PinePods-Eintrag passt nicht mehr zur Folge.')
    d=n['duration']
    if type(d) not in (int,float) or not math.isfinite(d) or not 1<=d<=172800:
        raise ReviewError('PinePods meldet keine verwendbare Laufzeit.')
    if 'old_actions' in route and source_history_evidence(m,c.pine.actions(m['feed_url'])['actions'])!=route['old_actions']:
        raise ReviewError('Unter der früheren Audio-Adresse hat sich der Hörstand geändert. Bitte erneut prüfen.')
    binding=fingerprint([source,identity]);version=fingerprint([binding,d])
    return dict(ready=True,fresh=True,validated_at=c.b.clock(),source_fingerprint=binding,proof_fingerprint=version,
                evidence_sha256=version,own_duration=d,**{k:identity[k] for k in ('feed_url','enclosure_url','native_episode_id','native_user_id')})


def review(b,key,*,record=None,source=None):
    if not isinstance(key,str) or not key.startswith('_podcast:') or len(key)>1200:raise ReviewError('Ungültige Folge.')
    guard(b);c=PodcastCoordinator(b);c.account_guard(False);check_pending(b,key)
    mappings,_=c.mappings();m=next((x for x in mappings if x['id']==key),None)
    if not m:raise ReviewError('Keine eindeutige lokale Folge in Audiobookshelf und Jellyfin.')
    core=b.load(key);ss=b.load(state_key(key));prior=b.load(PREFIX+key)
    local=fingerprint([m['fingerprint'],m['jf']['fingerprint']])
    if core.get('mapping',local)!=local:raise ReviewError('Die lokale Datei hat sich geändert.')
    route=prior.get('route');proof=None;sc=StatusCoordinator(c)
    if ss.get('binding'):
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
                if any(x['id']!=key and (
                    (x.get('feed_url'),x.get('enclosure_url'))==(route['feed_url'],route['enclosure_url'])
                    or (b.load(x['id']).get('pine_raw') or {}).get('native_episode_id')==route['native_episode_id']
                    or (b.load(PREFIX+x['id']).get('route') or {}).get('native_episode_id')==route['native_episode_id']) for x in mappings):
                    raise ReviewError('Dieser PinePods-Eintrag ist bereits einer anderen lokalen Folge zugeordnet.')
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
    sig=fingerprint([key,local,c.accounts,m.get('abs_guid'),route,version_receipt(proof) if proof else None,raw,
                     core.get('baseline'),ss.get('binding'),prior,core.get('manual_choice'),ss.get('reviewed_source')])
    public=dict(editable=True,episode_id=key,title=m['title'],show=m['show'],duration=m['duration'],pine_id=ident,
                pine_duration=proof['own_duration'] if proof else (m['duration'] if ident else None),
                states=for_peer,missing=[k for k,v in states.items() if v.get('missing')],sources=[k for k in states if not states[k].get('missing')],
                digest=sig,explanation=explanation)
    if record is None:return public
    if record.get('digest')!=sig:raise ReviewError('Die Zuordnung oder der Hörstand hat sich geändert. Bitte erneut prüfen.')
    if source not in TARGETS and source not in public['sources']:raise ReviewError('Bitte einen Hörstatus auswählen.')
    target=source if source in TARGETS else for_peer[source]
    if not proof and target=='in_progress' and source not in states:raise ReviewError('Bitte eine Quelle wählen.')
    if proof:
        binding=sc.binding(m,proof)
        note=prior or dict(mode='personal_route',approved_by='user',binding=binding,version=version_receipt(proof),previous_core=core,route=route)
        updated=dict(schema=2,binding=binding,version=version_receipt(proof),baseline=states,raw=raw,enrolled=True)
        if any(v!=target for v in for_peer.values()):
            updated['reviewed_source']=dict(target=target,snapshot=fingerprint(raw))
        changes={PREFIX+key:note,state_key(key):updated,key:dict(mapping=local,baseline={k:states[k] for k in ('abs','jf')},pine_raw=None)}
    else:
        goal=dict(status=target,position=m['duration'] if target=='finished' else 0 if target=='not_started' else states[source]['position'])
        changes={key:dict(core,manual_choice=dict(goal=goal,snapshot=fingerprint(raw)))}
    with b.db:
        b.db.execute('BEGIN IMMEDIATE');check_pending(b,key)
        if (core,ss,prior)!=(b.load(key),b.load(state_key(key)),b.load(PREFIX+key)):raise ReviewError('Hörstand inzwischen geändert.')
        for k,v in changes.items():b.db.execute('INSERT OR REPLACE INTO state(id,value) VALUES(?,?)',(k,encode(v)))
    return public
