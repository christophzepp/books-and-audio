"""Reviewed account/library changes. Only the sync worker writes the encrypted connection store."""
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath
import copy
import hashlib
import json
import re
import sqlite3
import time
import uuid

from bo_client import BOClient
from watch_deployment import DeploymentError, RECEIPT_MAX_AGE, bookorbit_instance_policy, bookorbit_policy_digest

APPROVAL = '_bookorbit_scope_approval'


class ScopeError(ValueError):
    pass


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def normalize_scope(user_id, libraries, audio, ebook):
    if (type(user_id) not in (int, str) or not re.fullmatch(r'[1-9][0-9]*', str(user_id))
            or not isinstance(audio, list) or not 1 <= len(audio) <= 100
            or any(type(item) is not int or item <= 0 for item in audio) or len(set(audio)) != len(audio)
            or ebook is not None and (type(ebook) is not int or ebook <= 0)):
        raise ScopeError('Bitte ein gültiges Konto und die IDs der Hörbuch- und E-Book-Bibliotheken wählen.')
    if not isinstance(libraries, list) or {r['id'] for r in libraries} != set(audio) | ({ebook} if ebook else set()):
        raise ScopeError('Die ausgewählten Bibliotheken stimmen nicht mit der Kontoprüfung überein.')
    return dict(user_id=str(user_id), library_ids=sorted(audio), ebook_library_id=ebook,
                libraries=sorted([dict(id=r['id'], roots=sorted(r['roots'])) for r in libraries],key=lambda r:r['id']))


def initial(admin):
    spec = bookorbit_instance_policy(admin.base_config)
    cfg = admin.base_config
    ebooks = cfg.get('jellyfin', {}).get('auto_ebooks', {})
    return normalize_scope(spec['user_id'], spec['libraries'], cfg['auto_discovery']['bo_library_ids'],
                           ebooks.get('bo_library_id') if ebooks.get('enabled') else None)


def public(admin, cfg=None, profile_id=None):
    if 'bookorbit_instance' not in admin.base_config:
        return {'available': False}
    if cfg is None:
        cfg, _, _ = admin.manager.effective()
    approval = cfg.get(APPROVAL)
    try:
        active = approval['scope'] if approval else initial(admin)
    except (ValueError, RuntimeError, TypeError, KeyError):
        return {'available': False}
    return dict(available=True, active=active, profile_id=(approval or {}).get('profile_id',profile_id))


def overlay(admin, cfg):
    result = dict(cfg)
    result.pop('bookorbit_instance', None)
    result.pop('_bookorbit_storage', None)
    result.pop('_bookorbit_activation', None)
    approval = result.pop(APPROVAL, None)
    if 'bookorbit_instance' not in admin.base_config:
        if approval:
            raise ScopeError('Für das gespeicherte Konto fehlt die Freigabe der BookOrbit-Instanz.')
        return result
    spec = copy.deepcopy(admin.base_config['bookorbit_instance'])
    result['bookorbit_instance'] = spec
    if not approval:
        return result
    if result.get('mapping_provider','bookorbit') != 'bookorbit':
        return result
    if approval['infrastructure'] != bookorbit_policy_digest(bookorbit_instance_policy(result)):
        raise ScopeError('Die gespeicherte Kontofreigabe gehört zu einer anderen BookOrbit-Instanz.')
    scope = approval['scope']
    spec.update(user_id=scope['user_id'], libraries=copy.deepcopy(scope['libraries']))
    if scope != initial(admin):
        result['_bookorbit_storage'] = signature([approval['infrastructure'],scope])
        # These materialized mapping approvals belong to the original scope.
        result.update(mappings=[],paused_mappings={},native_readalong={})
        jf = copy.deepcopy(result.get('jellyfin', {}))
        jf['ebooks'] = []
        if 'auto_ebooks' in jf:
            jf['auto_ebooks']['enabled'] = scope['ebook_library_id'] is not None
            if scope['ebook_library_id'] is not None:
                jf['auto_ebooks']['bo_library_id'] = scope['ebook_library_id']
        result['jellyfin'] = jf
    result['_bookorbit_activation'] = approval['activation']
    return result


def database(root, cfg, create=False):
    key = cfg.get('_bookorbit_storage')
    if key is None:
        return Path(root) / 'state.sqlite'
    if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{64}', key):
        raise ScopeError('Ungültige Kennung für den kontogebundenen Buchzustand.')
    directory = Path(root) / 'bookorbit-scopes'
    if create:
        directory.mkdir(mode=0o700,exist_ok=True)
    path = directory / (key+'.sqlite')
    if directory.is_symlink() or path.is_symlink():
        raise ScopeError('Ungültiger Speicherpfad für den Buchzustand.')
    return path


@contextmanager
def podcast_database(bridge):
    original = getattr(bridge, '_shared_podcast_db', None)
    if original is None:
        yield
        return
    book_db, bridge.db = bridge.db, original
    try:
        yield
    finally:
        bridge.db = book_db


