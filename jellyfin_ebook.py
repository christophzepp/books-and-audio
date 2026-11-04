"""Same-file EPUB reading progress, with a durable journal and readback checks."""
import copy
import hashlib
from pathlib import Path
import uuid
from datetime import datetime, timezone
from epub_locations import EpubLocations
from jellyfin_audio import choose, same, encode
from folder_manager import translate


def reading(bo, file_id):
    r=bo._request('GET','/api/v1/reading-progress?fileId='+bo._id(file_id),'/api/v1/reading-progress')
    if not isinstance(r,dict) or len(r.get('items',[]))!=1 or r['items'][0]['fileId']!=file_id:
        raise RuntimeError('Incomplete BookOrbit reading progress')
    return r['items'][0]


def write_reading(bo,file_id,old,cfi,percentage):
    w=dict(fileId=file_id,kind='text',percentage=percentage,text={'cfi':cfi,'page':None},
           kobo=old.get('kobo'),koreader=old.get('koreader'))
    if old.get('updatedAt'):w['editedAt']=old['updatedAt']
    r=bo._request('PUT','/api/v1/reading-progress','/api/v1/reading-progress',payload={'writes':[w]})
    if len(r.get('results',[]))!=1 or r['results'][0].get('fileId')!=file_id or r['results'][0].get('status')!='saved':
        raise RuntimeError('BookOrbit reading write superseded or unconfirmed')


