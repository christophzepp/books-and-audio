"""Review differing first positions inside the normal, journalled book coordinators."""

import re
import time
import uuid
from datetime import datetime

from bookorbit_scope import ScopeError, initial_agreement, signature


class InitialConflict(ScopeError):
    """The mapping is verified and only the first progress value needs a choice."""


class ReviewError(ValueError):
    pass


class PreviewReady(Exception):
    def __init__(self, result):
        self.result = result


def valid(kind, data):
    if not isinstance(data, dict):
        return False
    if kind == 'bookstart.preview':
        ident = data.get('id')
        return (set(data) == {'media', 'id'} and isinstance(ident, str) and 0 < len(ident) <= 256
                and ident.strip() == ident and ident.isprintable()
                and (data['media'] == 'audiobook'
                     or data['media'] == 'ebook' and re.fullmatch(r'ebook:[1-9][0-9]{0,17}', ident) is not None))
    if kind != 'bookstart.apply' or set(data) != {'review_id','digest','confirm','source'}:
        return False
    try:
        return (data['confirm'] is True and data['source'] in ('abs','bo','jf')
                and isinstance(data['digest'],str) and re.fullmatch('[a-f0-9]{64}',data['digest']) is not None
                and str(uuid.UUID(data['review_id'])) == data['review_id'])
    except (AttributeError, TypeError, ValueError):
        return False


def source(bridge, key, states, baseline, equal, ordinary, *, review=None, evidence=None, total=None, restrictions=None):
    """Offer exactly the cases where ordinary synchronization blocks on a first comparison."""
    try:
        first = False if getattr(bridge,'provider',None) == 'bookbridge' else initial_agreement(bridge, key, states, equal)
        chosen = ordinary()
    except ScopeError as exc:
        reason = str(exc)
    except ValueError as exc:
        if baseline or str(exc) not in ('initial_progress_disagrees', 'Concurrent progress conflict'):
            raise
        reason = 'Die ersten Fortschrittsstände unterscheiden sich. Bitte den Ausgangsstand auswählen.'
    else:
        if review is not None:
            raise ReviewError('Für diesen Titel liegt kein offener Erststand mehr vor.')
        return chosen, first

    if baseline and (set(baseline) != set(states)
                     or any(baseline[k].get('fingerprint') != s.get('fingerprint') for k,s in states.items())):
        raise ReviewError('Die Identität der Ausgabe hat sich geändert. Bitte zuerst die Zuordnung prüfen.')
    if review is None:
        raise InitialConflict(reason)

    reasons = restrictions() if restrictions else {}
    for name, row in states.items():
        if row['status'] == 'not_started' or row['status'] == 'in_progress' and row['position'] == 0:
            reasons[name] = 'Ein Zurücksetzen auf null ist bei der Erstfreigabe gesperrt.'
    digest = signature([review['request'],review['scope'],key,states,baseline,evidence,reasons])
    if review.get('source') is None:
        public = [dict(source=k,status=s['status'],selectable=k not in reasons,reason=reasons.get(k),
                       position=s['position'] if total is None else round(10*s['position']/total, 3),
                       unit='seconds' if total is None else 'percent') for k,s in states.items()]
        raise PreviewReady(dict(digest=digest, states=public))
    chosen = review['source']
    if (time.time() > review['expires_at'] or digest != review['digest'] or chosen not in states
            or chosen in reasons):
        raise ReviewError('Stände, Quelle oder Zuordnung passen nicht mehr zur Vorschau. Bitte neu prüfen.')
    return chosen, bool(bridge.config.get('_bookorbit_activation'))


def audio_restrictions(bridge, mapping, states, raw, jf_rows=None):
    from sync import AutomaticCompletion, equal, ms_from_date, validate_bo_audio_goal
    reasons = {}
    if 'bo' in states:
        for name in ('abs','jf'):
            row = states.get(name)
            if row and row['status'] == 'in_progress' and not equal(row,states['bo']):
                try:
                    validate_bo_audio_goal(bridge.bo,mapping,row)
                except AutomaticCompletion:
                    reasons[name] = 'BookOrbit würde diesen noch offenen Stand automatisch als abgeschlossen markieren.'
        if states['bo']['status'] == 'finished' and not equal(states['bo'],states['abs']):
            try:
                finished = ms_from_date((raw.get('bo_status') or {}).get('finishedAt'))
            except (AttributeError, ValueError, OverflowError):
                finished = None
            if not finished and not (states['abs']['status'] == 'finished' and (raw.get('abs') or {}).get('finishedAt')):
                reasons['bo'] = 'Das Abschlussdatum für Audiobookshelf fehlt.'
    if jf_rows is not None and states['jf']['status'] == 'finished':
        try:
            dates = [datetime.fromisoformat(r['LastPlayedDate'].replace('Z','+00:00')) for r in jf_rows if r.get('LastPlayedDate')]
        except (AttributeError, ValueError):
            dates = []
        if not dates or any(d.tzinfo is None for d in dates):
            reasons['jf'] = 'Jellyfin liefert kein vollständiges Abschlussdatum.'
    return reasons


