"""Register stable, unambiguous EPUBs from the verified shared ebook mount.

Discovery never alters an existing mapping or writes application progress.
Read-aloud packages and mixed audio/text books are deliberately excluded.
"""
import copy
import hashlib
import math
import re
import json
from pathlib import Path, PurePosixPath
import zipfile
from xml.dom import minidom
from xml.parsers.expat import ExpatError

from epub_locations import EpubLocations
from io_policy import settings as io_settings
from io_metrics import increment


class DiscoveryUnavailable(RuntimeError):
    """A transient, safely described source/deployment failure."""


def relative(path, root):
    p = PurePosixPath(path)
    if not p.is_absolute() or '..' in p.parts:
        raise ValueError('Invalid ebook path')
    try:return p.relative_to(root)
    except ValueError:raise ValueError('EPUB outside configured BookOrbit root') from None


def inspect_epub(path, *, return_index=False):
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('EPUB exceeds automatic mapping limit')
    with zipfile.ZipFile(path) as z:
        if sum(x.file_size for x in z.infolist()) > 128 * 1024 * 1024:
            raise ValueError('EPUB exceeds automatic unpacked limit')
        for name in z.namelist():
            if name.lower().endswith(('.smil', '.mp3', '.m4a', '.m4b', '.ogg', '.opus', '.wav')):
                raise ValueError('Read-along or embedded audio excluded')
            if name.lower().endswith('.opf'):
                raw = z.read(name)
                if len(raw) > 1024 * 1024 or b'<!ENTITY' in raw:
                    raise ValueError('Unsupported EPUB package')
                doc = minidom.parseString(raw)
                if any(e.hasAttribute('media-overlay') or e.getAttribute('media-type').startswith('audio/')
                       for e in doc.getElementsByTagName('*')):
                    raise ValueError('Read-along package excluded')
    e = EpubLocations(path)
    # Verify the conversion at every generated position before admitting a book.
    for i in range(e.total + 1):
        point, _ = e.index_to_bo(i)
        if e.bo_to_index(point) != i or e.ticks_to_index(e.index_to_ticks(i)) != i:
            raise ValueError('EPUB position conversion is not reversible')
    return e if return_index else e.hash


def file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _time(value,now):
    return type(value) in (int,float) and math.isfinite(value) and 0<=value<=now


def _ids(values):
    if values is None:return None
    if not isinstance(values,(list,tuple,set)) or not 1<=len(values)<=1000:
        raise ValueError('invalid_ebook_retry_file_ids')
    result=set()
    for value in values:
        if type(value) is int and value>0:result.add(str(value))
        elif isinstance(value,str) and re.fullmatch(r'[1-9][0-9]*',value):result.add(value)
        else:raise ValueError('invalid_ebook_retry_file_ids')
    return result


def _error(title,reason,*,book_id=None,file_id=None,code='source_unavailable',retry_at=None,
           retry_mode='backoff',cached=False,attempts=0):
    return dict(title=title,reason=reason,book_id=book_id,file_id=file_id,code=code,
                retry_at=retry_at,retry_mode=retry_mode,cached=cached,attempts=attempts)


def _retry(old,binding,now,reason,code,initial,maximum,**identity):
    attempts=old.get('attempts',0) if isinstance(old,dict) and old.get('binding')==binding else 0
    if type(attempts) is not int or not 0<=attempts<=100:attempts=0
    attempts=min(100,attempts+1)
    return dict(schema=3,outcome='retry',binding=binding,attempts=attempts,
                retry_at=now+min(maximum,initial*2**min(attempts-1,20)),reason=reason[:1024],code=code,**identity)


def _deferred(old,binding,now):
    return (isinstance(old,dict) and type(old.get('schema')) is int and old['schema']==3 and old.get('outcome')=='retry'
            and old.get('binding')==binding and type(old.get('retry_at')) in (int,float)
            and math.isfinite(old['retry_at']) and now<old['retry_at']<=now+365*86400
            and type(old.get('attempts')) is int and 1<=old['attempts']<=100
            and isinstance(old.get('reason'),str) and len(old['reason'])<=1024
            and old.get('code') in ('inventory_unavailable','candidate_unavailable'))


def _negative(old,binding,now):
    return (binding is not None and isinstance(old,dict) and type(old.get('schema')) is int
            and old['schema']==3 and old.get('outcome')=='rejected' and old.get('binding')==binding
            and old.get('code')=='epub_content_rejected' and isinstance(old.get('reason'),str)
            and 0<len(old['reason'])<=1024 and _time(old.get('verified_at'),now)
            and _time(old.get('since'),old['verified_at'])
            and isinstance(old.get('signature'),str) and re.fullmatch(r'[0-9a-f]{64}',old['signature']))