@contextmanager
def book_database(bridge):
    if getattr(bridge, '_shared_podcast_db', None) is None:
        yield
        return
    previous, bridge.db = bridge.db, bridge._book_state_db
    try:
        yield
    finally:
        bridge.db = previous


def baseline_needed(bridge, key):
    activation = getattr(bridge, 'config', {}).get('_bookorbit_activation')
    return bool(activation and bridge.load('_bookorbit_scope_seen:'+key).get('activation') != activation)


def initial_agreement(bridge, key, states, equal):
    if not baseline_needed(bridge, key):
        return False
    source, *others = states.values()
    if not all(equal(source, other) for other in others):
        raise ScopeError('Die Hör- oder Lesestände unterscheiden sich beim Kontowechsel. Bitte die Stände in den beteiligten Diensten prüfen.')
    return True


def mark_agreement(bridge, key):
    # The actual baseline was durably written first. A crash between writes
    # therefore only repeats the read-only first comparison after restart.
    bridge.save('_bookorbit_scope_seen:' + key, {'activation': bridge.config['_bookorbit_activation']})


def read_library(client, ident):
    from watch_deployment import _canonical_absolute_path
    library = client.library(ident)
    folders = library.get('folders') if isinstance(library,dict) else None
    if not isinstance(library,dict) or type(library.get('id')) is not int or library['id'] != ident or not isinstance(folders,list) or not 1 <= len(folders) <= 100:
        raise ScopeError('Die erwartete BookOrbit-Bibliothek konnte nicht geprüft werden.')
    paths, ids = set(), set()
    for folder in folders:
        if (not isinstance(folder,dict) or type(folder.get('libraryId')) is not int or folder['libraryId'] != ident
                or type(folder.get('id')) is not int or folder['id'] <= 0 or folder['id'] in ids):
            raise ScopeError('Die Ordnerzuordnung der BookOrbit-Bibliothek ist ungültig.')
        path = str(_canonical_absolute_path(folder.get('path')))
        if path in paths:
            raise ScopeError('Die BookOrbit-Bibliothek enthält doppelte Ordner.')
        paths.add(path)
        ids.add(folder['id'])
    return dict(id=ident, roots=sorted(paths))


def receipt(admin):
    gate = json.loads((admin.root/'deployment.json').read_text())
    spec = bookorbit_instance_policy(admin.base_config)
    stamp = gate.get('checked_at')
    if (gate.get('ok') is not True or type(stamp) not in (int,float) or not 0 <= time.time()-stamp <= RECEIPT_MAX_AGE):
        raise ScopeError('Die aktuelle BookOrbit-Instanz ist auf dem Server noch nicht bestätigt.')
    from bookorbit_instance import verified_receipt
    return verified_receipt(gate, spec)


def ensure_idle(path):
    if not path.exists():
        return
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        if (db.execute("SELECT 1 FROM operations WHERE status='pending' AND substr(book,1,9)!='_podcast:' LIMIT 1").fetchone()
                or any(json.loads(raw).get('pending') for key,raw in db.execute('SELECT id,value FROM state')
                       if not key.startswith(('_podcast', '_pine')))):
            raise ScopeError('Eine offene Buchübertragung muss vor dem Wechsel abgeschlossen werden.')


def verify_selected_roots(admin, cfg, scope):
    """Require each selected library to include a folder served by our media mount."""
    from folder_manager import default_rows
    from watch_deployment import validate_folder_aliases
    if admin.folders:
        cfg = admin.folders._effective(admin.folders._read()[0], cfg)
    cfg = copy.deepcopy(cfg)
    cfg.setdefault('jellyfin', {}).setdefault('auto_ebooks', {})['enabled'] = scope['ebook_library_id'] is not None
    validate_folder_aliases(cfg, json.loads((admin.root / 'deployment.json').read_text()))
    allowed = {}
    for row in default_rows(cfg):
        path = row.get('paths', {}).get('bo')
        if row.get('active') and path:
            allowed.setdefault(row['kind'], []).append(PurePosixPath(path))
    by_id = {entry['id']: entry['roots'] for entry in scope['libraries']}
    choices = [(ident, 'audiobooks') for ident in scope['library_ids']]
    if scope['ebook_library_id']:
        choices.append((scope['ebook_library_id'], 'ebooks'))
    for ident, kind in choices:
        if not any(PurePosixPath(path).is_relative_to(root)
                   for path in by_id[ident] for root in allowed.get(kind, [])):
            raise ScopeError('Die gewählte Bibliothek enthält keinen der eingerichteten Medienordner für diesen Medientyp.')