def audio_decision(pair, baseline):
    from core import decide
    decision = decide(pair['abs'],pair['bo'],baseline)
    action = decision['action']
    if action == 'conflict':
        raise ValueError(decision['reason'])
    return {'baseline':None,'bo_to_abs':'bo','abs_to_bo':'abs'}[action]


def mapping_for(bridge, request):
    if request['media'] == 'audiobook':
        rows = [m for m in bridge.list_mappings() if m['abs_id'] == request['id']]
    else:
        mappings = (bridge.config.get('jellyfin',{}).get('ebooks',[])
                    + bridge.load('_ebook_discovery').get('mappings',[]))
        rows = [m for m in mappings if 'ebook:'+str(m['file_id']) == request['id']]
    if len(rows) != 1:
        raise ReviewError('Die Ausgabe ist nicht mehr eindeutig zugeordnet.')
    return rows[0]


def verified_scope(admin, bridge, request, mapping):
    """Bind the review to the actual users, provider activation and libraries."""
    from admin_runtime import participant_block
    state, _ = admin.manager._read()
    if (bridge is None or state.get('disabled_reason') or participant_block(admin.root,bridge.config)):
        raise ReviewError('Die beteiligten Buchdienste müssen aktiv und bestätigt sein.')
    if admin.store.paused():
        raise ReviewError('Bitte zuerst die Synchronisation wieder freigeben.')
    cfg = bridge.config
    bridge.guard()
    profiles = {r['kind']:r for r in state['profiles'] if r['active']}
    users = {}
    def verify(kind, user):
        identity = profiles[kind].get('identity') or (admin.identities or {}).get(kind) or {}
        if user is None or not identity.get('user_id') or str(user) != str(identity['user_id']):
            raise ReviewError('Das angemeldete Konto entspricht nicht der Freigabe.')
        users[kind] = [profiles[kind]['id'], str(user)]

    if bridge.provider == 'bookbridge':
        if request['media'] != 'audiobook' or not bridge.jellyfin:
            raise ReviewError('BookBridge benötigt für den Hörbuchabgleich eine aktive Jellyfin-Bibliothek.')
        profile = profiles['bookbridge']
        credentials = profile['settings']
        info = bridge.bookbridge.probe()
        if (info.get('username') != credentials['username'] or info.get('version') != credentials['version']
                or info.get('abs_enabled') is not True or info.get('bookorbit_enabled') is not False
                or info.get('abs_username') != cfg['bookbridge'].get('expected_abs_username')):
            raise ReviewError('Das BookBridge-Profil oder sein Audiobookshelf-Konto hat sich geändert.')
        users['bookbridge'] = [profile['id'],info['username'],info['abs_username']]
    else:
        verify('bookorbit',bridge.bo._request('GET','/api/v1/auth/me','/api/v1/auth/me').get('id'))
        detail = bridge.bo.detail(mapping['bo_id'])
        if (detail.get('id') != mapping['bo_id'] or detail.get('libraryId') != mapping['bo_library_id']):
            raise ReviewError('Die BookOrbit-Ausgabe hat die Bibliothek gewechselt.')
    if bridge.jellyfin:
        bridge.jellyfin.guard()
        verify('jellyfin',bridge.jellyfin.user)
    if request['media'] == 'audiobook':
        verify('audiobookshelf',bridge.abs._request('GET','/api/me','/api/me').get('id'))
        spec = cfg.get('auto_discovery',{})
        item = bridge.abs.item(mapping['abs_id'])
        if (not spec.get('abs_library_id') or item.get('libraryId') != spec['abs_library_id']
                or bridge.provider == 'bookorbit' and mapping['bo_library_id'] not in spec.get('bo_library_ids',[])):
            raise ReviewError('Die Hörbuchbibliothek stimmt nicht mit der Freigabe überein.')
    else:
        spec = cfg.get('jellyfin',{}).get('auto_ebooks',{})
        if not bridge.jellyfin or not spec.get('jf_library_id') or spec.get('bo_library_id') != mapping['bo_library_id']:
            raise ReviewError('Die E-Book-Bibliothek stimmt nicht mit der Freigabe überein.')
        items = bridge.jellyfin.items(parent=spec['jf_library_id'],ids=[mapping['jf_id']],types='Book')
        if len(items) != 1 or items[0]['Id'] != mapping['jf_id']:
            raise ReviewError('Die EPUB-Ausgabe ist nicht mehr in der bestätigten Jellyfin-Bibliothek.')
    return [state['revision'],bridge.provider,users,cfg.get('_bookorbit_storage'),cfg.get('_bookorbit_activation')]