def _preserve_errors(report,previous,selected=None):
    """Keep last-known errors for sources outside a retry or incomplete scan."""
    present={(row.get('file_id'),row.get('book_id')) for row in report['blocked']}
    for row in previous.get('blocked',[]) if isinstance(previous,dict) else []:
        key=(row.get('file_id'),row.get('book_id'))
        if key==(None,None) or key in present:continue
        if selected is not None and str(row.get('file_id')) in selected:continue
        report['blocked'].append(dict(row,cached=True));present.add(key)


def discover(bo,jf,spec,existing,previous,now,*,stable_seconds=60,metrics=None,index_cache=None,
             media_guard=None,observation_binding=None,binding_reader=None,force=False,retry_file_ids=None,
             retry_initial_seconds=300,retry_max_seconds=86400):
    """Only changed/new candidates inspect media; stable rejections are durable.

    observation_binding is a fresh account/mount context, never physical proof.
    A missing context disables permanent rejections. Explicit retries still
    require source stability and fresh physical admission checks.
    """
    selected=_ids(retry_file_ids)
    if not _time(now,now):raise ValueError('Invalid ebook discovery clock')
    previous=previous if isinstance(previous,dict) else {}
    obs=copy.deepcopy(previous);additions=[]
    report=dict(enabled=True,scanned=True,candidates=0,waiting=0,added=0,blocked=[],
                cached_rejections=0,retry_deferred=0)
    cards=bo.query();items=jf.items(parent=spec['jf_library_id'],types='Book')
    # A restarted client authenticates on its first inventory request. Bind the
    # verified account after that request, not its pre-login empty user field.
    if binding_reader is not None:observation_binding=binding_reader()
    bypath={}
    for item in items:bypath.setdefault(item.get('Path'),[]).append(item)
    existing_files={str(m['file_id']) for m in existing};existing_books={m['bo_id'] for m in existing}
    existing_jf={m['jf_id'] for m in existing};candidates=[];all_candidate_files=[];seen_files=set();failed_books=set()
    selected_books={row.get('book_id') for row in previous.values() if isinstance(row,dict)
                    and selected is not None and str(row.get('file_id')) in selected}
    card_ids={card['id'] for card in cards if card.get('libraryId')==spec['bo_library_id']}
    for card in cards:
        if card.get('libraryId')!=spec['bo_library_id'] or card['id'] in existing_books:continue
        book_key='book:'+str(card['id']);old=previous.get(book_key,{})
        book_binding=_hash(dict(context=observation_binding,card={k:card.get(k) for k in ('id','libraryId','updatedAt','revision')}))
        forced_book=force if selected is None else card['id'] in selected_books
        if not forced_book and _deferred(old,book_binding,now):
            report['retry_deferred']+=1;failed_books.add(card['id'])
            report['blocked'].append(_error(card.get('title') or 'E-Book '+str(card['id']),old['reason'],
                book_id=card['id'],code=old['code'],retry_at=old['retry_at'],attempts=old['attempts'],cached=True))
            continue
        try:
            detail=bo.detail(card['id'])
            if detail.get('id')!=card['id'] or detail.get('libraryId')!=spec['bo_library_id']:
                raise ValueError('BookOrbit ebook inventory changed')
            files=detail.get('files')
            if not isinstance(files,list):raise ValueError('Incomplete BookOrbit ebook files')
            media=[f for f in files if f.get('format','').lower() not in ('nfo','jpg','jpeg','png','webp')]
            obs.pop(book_key,None)
            if len(media)!=1 or media[0].get('format','').lower()!='epub':continue
            file=media[0]
            if (type(file.get('id')) is not int or file['id']<1
                    or not isinstance(file.get('absolutePath'),str)
                    or type(file.get('sizeBytes')) is not int or file['sizeBytes']<1):
                raise ValueError('Incomplete BookOrbit ebook file')
            key=str(file['id']);seen_files.add(key)
            if key in existing_files:continue
            all_candidate_files.append(file)
            if selected is not None and key not in selected:continue
            candidates.append((detail,file))
        except Exception as exc:
            failed_books.add(card['id']);reason=str(exc) if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__
            row=_retry(old,book_binding,now,reason,'inventory_unavailable',retry_initial_seconds,retry_max_seconds,book_id=card['id'])
            obs[book_key]=row
            report['blocked'].append(_error(card.get('title') or 'E-Book '+str(card['id']),reason,
                book_id=card['id'],code=row['code'],retry_at=row['retry_at'],attempts=row['attempts']))
    ids={};paths={}
    for file in all_candidate_files:
        ids[file['id']]=ids.get(file['id'],0)+1;paths[file['absolutePath']]=paths.get(file['absolutePath'],0)+1
    guarded=False
    for detail,file in candidates:
        title=detail.get('title') or 'E-Book '+str(detail['id']);key=str(file['id']);old=previous.get(key,{})
        binding=None;api_binding=None;candidate=None;before=None;signature=None;since=now;path=None
        try:
            rel=relative(file['absolutePath'],spec['bo_root'])
            if not rel.parts or rel.suffix.lower()!='.epub' or any('readalong' in p.lower() or 'readaloud' in p.lower() for p in rel.parts):continue
            if ids[file['id']]!=1 or paths[file['absolutePath']]!=1:raise ValueError('Ambiguous BookOrbit ebook identity')
            jp=str(PurePosixPath(spec['jf_root'])/rel);matches=bypath.get(jp,[])
            descriptor=dict(context=observation_binding,spec={k:spec.get(k) for k in
                ('bo_root','jf_root','local_root','bo_library_id','jf_library_id')},
                book=dict(id=detail['id'],libraryId=detail['libraryId']),file=file,
                jellyfin=[{k:row.get(k) for k in ('Id','Path','Type')} for row in matches])
            api_binding=_hash(descriptor)
            binding=api_binding if observation_binding is not None else None
            retrying=force or selected is not None
            report['candidates']+=1
            if not retrying and _negative(old,binding,now):
                report['cached_rejections']+=1
                if metrics is not None:increment(metrics,'ebook_discovery_negative_hits')
                report['blocked'].append(_error(title,old['reason'],book_id=detail['id'],file_id=file['id'],
                    code=old['code'],retry_mode='manual_or_source_change',cached=True))
                continue
            if not retrying and _deferred(old,api_binding,now):
                report['waiting']+=1;report['retry_deferred']+=1
                report['blocked'].append(_error(title,old['reason'],book_id=detail['id'],file_id=file['id'],
                    code=old['code'],retry_at=old['retry_at'],attempts=old['attempts'],cached=True))
                continue
            if failed_books:
                # Unknown book details can hide another owner of this path/ID.
                # Preserve prior physical stability and rejection evidence, but
                # neither revisit media nor admit anything from a partial view.
                if not isinstance(old,dict) or not old:
                    obs[key]=dict(schema=3,outcome='inventory_wait',binding=api_binding,
                                  book_id=detail['id'],file_id=file['id'])
                retries=[row.get('retry_at') for row in obs.values() if isinstance(row,dict)
                         and row.get('book_id') in failed_books and type(row.get('retry_at')) in (int,float)]
                report['waiting']+=1
                report['blocked'].append(_error(title,'Complete ebook inventory is required before new admission',
                    book_id=detail['id'],file_id=file['id'],code='inventory_incomplete',
                    retry_at=min(retries) if retries else now+retry_initial_seconds,cached=True))
                continue
            if not matches:raise FileNotFoundError('Jellyfin EPUB is not indexed yet')
            if len(matches)!=1 or matches[0]['Id'] in existing_jf:raise ValueError('Ambiguous Jellyfin ebook identity')
            if media_guard is not None and not guarded:media_guard();guarded=True
            root=Path(spec['local_root']).resolve(strict=True);path=root/rel
            if path.resolve(strict=True)!=path or not path.is_file():raise ValueError('Ebook symlink or non-file excluded')
            before=path.stat()
            if before.st_size!=file['sizeBytes']:raise ValueError('Ebook size differs from BookOrbit')
            candidate=dict(title=title,bo_id=detail['id'],bo_library_id=spec['bo_library_id'],file_id=file['id'],
                bo_path=file['absolutePath'],jf_path=jp,jf_id=matches[0]['Id'],local_path=str(path),size=before.st_size)
            signature=_hash(dict(schema=2,candidate=candidate,file_identity=file_identity(before)))
            since=old.get('since',now) if old.get('signature')==signature and old.get('schema') in (2,3) else now
            if not _time(since,now):since=now
            obs[key]=dict(schema=3,outcome='observing',binding=api_binding,signature=signature,since=since,
                          book_id=detail['id'],file_id=file['id'])
            if now-since<stable_seconds:
                report['waiting']+=1
                if metrics is not None:increment(metrics,'ebook_discovery_waits')
                continue
            if metrics is not None:increment(metrics,'ebook_discovery_content_checks')
            try:index=inspect_epub(path,return_index=True)
            except (ValueError,zipfile.BadZipFile,ExpatError,KeyError,IndexError) as exc:
                if file_identity(before)!=file_identity(path.stat()):raise DiscoveryUnavailable('Ebook changed during validation') from None
                if binding_reader is not None and binding_reader()!=observation_binding:
                    raise DiscoveryUnavailable('Ebook deployment changed during validation') from None
                fresh=bo.detail(detail['id'])
                if (fresh.get('id')!=detail['id'] or fresh.get('libraryId')!=detail['libraryId']
                        or [f for f in fresh.get('files',[]) if f.get('id')==file['id']]!=[file]):
                    raise DiscoveryUnavailable('Ebook API source changed during validation') from None
                if binding is None:raise DiscoveryUnavailable(str(exc)+'; deployment identity is unverified') from None
                reason=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
                obs[key]=dict(schema=3,outcome='rejected',binding=binding,signature=signature,since=since,
                    verified_at=now,book_id=detail['id'],file_id=file['id'],reason=reason[:1024],code='epub_content_rejected')
                report['blocked'].append(_error(title,reason,book_id=detail['id'],file_id=file['id'],
                    code='epub_content_rejected',retry_mode='manual_or_source_change'))
                continue
            if file_identity(before)!=file_identity(path.stat()):raise DiscoveryUnavailable('Ebook changed during validation')
            if binding_reader is not None and binding_reader()!=observation_binding:raise DiscoveryUnavailable('Ebook deployment changed during validation')
            fresh=bo.detail(detail['id'])
            if (fresh.get('id')!=detail['id'] or fresh.get('libraryId')!=detail['libraryId']
                    or [f for f in fresh.get('files',[]) if f.get('id')==file['id']]!=[file]):
                raise DiscoveryUnavailable('Ebook API source changed during validation')
            current_items=jf.items(ids=[matches[0]['Id']],types='Book')
            wanted={k:matches[0].get(k) for k in ('Id','Path','Type')}
            current=[{k:item.get(k) for k in wanted} for item in current_items if item.get('Id')==matches[0]['Id']]
            if current!=[wanted]:raise DiscoveryUnavailable('Jellyfin ebook source changed during validation')
            if index_cache is not None:index_cache.seed(index)
            additions.append(dict(candidate,sha256=index.hash));obs.pop(key,None)
        except Exception as exc:
            reason=str(exc) if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__
            row=_retry(old,api_binding,now,reason,'candidate_unavailable',retry_initial_seconds,retry_max_seconds,
                       book_id=detail['id'],file_id=file['id'])
            if signature is not None and candidate is not None:row.update(signature=signature,since=since)
            obs[key]=row;report['waiting']+=1
            report['blocked'].append(_error(title,reason,book_id=detail['id'],file_id=file['id'],
                code=row['code'],retry_at=row['retry_at'],attempts=row['attempts']))
    if selected is None:
        obs={key:row for key,row in obs.items() if isinstance(row,dict) and
             (key in seen_files or row.get('book_id') in failed_books or
              key.startswith('book:') and row.get('book_id') in card_ids)}
    else:report['unmatched_retry_file_ids']=sorted(selected-seen_files-existing_files)
    report['added']=len(additions)
    return additions,obs,report