def prepare(admin, state, request):
    if (set(request) != {'profile_id','library_ids','ebook_library_id','expected_revision'}
            or type(request['expected_revision']) is not int or request['expected_revision'] != state['revision']):
        raise ScopeError('Die Verbindungen haben sich geändert. Bitte die Vorschau erneut starten.')
    cfg = state['effective_config']
    if cfg.get('mapping_provider','bookorbit') != 'bookorbit':
        raise ScopeError('Für diese Freigabe muss BookOrbit als Zuordnungsdienst gewählt sein.')
    profile = next((p for p in state['profiles'] if p['id']==request['profile_id'] and p['kind']=='bookorbit'), None)
    if not profile:
        raise ScopeError('Das BookOrbit-Verbindungsprofil ist nicht verfügbar.')
    spec = bookorbit_instance_policy(admin.base_config)
    if profile['settings'].get('url','').rstrip('/') != spec['api_origin'].rstrip('/'):
        raise ScopeError('Das Profil gehört nicht zur freigegebenen BookOrbit-Adresse.')
    audio, ebook = request['library_ids'], request['ebook_library_id']
    # Validate types and limits before constructing requests or touching remote IDs.
    if (not isinstance(audio,list) or not 1 <= len(audio) <= 100
            or any(type(i) is not int or i<=0 for i in audio) or len(set(audio))!=len(audio)
            or ebook is not None and (type(ebook) is not int or ebook<=0)):
        raise ScopeError('Bitte gültige Bibliotheks-IDs angeben.')
    if ebook is not None and not admin.base_config.get('jellyfin',{}).get('auto_ebooks'):
        raise ScopeError('Für E-Books muss zuerst die zugehörige Jellyfin-Bibliothek eingerichtet sein.')
    before = receipt(admin)
    client = BOClient(profile['settings']['url'],profile['settings']['username'],profile['secrets']['password'])
    try:
        user = client._request('GET','/api/v1/auth/me','/api/v1/auth/me')
        if not isinstance(user,dict) or not isinstance(client.user,dict) or str(client.user.get('id')) != str(user.get('id')):
            raise ScopeError('Der angemeldete BookOrbit-Nutzer konnte nicht zweifelsfrei bestätigt werden.')
        libraries = [read_library(client,i) for i in sorted(set(audio) | ({ebook} if ebook else set()))]
    finally:
        client.close()
    proposed = normalize_scope(user.get('id'),libraries,audio,ebook)
    verify_selected_roots(admin, cfg, proposed)
    current = public(admin,cfg)['active']
    if receipt(admin)!=before:
        raise ScopeError('Die BookOrbit-Instanz hat sich während der Prüfung geändert.')
    return dict(previous=current,proposed=proposed,request=request,instance=before)


def process(admin, kind, payload, command):
    try:
        if kind=='bookorbit.preview_scope':
            state, _ = admin.manager._read()
            record = prepare(admin,state,payload)
            digest = signature(record)
            admin.store.finish(command,result=dict(review_id=command,digest=digest,
                previous=record['previous'],proposed=record['proposed']),private=dict(record=record,expires=time.time()+300))
            return
        if set(payload)!={'review_id','digest','confirm'} or payload['confirm'] is not True:
            raise ScopeError('Bitte den geprüften Kontowechsel ausdrücklich bestätigen.')
        saved = admin.store.review(payload['review_id'],'bookorbit.preview_scope')
        if time.time()>saved['expires'] or signature(saved['record'])!=payload['digest']:
            raise ScopeError('Die Vorschau ist abgelaufen oder hat sich geändert.')
        request = saved['record']['request']
        with admin.manager._locked():
            state, raw = admin.manager._read()
            record = prepare(admin,state,request)
            if record!=saved['record'] or time.time()>saved['expires']:
                raise ScopeError('Konto, Bibliotheken oder Instanz haben sich geändert. Bitte erneut prüfen.')
            old_path=database(admin.root,overlay(admin,state['effective_config']))
            ensure_idle(old_path)
            target = record['proposed']
            infrastructure = record['instance']['profile_digest']
            new_path=admin.root/'state.sqlite' if target==initial(admin) else database(admin.root,{'_bookorbit_storage':signature([infrastructure,target])})
            ensure_idle(new_path)
            profile_id=request['profile_id']
            for profile in state['profiles']:
                if profile['kind']=='bookorbit':
                    profile['active']=profile['id']==profile_id
                    if profile['active']:
                        profile['settings']['library_ids']=target['library_ids']
                        profile['identity']={'ok':True,'user_id':target['user_id'],'username':profile['settings']['username'],
                                             'library_ids':target['library_ids']}
            state['base_config'][APPROVAL]=dict(scope=target,activation=uuid.uuid4().hex,approved_at=time.time(),
                                                 infrastructure=infrastructure,profile_id=profile_id)
            admin.manager._materialize(state)
            state['revision']+=1
            admin.manager._write(state,raw)
        admin.store.finish(command,result={'approved':True,'scope':target})
    except ScopeError as exc:
        admin.store.finish(command,error=str(exc))
    except DeploymentError:
        admin.store.finish(command, error='Die aktuelle Instanz oder der Medienordner ist auf dem Server nicht bestätigt.')