def run(bridge, mapping, review):
    from jellyfin_audio import AudioCoordinator
    from jellyfin_ebook import EbookCoordinator
    from sync import equal
    request = review['request']
    applying = 'source' in review
    if getattr(bridge,'provider',None) == 'bookbridge':
        from provider_runtime import BookBridgeCoordinator, bind_audio
        ident = mapping['abs_id']
        key = '_bookbridge_audio:' + ident
        if (request['media'] != 'audiobook' or not bridge.jellyfin or ident in bridge.config.get('paused_mappings',{})
                or bridge.load(key).get('pending') or bridge.db.execute(
                    "SELECT 1 FROM operations WHERE book=? AND status='pending' LIMIT 1", (key,)).fetchone()):
            raise ReviewError('Diese BookBridge-Zuordnung ist für die Erststandwahl gesperrt.')
        coordinator = BookBridgeCoordinator(bridge)
        coordinator.verify_mapping(mapping)
        inventory = bridge.jellyfin.items(parent=bridge.config['jellyfin']['audio_library_id'],types='AudioBook')
        tracks = bind_audio(mapping,inventory)
        if not tracks:
            raise ReviewError('Die geprüfte Audiofassung fehlt in der Jellyfin-Bibliothek.')
        return coordinator.handle(mapping,tracks,applying,0,review=review)
    if request['media'] == 'ebook':
        if bridge.load('_jellyfin_ebook:'+str(mapping['file_id'])).get('pending'):
            raise ReviewError('Für diese Ausgabe ist eine Übertragung im Journal offen.')
        return EbookCoordinator(bridge).handle(mapping,applying,0,review=review)

    ident = mapping['abs_id']
    bridge.require_active_mapping(ident)
    if bridge.has_pending_transfer(ident):
        raise ReviewError('Für diese Ausgabe ist eine Übertragung im Journal offen.')
    stored = bridge.load(ident)
    bridge.verify_transfer_media(mapping)
    pair,raw = bridge.read_pair(mapping)
    if (raw['bo'] is None and ((stored.get('baseline') or {}).get('bo',{}).get('playback_missing') is False
            or pair['bo']['status'] != 'not_started' and pair['abs']['position'] > 1)):
        raise ReviewError('Die BookOrbit-Audioposition fehlt für diese Ausgabe.')
    if bridge.jellyfin:
        result = AudioCoordinator(bridge,bridge.jellyfin).handle(mapping,pair,raw,applying,0,review=review)
        if result is not None:
            return result
        if bridge.load('_jellyfin_audio:'+ident).get('baseline'):
            raise ReviewError('Der zuvor beteiligte Jellyfin-Titel fehlt im aktuellen Katalog.')
    key = 'audio:'+ident
    baseline = stored.get('baseline')
    chosen,first = source(bridge,key,pair,baseline,equal,lambda: audio_decision(pair,baseline),
                          review=review,evidence=[mapping,raw],restrictions=lambda: audio_restrictions(bridge,mapping,pair,raw))
    direction = 'abs_to_bo' if chosen == 'abs' else 'bo_to_abs'
    final = bridge.apply(mapping,pair,raw,direction,pair)
    bridge.save(ident,{'baseline':final})
    if first:
        from bookorbit_scope import mark_agreement
        mark_agreement(bridge,key)
    return 'synced'


def process(admin, bridge, kind, payload, command):
    try:
        if not valid(kind,payload):
            raise ReviewError('Bitte eine gültige Ausgabe und den geprüften Stand bestätigen.')
        if bridge is None:
            raise ReviewError('Die Buchdienste sind zurzeit nicht verfügbar.')
        if kind == 'bookstart.preview':
            review = dict(request=payload)
        else:
            saved = admin.store.review(payload['review_id'],'bookstart.preview')
            if saved['digest'] != payload['digest'] or time.time() > saved['expires_at']:
                raise ReviewError('Die Freigabe ist abgelaufen. Bitte neu prüfen.')
            review = dict(saved,source=payload['source'])
        request = review['request']
        mapping = mapping_for(bridge,request)
        scope = verified_scope(admin,bridge,request,mapping)
        if 'scope' in review and scope != review['scope']:
            raise ReviewError('Konto oder Verbindung haben sich geändert. Bitte neu prüfen.')
        review['scope'] = scope
        # All API reads for both commands must bypass the previous cycle's response cache.
        batch,bridge._book_observations = getattr(bridge,'_book_observations',None),None
        try:
            result = run(bridge,mapping,review)
        except PreviewReady as ready:
            expires = time.time()+300
            admin.store.finish(command,result=dict(ready.result,title=mapping['title'],media=request['media'],
                review_id=command,expires_at=expires),private=dict(review,digest=ready.result['digest'],expires_at=expires))
            return
        finally:
            if batch is not None:
                batch.invalidate()
            bridge._book_observations = batch
        if result != 'synced':
            raise ReviewError('Der Erststand wurde nicht übertragen. Bitte nach der Wiedergabe erneut prüfen.')
        admin.store.finish(command,result={'applied':True})
    except ReviewError as exc:
        admin.store.finish(command,error=str(exc))
    except Exception:
        admin.store.finish(command,error='Die Ausgabe, ihr Konto oder ihr Fortschritt konnte nicht sicher bestätigt werden. Bitte Journal und Freigaben prüfen.')
