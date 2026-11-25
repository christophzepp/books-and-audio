"""Explicitly retire missing book pairs inside the active account's database."""
import json

from bookorbit_scope import signature
from admin_store import stamp


class CleanupError(ValueError):
    pass


def overlay(config, db):
    """Mask exactly the retired seeds, even if the host/encrypted config still contains them."""
    if config.get('mapping_provider', 'bookorbit') != 'bookorbit':
        return config
    rows = db.execute("SELECT media,local_id,seed,pin,pause FROM retired_book_seeds").fetchall()
    if not rows:
        return config
    result = dict(config)
    keys = {(kind,key,digest) for kind,key,digest,_,_ in rows}
    result['mappings'] = [m for m in config.get('mappings', [])
                          if ('audio',m['abs_id'],signature(m)) not in keys]
    jf = dict(config.get('jellyfin', {}))
    jf['ebooks'] = [m for m in jf.get('ebooks', [])
                    if ('ebook',str(m['file_id']),signature(m)) not in keys]
    if 'jellyfin' in config:
        result['jellyfin'] = jf
    for field, index in [('native_readalong', 3), ('paused_mappings', 4)]:
        if field in config:
            retired = {(r[1], r[index]) for r in rows if r[0] == 'audio'}
            result[field] = {k:v for k,v in config[field].items() if (k,signature(v)) not in retired}
    return result


DEPENDENCIES = ('mapping_revision_overrides', 'mapping_revision_history', 'mapping_revision_evidence',
                'native_readalong_enrollments', 'native_readalong_observations', 'native_readalong_witnesses')


def state_keys(kind, local_id, provider):
    if kind == 'ebook':
        key = '_jellyfin_ebook:' + local_id
        return [key, '_jellyfin_ebook_error:'+local_id, '_bookorbit_scope_seen:'+key]
    if provider == 'bookbridge':
        return ['_bookbridge_audio:'+local_id, '_bookbridge_error:'+local_id]
    return [local_id, '_jellyfin_audio:'+local_id, '_bookorbit_scope_seen:audio:'+local_id,
            '_bookorbit_scope_seen:audio-jf:'+local_id]


def records(bridge, kind):
    cfg = bridge.config
    if kind == 'audio':
        if bridge.provider == 'bookbridge':
            return [(m,'BookBridge') for m in bridge.load('_bookbridge_mapping_inventory').get('mappings', [])]
        rows = [(m,'Konfiguration') for m in cfg.get('mappings', [])]
        for local,peer,raw in bridge.db.execute('SELECT id,bo_id,value FROM auto_mappings'):
            m = json.loads(raw)
            if m.get('abs_id') != local or m.get('bo_id') != peer:
                raise CleanupError('Eine automatische Hörbuchzuordnung enthält widersprüchliche Kennungen.')
            rows.append((m,'Automatisch'))
        return rows
    if bridge.provider == 'bookbridge':
        return []
    return ([(m,'Konfiguration') for m in cfg.get('jellyfin',{}).get('ebooks',[])] +
            [(m,'Automatisch') for m in bridge.load('_ebook_discovery').get('mappings',[])])