class EbookCoordinator:
    def __init__(self,bridge):self.b=bridge;self.jf=bridge.jellyfin

    def read(self,m,e):
        d=self.b.bo.detail(m['bo_id'])
        files=[f for f in d['files'] if f['id']==m['file_id']]
        if len(files)!=1 or files[0]['absolutePath']!=m['bo_path'] or files[0]['sizeBytes']!=m['size']:
            raise ValueError('BookOrbit EPUB identity changed')
        item=self.jf.items(ids=[m['jf_id']],types='Book')[0]
        if item.get('Path')!=m['jf_path'] or item.get('Type')!='Book':raise ValueError('Jellyfin EPUB identity changed')
        p=reading(self.b.bo,m['file_id']);u=item['UserData'];rs=d.get('readStatus')
        status=(rs or {}).get('status');point=(p.get('text') or {}).get('cfi')
        if status not in (None,'unread','want_to_read','reading','rereading','read'):raise ValueError('Unsupported BookOrbit reading status')
        bi=e.bo_to_index(point) if point else 0
        if not point and p.get('percentage',0)>0:raise ValueError('BookOrbit reading position has no CFI')
        bs='finished' if status=='read' else 'in_progress' if status in ('reading','rereading') or point else 'not_started'
        tick=u.get('PlaybackPositionTicks');ji=e.ticks_to_index(tick)
        if type(u.get('Played')) is not bool:raise ValueError('Invalid Jellyfin completion flag')
        js='finished' if u['Played'] else 'in_progress' if tick or u.get('PlayCount') else 'not_started'
        # Mark-as-read may clear the Jellyfin bookmark. It means the end of
        # this EPUB, not a request to send the reader back to the first page.
        if js=='finished' and not tick:ji=e.total
        if bs=='finished' and not point:bi=e.total
        fp=m['sha256']+':'+str(m['file_id'])+':'+m['jf_id']
        pair={'bo':dict(position=bi*10,status=bs,fingerprint=fp),'jf':dict(position=ji*10,status=js,fingerprint=fp)}
        raw=dict(progress=p,status=rs,jf={k:u.get(k) for k in ('PlaybackPositionTicks','Played','PlayCount','LastPlayedDate')})
        return pair,raw

    def handle(self,m,apply,settle):
        b=self.b;key='_jellyfin_ebook:'+str(m['file_id'])
        config = getattr(b, 'config', {})
        path=Path(m['local_path']).resolve()
        if 'folder_mappings' in config:
            if path != Path(m['local_path']): raise ValueError('EPUB symlink or noncanonical path excluded')
            if (translate(m['bo_path'], 'bo', 'worker', 'ebooks', config) != str(path)
                    or translate(m['bo_path'], 'bo', 'jf', 'ebooks', config) != m['jf_path']):
                raise ValueError('EPUB folder aliases no longer match the verified mapping')
        elif not path.is_relative_to(Path('/ebooks')):raise ValueError('EPUB outside allowed root')
        e=EpubLocations(path,m['sha256'])
        if path.stat().st_size!=m['size']:raise ValueError('EPUB size changed')
        self.jf.guard();pair,raw=self.read(m,e);stored=b.load(key)
        if stored.get('pending'):
            if all(same(x,stored['pending']['goal']) for x in pair.values()):
                if apply:b.save(key,dict(baseline=pair))
                return 'unchanged'
            raise RuntimeError('Interrupted EPUB transfer requires journal review')
        source=choose(pair,stored.get('baseline'))
        if source is None:
            if apply:b.save(key,dict(baseline=pair))
            return 'unchanged'
        if not apply:return 'proposed'
        signature=hashlib.sha256(encode(raw).encode()).hexdigest();candidate=stored.get('candidate',{})
        if candidate.get('signature')!=signature:
            candidate=dict(signature=signature,since=b.clock());stored['candidate']=candidate;b.save(key,stored)
        if b.clock()-candidate['since']<settle or m['jf_id'] in self.jf.active_ids():return 'waiting'
        goal=pair[source];index=round(goal['position']/10);point,pct=e.index_to_bo(index)
        if goal['status']=='finished':pct=100
        if source=='jf' and goal['status']!='finished':
            lib=b.bo.library(m['bo_library_id']);threshold=lib.get('markAsFinishedPercentComplete')
            if pct>=(98 if threshold is None else threshold):raise ValueError('BookOrbit would prematurely mark EPUB finished')
        def unchanged():
            b.guard();self.jf.guard()
            if hashlib.sha256(path.read_bytes()).hexdigest()!=m['sha256']:raise ValueError('EPUB changed before transfer')
            _,fresh=self.read(m,e)
            if encode(fresh)!=encode(raw):raise RuntimeError('Concurrent EPUB progress change')
        unchanged()
        record=dict(id=str(uuid.uuid4()),goal=goal,source=source,before=raw,stage='prepared')
        stored['pending']=record;b.save(key,stored)
        def stage(label):record['stage']=label;b.save(key,stored)
        if source=='bo':
            update=dict(PlaybackPositionTicks=e.index_to_ticks(index),Played=goal['status']=='finished',PlayCount=max(raw['jf'].get('PlayCount') or 0,1))
            date=raw['progress'].get('updatedAt')
            if date:update['LastPlayedDate']=date
            unchanged();stage('sending_jellyfin');self.jf.update(m['jf_id'],update)
            final,fr=self.read(m,e)
            if fr['progress']!=raw['progress'] or fr['status']!=raw['status']:raise RuntimeError('BookOrbit changed during EPUB transfer')
        else:
            unchanged();stage('sending_bookorbit_position')
            write_reading(b.bo,m['file_id'],raw['progress'],point,pct)
            current,fr=self.read(m,e)
            if fr['jf']!=raw['jf'] or not same(dict(current['bo'],status=goal['status']),goal):raise RuntimeError('BookOrbit EPUB position not confirmed')
            if fr['progress'].get('narration')!=raw['progress'].get('narration') or fr['progress'].get('kobo')!=raw['progress'].get('kobo') or fr['progress'].get('koreader')!=raw['progress'].get('koreader'):
                raise RuntimeError('Unrelated reading marker changed')
            raw=fr
            if current['bo']['status']!=goal['status']:
                unchanged();stage('sending_bookorbit_status')
                previous=raw['status'] or {};date=raw['jf'].get('LastPlayedDate')
                finished=None
                if goal['status']=='finished':
                    if not date:raise RuntimeError('Jellyfin EPUB completion date missing')
                    finished=datetime.fromisoformat(date.replace('Z','+00:00')).date().isoformat()
                b.bo.write_status(m['bo_id'],'read' if goal['status']=='finished' else 'reading',started_at=previous.get('startedAt'),finished_at=finished)
            final,fr=self.read(m,e)
        if not all(same(v,goal) for v in final.values()):raise RuntimeError('EPUB transfer not confirmed')
        record.update(stage='complete',after=fr);b.write_record(record['id'],key,record,'complete');b.save(key,dict(baseline=final))
        b.event(key,'ebook_synced',dict(title=m['title'],source=source,location=index,status=goal['status']))
        return 'synced'

    def cycle(self,apply,settle):
        from ebook_discovery import refresh
        mappings, discovery = refresh(self.b, apply)
        r=dict(mapped=0,synced=0,unchanged=0,waiting=0,proposed=0,blocked=[])
        if discovery is not None:r['discovery']=discovery
        for m in mappings:
            r['mapped']+=1
            try:
                r[self.handle(m,apply,settle)]+=1
                if apply:self.b.save('_jellyfin_ebook_error:'+str(m['file_id']),{})
            except Exception as exc:
                reason=str(exc) if isinstance(exc,(RuntimeError,ValueError)) or type(exc).__name__=='RequestError' else type(exc).__name__
                r['blocked'].append(dict(title=m['title'],reason=reason))
                error_key='_jellyfin_ebook_error:'+str(m['file_id'])
                if apply and self.b.load(error_key).get('reason')!=reason:
                    self.b.alert(error_key,'E-Book-Sync zurueckgestellt: '+m['title']+' — '+reason)
                    self.b.save(error_key,dict(reason=reason))
        return r
