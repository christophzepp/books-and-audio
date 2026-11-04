import copy,hashlib,json,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from epub_locations import EpubLocations
from jellyfin_ebook import EbookCoordinator

class EbookTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.path=Path(self.tmp.name)/'test.epub'
  with zipfile.ZipFile(self.path,'w') as z:
   z.writestr('META-INF/container.xml','<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
   z.writestr('content.opf','<package><metadata/><manifest><item id="s" href="s.xhtml"/></manifest><spine><itemref idref="s"/></spine></package>')
   z.writestr('s.xhtml','<html xmlns="http://www.w3.org/1999/xhtml"><head/><body><p>'+('Hello world. '*900)+'</p></body></html>')
  self.e=EpubLocations(self.path);self.store={};self.writes=[];self.fail=False;self.concurrent=False
  self.m=dict(file_id=1,bo_id=1,bo_library_id=2,bo_path='/books/test.epub',jf_path='/data/ebooks/test.epub',jf_id='a'*32,sha256=self.e.hash,size=self.path.stat().st_size,local_path=str(self.path),title='Test')
  self.progress=dict(fileId=1,percentage=0,updatedAt=None,text=dict(cfi=None,page=None,updatedAt=None),narration=dict(seconds=None),kobo=dict(value=None),koreader='keep')
  self.status=None;self.user=dict(PlaybackPositionTicks=0,Played=False,PlayCount=0)
  self.bo=SimpleNamespace(detail=lambda _:dict(files=[dict(id=1,absolutePath=self.m['bo_path'],sizeBytes=self.m['size'])],readStatus=copy.deepcopy(self.status)),_id=str,_request=self.request,library=lambda _:dict(markAsFinishedPercentComplete=98),write_status=self.write_status)
  self.jf=SimpleNamespace(guard=lambda:None,active_ids=lambda:set(),items=lambda **kw:[dict(Id='a'*32,Type='Book',Path=self.m['jf_path'],UserData=copy.deepcopy(self.user))],update=self.update)
  b=SimpleNamespace(bo=self.bo,jellyfin=self.jf,load=lambda k:copy.deepcopy(self.store.get(k,{})),save=lambda k,v:self.store.update({k:copy.deepcopy(v)}),clock=lambda:1000,guard=lambda:None,write_record=lambda *a:None,event=lambda *a:None)
  self.c=EbookCoordinator(b)
  p=patch('jellyfin_ebook.Path.is_relative_to',return_value=True);p.start();self.addCleanup(p.stop)
 def request(self,method,path,label,payload=None):
  if method=='GET':return dict(items=[copy.deepcopy(self.progress)])
  self.writes.append('bo');w=payload['writes'][0]
  if self.fail:raise RuntimeError('uncertain')
  self.progress.update(percentage=w['percentage'],text=dict(w['text'],updatedAt='2026-10-30T12:00:00Z'),updatedAt='2026-10-30T12:00:00Z')
  if not self.status:self.status=dict(status='reading',startedAt='2026-10-30',finishedAt=None)
  if self.concurrent:self.user['PlaybackPositionTicks']=9000000
  return dict(results=[dict(fileId=1,status='saved')])
 def update(self,id,values):
  self.writes.append('jf')
  if self.fail:raise RuntimeError('uncertain')
  self.user.update(values)
 def write_status(self,id,status,**kw):self.writes.append('status');self.status=dict(status=status,**kw)
 def run_cycle(self):return self.c.handle(self.m,True,0)
 def set_bo(self,i,finished=False):
  point,pct=self.e.index_to_bo(i);self.progress.update(percentage=pct,text=dict(cfi=point,page=None,updatedAt='2026-10-30T12:00:00Z'),updatedAt='2026-10-30T12:00:00Z');self.status=dict(status='read' if finished else 'reading',startedAt='2026-10-30',finishedAt='2026-10-30' if finished else None)
 def set_jf(self,i,finished=False):self.user.update(PlaybackPositionTicks=self.e.index_to_ticks(i),Played=finished,PlayCount=1,LastPlayedDate='2026-10-30T12:00:00Z')
 def test_both_directions_idempotent(self):
  self.assertEqual(self.run_cycle(),'unchanged');self.set_bo(3);self.assertEqual(self.run_cycle(),'synced');self.assertEqual(self.e.ticks_to_index(self.user['PlaybackPositionTicks']),3)
  self.set_jf(5);self.assertEqual(self.run_cycle(),'synced');self.assertEqual(self.e.bo_to_index(self.progress['text']['cfi']),5);self.assertEqual(self.progress['koreader'],'keep')
  n=len(self.writes);self.assertEqual(self.run_cycle(),'unchanged');self.assertEqual(len(self.writes),n)
 def test_completion_and_reopening(self):
  self.set_bo(2);self.run_cycle();self.set_jf(5,True);self.run_cycle();self.assertEqual(self.status['status'],'read')
  self.set_jf(2);self.run_cycle();self.assertEqual(self.status['status'],'reading')
  self.set_bo(3,True);self.run_cycle();self.assertTrue(self.user['Played']);self.set_bo(1);self.run_cycle();self.assertFalse(self.user['Played'])
 def test_conflict_blocks(self):
  self.run_cycle();self.set_bo(2);self.set_jf(4)
  with self.assertRaisesRegex(ValueError,'Concurrent'):self.run_cycle()
  self.assertFalse(self.writes)
 def test_uncertain_not_replayed(self):
  self.run_cycle();self.set_jf(2);self.fail=True
  with self.assertRaises(RuntimeError):self.run_cycle()
  self.fail=False;n=len(self.writes)
  with self.assertRaisesRegex(RuntimeError,'Interrupted'):self.run_cycle()
  self.assertEqual(len(self.writes),n)
 def test_source_change_stops_followup(self):
  self.run_cycle();self.set_jf(3);self.concurrent=True
  with self.assertRaises(RuntimeError):self.run_cycle()
  self.assertEqual(self.writes,['bo'])
 def test_changed_file_blocks(self):
  self.set_bo(2);self.path.write_bytes(b'different')
  with self.assertRaisesRegex(ValueError,'changed'):self.run_cycle()
  self.assertFalse(self.writes)
 def test_all_location_roundtrips(self):
  for i in range(self.e.total+1):
   p,_=self.e.index_to_bo(i);self.assertEqual(self.e.bo_to_index(p),i);self.assertEqual(self.e.ticks_to_index(self.e.index_to_ticks(i)),i)
if __name__=='__main__':unittest.main()