def live_scope(admin, bridge, state, kind):
    """Identify both accounts and libraries independently of their item counts."""
    if bridge is None or state['disabled_reason']:
        raise CleanupError('Bitte zuerst die aktiven Verbindungen prüfen.')
    cfg = bridge.config
    bridge.guard()
    if admin.folders:
        admin.media_guard('audiobooks' if kind == 'audio' else 'ebooks')
    active = {p['kind']:p for p in state['profiles'] if p['active']}
    def expected(name):
        saved = active[name].get('identity') or (admin.identities or {}).get(name)
        if not saved or not saved.get('user_id'):
            raise CleanupError('Die Kontoidentität für '+name+' wurde noch nicht bestätigt.')
        return str(saved['user_id'])

    names = []
    if kind == 'audio':
        lib = cfg['auto_discovery']['abs_library_id']
        me = bridge.abs._request('GET','/api/me','/api/me')
        libs = [r for r in bridge.abs.libraries() if r.get('id')==lib and r.get('mediaType')=='book']
        if str(me.get('id')) != expected('audiobookshelf') or len(libs)!=1:
            raise CleanupError('Das Audiobookshelf-Konto oder die Hörbuchbibliothek stimmt nicht mehr.')
        inventory = bridge.abs.inventory(lib)
        if any(r.get('libraryId')!=lib for r in inventory):
            raise CleanupError('Audiobookshelf hat Einträge einer anderen Bibliothek geliefert.')
        left = {r['id'] for r in inventory}
        names.append('Audiobookshelf · '+str(me.get('username') or me['id'])+' · '+str(libs[0].get('name') or lib))
    else:
        if bridge.provider == 'bookbridge':
            raise CleanupError('BookBridge verwaltet seine E-Book-Zuordnungen selbst.')
        jf = bridge.jellyfin
        jf.guard()
        lib = cfg['jellyfin']['auto_ebooks']['jf_library_id']
        views = jf.request('GET','/Users/'+jf.user+'/Views')
        libs = [r for r in views['Items'] if r.get('Id') == lib]
        if len(libs)!=1 or jf.user != expected('jellyfin'):
            raise CleanupError('Das Jellyfin-Konto oder die E-Book-Bibliothek stimmt nicht mehr.')
        left = {r['Id'] for r in jf.items(parent=lib,types='Book')}
        names.append('Jellyfin · '+jf.username+' · '+str(libs[0].get('Name') or lib))
    if bridge.provider == 'bookbridge':
        pairs = bridge.bookbridge.inventory()
        right = {r['id']:r.get('abs_id') for r in pairs}
        names.append('BookBridge · '+active['bookbridge']['settings']['username'])
    else:
        user = bridge.bo._request('GET','/api/v1/auth/me','/api/v1/auth/me')
        if str(user.get('id')) != expected('bookorbit'):
            raise CleanupError('Das BookOrbit-Konto stimmt nicht mit der letzten Freigabe überein.')
        ids = cfg['auto_discovery']['bo_library_ids'] if kind=='audio' else [cfg['jellyfin']['auto_ebooks']['bo_library_id']]
        if not ids or any(type(i) is not int or i<=0 for i in ids):
            raise CleanupError('Bitte die BookOrbit-Bibliothek festlegen.')
        labels = []
        for ident in ids:
            info = bridge.bo.library(ident)
            if type(info.get('id')) is not int or info['id'] != ident:
                raise CleanupError('BookOrbit hat die gewählte Bibliothek nicht bestätigt.')
            labels.append(str(info.get('name') or ident))
        names.append('BookOrbit · '+str(user.get('username') or user['id'])+' · '+', '.join(labels))
        cards = bridge.bo.query()
        if any(type(r.get('libraryId')) is not int or r['libraryId'] <= 0 for r in cards):
            raise CleanupError('Die Bibliothek eines BookOrbit-Eintrags fehlt.')
        right = {r['id']:r['libraryId'] for r in cards if r['libraryId'] in ids}
    files = {}
    if kind=='ebook':
        for m,_ in records(bridge,kind):
            peer=m['bo_id']
            if peer in files or peer not in right:
                continue
            detail = bridge.bo.detail(peer)
            listed = detail.get('files')
            if (detail.get('id')!=peer or detail.get('libraryId')!=right[peer] or not isinstance(listed,list)
                    or any(type(f.get('id')) is not int or f['id']<=0 for f in listed)):
                raise CleanupError('Die BookOrbit-Dateiliste konnte nicht vollständig geprüft werden.')
            files[peer]={f['id'] for f in listed}
            if len(files[peer])!=len(listed):
                raise CleanupError('BookOrbit hat doppelte Dateikennungen geliefert.')
    return names, left, right, files


def preview(bridge, state, kind, catalog):
    if kind not in ('ebook','audio'):
        raise CleanupError('Bitte Bücher oder Hörbücher auswählen.')
    names, left, right, files = catalog
    rows, seen = {}, set()
    for mapping, source in records(bridge,kind):
        bb = bridge.provider=='bookbridge'
        local = str(mapping['abs_id'] if kind=='audio' else mapping['file_id'])
        if local in seen or not local or (kind=='audio' and local.startswith('_')):
            raise CleanupError('Eine gespeicherte Buchkennung ist mehrdeutig oder ungültig.')
        seen.add(local)
        peer = mapping['bb_id' if bb else 'bo_id']
        missing = []
        if (mapping['abs_id'] if kind=='audio' else mapping['jf_id']) not in left:
            missing.append('Audiobookshelf' if kind=='audio' else 'Jellyfin')
        if peer not in right or bb and right[peer]!=mapping['abs_id']:
            missing.append('BookBridge' if bb else 'BookOrbit')
        elif kind=='ebook' and mapping['file_id'] not in files[peer]:
            missing.append('BookOrbit')
        if not missing:
            continue
        keys = state_keys(kind,local,bridge.provider)
        pending = any(bridge.load(k).get('pending') or bridge.db.execute(
            "SELECT 1 FROM operations WHERE book=? AND status='pending' LIMIT 1",(k,)).fetchone() for k in keys)
        approvals = []
        proof = {}
        if kind == 'audio' and not bb:
            for field,label in [('native_readalong','Mitlese-Freigabe'),('paused_mappings','Titelsperre')]:
                if local in bridge.config.get(field,{}):
                    proof[field] = bridge.config[field][local]
                    approvals.append(label)
            for table in DEPENDENCIES:
                evidence = bridge.db.execute('SELECT * FROM '+table+' WHERE id=? ORDER BY rowid',(local,)).fetchall()
                if evidence:
                    proof[table] = evidence
            if 'native_readalong_enrollments' in proof:
                approvals.append('Bestätigte Mitlese-Zuordnung')
            if 'mapping_revision_overrides' in proof:
                approvals.append('Bestätigte Dateirevision')
        key = signature([kind,bridge.provider,local,peer])[:32]
        rows[key] = dict(id=key, local_id=local, title=mapping.get('title') or local, source=source,
                         missing=missing, approvals=approvals, pending=bool(pending),
                         signature=signature([mapping,proof]))
    return dict(kind=kind,revision=state['revision'],scope=names,provider=bridge.provider,
                database=bridge.config.get('_bookorbit_storage'),rows=rows)