def observation_context(bridge,spec):
    guard=getattr(bridge,'media_observation_guard',None)
    reader=getattr(bridge,'media_observation_generation',getattr(bridge,'media_generation',None))
    if guard is None or reader is None:return None
    guard('ebooks');generation=reader('ebooks')
    if not isinstance(generation,str) or not generation:return None
    config=bridge.config;bo=bridge.bo;jf=bridge.jellyfin
    return dict(generation=generation,account=dict(bo_url=config.get('bo_url'),
        bo_username=getattr(bo,'_username',None),bo_user={k:(getattr(bo,'user',None) or {}).get(k) for k in ('id','username')},
        jellyfin={k:config.get('jellyfin',{}).get(k) for k in ('url','user_id','username','version')}),
        folders=[{k:row.get(k) for k in ('id','kind','active','paths')}
                 for row in config.get('folder_mappings',[]) if row.get('kind')=='ebooks' and row.get('active',True)])


def refresh(bridge,apply,*,force=False,retry_file_ids=None):
    selected=_ids(retry_file_ids);policy=io_settings(bridge.config)
    spec=bridge.config['jellyfin'].get('auto_ebooks');fixed=bridge.config['jellyfin'].get('ebooks',[])
    if not spec or spec.get('enabled') is not True:return fixed,None
    stored=bridge.load('_ebook_discovery');auto=stored.get('mappings',[]);existing=fixed+auto;now=bridge.clock()
    if not _time(now,now):raise ValueError('Invalid ebook discovery clock')
    config=bridge.config
    inventory_binding=_hash(dict(spec=spec,bo_url=config.get('bo_url'),bo_username=getattr(bridge.bo,'_username',None),
        jellyfin={k:config.get('jellyfin',{}).get(k) for k in ('url','user_id','username','version')},
        folders=[{k:row.get(k) for k in ('id','kind','active','paths')} for row in config.get('folder_mappings',[])
                 if row.get('kind')=='ebooks' and row.get('active',True)]))
    failure=stored.get('failure') or {};manual=force or selected is not None
    if not manual and _deferred(failure,inventory_binding,now):
        report=dict(enabled=True,scanned=False,added=0,candidates=0,waiting=1,cached_rejections=0,retry_deferred=1,
            blocked=[_error('Automatische E-Book-Zuordnung',failure['reason'],code=failure['code'],
                            retry_at=failure['retry_at'],attempts=failure['attempts'],cached=True)])
        _preserve_errors(report,stored.get('report'))
        return existing,report
    if not manual and _time(stored.get('checked_at'),now) and now-stored['checked_at']<policy['ebook_discovery_interval_seconds']:
        report=copy.deepcopy(stored.get('report') or dict(enabled=True,candidates=0,waiting=0,blocked=[]))
        report.update(scanned=False,added=0)
        return existing,report
    try:
        bridge.guard();bridge.jellyfin.guard()
        context=observation_context(bridge,spec)
        from jellyfin_ebook import EbookCoordinator
        additions,observations,report=discover(bridge.bo,bridge.jellyfin,spec,existing,
            stored.get('observations',{}),now,stable_seconds=policy['ebook_candidate_stable_seconds'],metrics=bridge,
            index_cache=EbookCoordinator(bridge).index_cache(bridge.config),
            media_guard=(lambda:bridge.media_guard('ebooks')) if getattr(bridge,'media_guard',None) else None,
            observation_binding=context,binding_reader=lambda:observation_context(bridge,spec),force=force,
            retry_file_ids=selected,retry_initial_seconds=policy['ebook_retry_initial_seconds'],
            retry_max_seconds=policy['ebook_retry_max_seconds'])
        if selected is not None:
            # A one-file retry must not hide other outstanding import errors.
            _preserve_errors(report,stored.get('report'),selected)
            report['retry_scope_file_ids']=sorted(selected)
        if apply:
            value=dict(stored,mappings=auto+additions,observations=observations,checked_at=now,report=report)
            value.pop('failure',None);bridge.save('_ebook_discovery',value)
            for mapping in additions:bridge.event('_jellyfin_ebook:'+str(mapping['file_id']),'ebook_mapped',dict(title=mapping['title']))
            error=json.dumps(report['blocked'],ensure_ascii=False,sort_keys=True)
            if report['blocked'] and bridge.load('_ebook_discovery_error').get('error')!=error:
                bridge.alert('_ebook_discovery','E-Book-Zuordnung zurueckgestellt: '+error)
            bridge.save('_ebook_discovery_error',dict(error=error))
        return existing+additions,report
    except Exception as exc:
        reason=str(exc) if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__
        failure=_retry(failure,inventory_binding,now,reason,'inventory_unavailable',
                       policy['ebook_retry_initial_seconds'],policy['ebook_retry_max_seconds'])
        report=dict(enabled=True,scanned=True,added=0,candidates=0,waiting=1,cached_rejections=0,retry_deferred=0,
            blocked=[_error('Automatische E-Book-Zuordnung',reason,code=failure['code'],retry_at=failure['retry_at'],attempts=failure['attempts'])])
        _preserve_errors(report,stored.get('report'))
        if apply:
            # A partial outage never destroys unrelated stability/negative proofs
            # or any established mappings, journals, approvals or progress.
            bridge.save('_ebook_discovery',dict(stored,mappings=auto,failure=failure,report=report))
            if bridge.load('_ebook_discovery_error').get('error')!=reason:bridge.alert('_ebook_discovery','E-Book-Zuordnung zurueckgestellt: '+reason)
            bridge.save('_ebook_discovery_error',dict(error=reason))
        return existing,report