def retire(bridge, kind, ids):
    """Caller holds the book DB write lock; no fs/config/remote mutations."""
    db, cfg = bridge.db, bridge.config
    bb = bridge.provider=='bookbridge'
    stored = bridge.load('_ebook_discovery' if kind=='ebook' else '_bookbridge_mapping_inventory')
    for mapping, source in records(bridge,kind):
        local = str(mapping['abs_id'] if kind=='audio' else mapping['file_id'])
        if local not in ids:
            continue
        for key in state_keys(kind,local,bridge.provider):
            db.execute('DELETE FROM state WHERE id=?',(key,))
        if kind=='audio' and not bb:
            for table in DEPENDENCIES:
                db.execute('DELETE FROM '+table+' WHERE id=?',(local,))
            db.execute('DELETE FROM auto_mappings WHERE id=? AND bo_id=?',(local,mapping['bo_id']))
        if bb:
            db.execute('INSERT OR IGNORE INTO retired_book_seeds VALUES(?,?,?,?,?)',
                       ('bookbridge',local,signature(mapping['pair']),'',''))
        else:
            db.execute('INSERT OR IGNORE INTO retired_book_seeds VALUES(?,?,?,?,?)',
                       (kind,local,signature(mapping) if source=='Konfiguration' else '',
                        signature(cfg['native_readalong'][local]) if kind=='audio' and local in cfg.get('native_readalong',{}) else '',
                        signature(cfg['paused_mappings'][local]) if kind=='audio' and local in cfg.get('paused_mappings',{}) else ''))
        db.execute('INSERT INTO events(at,book,kind,detail) VALUES(?,?,?,?)',
                   (stamp(),local,'book_retired',json.dumps(dict(title=mapping.get('title'),media=kind))))
    if kind=='ebook' or bb:
        stored['mappings']=[m for m in stored.get('mappings',[])
                            if str(m['file_id'] if kind=='ebook' else m['abs_id']) not in ids]
        db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',
                   ('_ebook_discovery' if kind=='ebook' else '_bookbridge_mapping_inventory',json.dumps(stored)))


def process(admin, bridge, kind, payload, command):
    try:
        if kind=='library.preview_missing':
            if set(payload)!={'media'} or payload['media'] not in ('ebook','audio'):
                raise CleanupError('Ungültiger Prüfauftrag.')
            state,_ = admin.manager._read()
            catalog = live_scope(admin,bridge,state,payload['media'])
            record = preview(bridge,state,payload['media'],catalog)
            public = [{k:v for k,v in row.items() if k!='signature'} for row in record['rows'].values()]
            admin.store.finish(command,result=dict(review_id=command,scope=record['scope'],rows=public),
                               private=record)
            return
        if set(payload)!={'review_id','selected','confirm'} or payload['confirm'] is not True:
            raise CleanupError('Bitte die ausgewählten Titel bestätigen.')
        saved = admin.store.review(payload['review_id'],'library.preview_missing')
        chosen = payload['selected']
        if (not isinstance(chosen,list) or not 1<=len(chosen)<=1000 or
                any(not isinstance(i,str) or i not in saved['rows'] for i in chosen) or len(set(chosen))!=len(chosen)):
            raise CleanupError('Die Auswahl gehört nicht zur angezeigten Prüfung.')
        with admin.manager._locked():
            state,_ = admin.manager._read()
            if state['revision']!=saved['revision']:
                raise CleanupError('Die aktiven Verbindungen haben sich geändert. Bitte erneut prüfen.')
            catalog = live_scope(admin,bridge,state,saved['kind'])
            with bridge.db:
                bridge.db.execute('BEGIN IMMEDIATE')
                current = preview(bridge,state,saved['kind'],catalog)
                if (any(current[k]!=saved[k] for k in ('scope','provider','database'))
                        or any(current['rows'].get(i)!=saved['rows'][i] or saved['rows'][i]['pending'] for i in chosen)):
                    raise CleanupError('Die Auswahl, Freigaben oder laufende Übertragungen haben sich geändert. Bitte erneut prüfen.')
                retire(bridge,saved['kind'],{saved['rows'][i]['local_id'] for i in chosen})
            bridge.config=overlay(bridge.config,bridge.db)
            if bridge.provider=='bookbridge':
                bridge.retired_bookbridge_pairs=set(bridge.db.execute("SELECT local_id,seed FROM retired_book_seeds WHERE media='bookbridge'"))
            bridge.invalidate_mapping_observations()
        admin.store.finish(command,result=dict(retired=len(chosen)))
    except CleanupError as exc:
        admin.store.finish(command,error=str(exc))
    except Exception:
        admin.store.finish(command,error='Konten, Bibliotheken und Kataloge konnten nicht sicher geprüft werden. Bitte erneut prüfen.')
