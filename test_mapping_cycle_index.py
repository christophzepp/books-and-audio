"""Cycle-local inventory reuse; real transaction guards remain fresh."""
from collections import Counter
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sync
from mapping import build_mapping
from revision import mapping_fingerprint
from test_sync import fixture
import test_sync as fixture_tests
from test_native_readalong import native_fixture


class MappingCycleScaleTests(unittest.TestCase):
    def scaled(self,count,*,native=False):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);items={};details={};manifests={};mappings=[];pins={}
        for index in range(count):
            if native:ai,bd,bm,pin=native_fixture()
            else:ai,bd,bm=fixture()
            ident='book-'+str(index);bo_id=23+index
            folder='/audiobooks/Book-'+str(index);path=folder+'/01.mp3'
            ai.update(id=ident,path=folder);ai['media']['tracks'][0]['metadata']['path']=path
            bd.update(id=bo_id,folderPath=folder);bd['files'][0]['absolutePath']=path
            bm['book']['id']=bo_id
            if native:
                pin=dict(pin,bo_id=bo_id,overlay_path=folder+'/readalong.epub')
                bd['files'][1]['absolutePath']=pin['overlay_path'];pins[ident]=pin
            items[ident]=ai;details[bo_id]=bd;manifests[bo_id]=bm
            mappings.append(build_mapping(ai,bd,bm,native_readalong=pin if native else None))
        class ABS:
            def version(self):return '2.37.1'
            def item(self,ident):return copy.deepcopy(items[ident])
            def progress(self,ident):return None
        class BO:
            def detail(self,ident):return copy.deepcopy(details[ident])
            def manifest(self,ident):return copy.deepcopy(manifests[ident])
            def playback(self,ident):return None
        cfg=dict(abs_url='http://abs',bo_url='http://bo',abs_version='2.37.1',mappings=[])
        if native:cfg.update(native_readalong=pins,auto_native_readalong={'enabled':True})
        with patch('sync.ABSClient',return_value=ABS()),patch('sync.BOClient',return_value=BO()):
            bridge=sync.Bridge(root,cfg,dict(abs_token='fixture',bo_username='reader',bo_password='fixture'),clock=lambda:1000)
        self.addCleanup(bridge.db.close)
        with bridge.db:bridge.db.executemany('INSERT INTO auto_mappings VALUES (?,?,?)',
            [(m['abs_id'],m['bo_id'],sync.dump(m)) for m in mappings])
        (root/'deployment.json').write_text('{"ok":true,"checked_at":1000}')
        return bridge,mappings

    def test_one_thousand_books_load_and_copy_mapping_inventory_linearly(self):
        measured=[]
        for count in (100,1000):
            bridge,mappings=self.scaled(count);sql=[];copies=Counter();decodes=Counter()
            bridge.db.set_trace_callback(sql.append)
            deepcopy=copy.deepcopy;loads=json.loads
            def copying(value,*args,**kwargs):
                if isinstance(value,list) and value and isinstance(value[0],dict) and 'abs_id' in value[0]:
                    copies['inventories']+=1;copies['mappings']+=len(value)
                return deepcopy(value,*args,**kwargs)
            def loading(value,*args,**kwargs):
                result=loads(value,*args,**kwargs)
                if isinstance(result,dict) and 'abs_id' in result:decodes['mapping_json']+=1
                return result
            with patch.object(copy,'deepcopy',copying),patch.object(json,'loads',loading), \
                    contextlib.redirect_stdout(io.StringIO()):result=bridge.cycle(apply=True,settle=0)
            bulk=[q for q in sql if q.startswith('SELECT id,bo_id,value FROM auto_mappings')]
            self.assertEqual(len(bulk),1)
            self.assertEqual(decodes['mapping_json'],count)
            self.assertEqual(copies['inventories'],2)
            self.assertEqual(copies['mappings'],2*count)
            self.assertEqual(result['unchanged'],count);self.assertEqual(result['blocked'],[])
            self.assertFalse(bridge._mapping_cycle_active);self.assertIsNone(bridge._mapping_cycle_cache)
            measured.append(sum(q.startswith('SELECT') for q in sql))
        self.assertLessEqual(measured[1],10*measured[0]+10)

    def test_one_thousand_configured_native_pins_are_checked_once_not_per_book(self):
        bridge,mappings=self.scaled(1000,native=True);sql=[];bridge.db.set_trace_callback(sql.append)
        with patch.object(bridge,'native_readalong',wraps=bridge.native_readalong) as pins, \
                patch.object(bridge,'_seed_mappings',wraps=bridge._seed_mappings) as seeds, \
                contextlib.redirect_stdout(io.StringIO()):result=bridge.cycle(apply=True,settle=0)
        self.assertEqual(result['unchanged'],1000);self.assertEqual(result['blocked'],[])
        self.assertEqual(pins.call_count,1);self.assertEqual(seeds.call_count,1)
        reads=[q for q in sql if q.startswith('SELECT bo_id,proof FROM native_readalong_enrollments WHERE id=')]
        self.assertEqual(len(reads),1000)

    def test_observed_inventory_and_pin_are_defensive_copies(self):
        bridge,mappings=self.scaled(1);m=mappings[0]
        bridge.config['native_readalong']={m['abs_id']:dict(bo_id=m['bo_id'],overlay_file_id=123,
            overlay_path=m['folder']+'/generated.epub',overlay_size=4321)}
        bridge._mapping_cycle_active=True
        first=bridge.list_mappings(observe=True);first[0]['folder']='/changed'
        self.assertEqual(bridge.list_mappings(observe=True)[0]['folder'],m['folder'])
        pin=bridge._native_pin(m['abs_id'],observe=True);pin['overlay_size']=1
        self.assertEqual(bridge._native_pin(m['abs_id'],observe=True)['overlay_size'],4321)
        bridge._mapping_cycle_active=False;bridge.invalidate_mapping_observations()

    def test_current_pause_is_honored_even_after_observed_inventory_is_loaded(self):
        bridge,mappings=self.scaled(1);ident=mappings[0]['abs_id']
        def pause():bridge.config['paused_mappings']={ident:'new user pause'}
        bridge.serve_preview=pause
        with patch.object(bridge.abs,'item',side_effect=AssertionError('paused book API read')), \
                contextlib.redirect_stdout(io.StringIO()):result=bridge.cycle(apply=True,settle=0)
        self.assertEqual(len(result['paused']),1);self.assertEqual(result['blocked'],[])
        self.assertEqual(bridge.load(ident),{})

    def test_failed_cycle_does_not_leave_a_view_available_to_later_callers(self):
        bridge,mappings=self.scaled(1);bridge.config['paused_mappings']={'unknown':'invalid'}
        with self.assertRaises(ValueError):bridge.cycle(apply=True,settle=0)
        self.assertFalse(bridge._mapping_cycle_active);self.assertIsNone(bridge._mapping_cycle_cache)


class MappingFreshCASTests(unittest.TestCase):
    setUp=fixture_tests.SyncIntegrationTests.setUp
    cycle=fixture_tests.SyncIntegrationTests.cycle

    def auto(self):
        self.bridge.config['mappings']=[]
        with self.bridge.db:self.bridge.db.execute('INSERT INTO auto_mappings VALUES (?,?,?)',
            (self.mapping['abs_id'],self.mapping['bo_id'],sync.dump(self.mapping)))

    def change_auto(self):
        changed=copy.deepcopy(self.mapping);changed['bo_id']+=1
        changed['fingerprint']=mapping_fingerprint(changed)
        with self.bridge.db:self.bridge.db.execute('UPDATE auto_mappings SET bo_id=?,value=? WHERE id=?',
            (changed['bo_id'],sync.dump(changed),changed['abs_id']))

    def test_midcycle_mapping_change_stops_before_first_remote_mutation(self):
        self.auto();self.abs.set(25);self.bridge.serve_preview=self.change_auto
        result=self.cycle()
        self.assertEqual(len(result['blocked']),1)
        self.assertIn('Dateizuordnung',result['blocked'][0]['reason'])
        self.assertFalse(self.abs.writes or self.bo.writes)
        self.assertEqual(self.bridge.db.execute('SELECT count(*) FROM operations').fetchone()[0],0)

    def test_mapping_change_after_journal_keeps_pending_and_sends_nothing(self):
        self.auto();self.abs.set(25);pair,raw=self.bridge.read_pair(self.mapping)
        self.bridge._mapping_cycle_active=True;self.bridge.list_mappings(observe=True)
        original=self.bridge.write_record
        def change_after_journal(oid,ident,record,status='pending'):
            original(oid,ident,record,status)
            if record['stage'].startswith('sending_'):self.change_auto()
        with patch.object(self.bridge,'write_record',change_after_journal):
            with self.assertRaisesRegex(RuntimeError,'Dateizuordnung'):
                self.bridge.apply(self.mapping,pair,raw,'abs_to_bo',None)
        self.assertFalse(self.abs.writes or self.bo.writes)
        rows=self.bridge.db.execute("SELECT value FROM operations WHERE status='pending'").fetchall()
        self.assertEqual(len(rows),1);self.assertEqual(json.loads(rows[0][0])['stage'],'sending_position')

    def test_pending_recovery_does_not_use_old_cycle_mapping_inventory(self):
        self.auto();self.abs.set(25);self.bo.set(25)
        self.bridge.write_record('fixture-pending',self.mapping['abs_id'],
            dict(goal=dict(position=25,status='in_progress'),stage='sending_position'))
        self.bridge._mapping_cycle_active=True;self.bridge.list_mappings(observe=True)
        self.change_auto()
        with self.assertRaisesRegex(RuntimeError,'Dateizuordnung'):self.bridge.read_pair(self.mapping)
        self.assertEqual(self.bridge.db.execute("SELECT count(*) FROM operations WHERE status='pending'").fetchone()[0],1)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_pending_prepare_reads_mapping_and_progress_without_observation_snapshots(self):
        self.auto();self.abs.set(25);self.bo.set(25)
        self.bridge.write_record('fixture-pending',self.mapping['abs_id'],
            dict(goal=dict(position=25,status='in_progress'),stage='sending_position'))
        self.bridge._mapping_cycle_active=True;self.bridge.list_mappings(observe=True)
        original=self.bridge.observation
        def only_fresh(*attributes):
            self.assertEqual(attributes,())
            return original()
        with patch.object(self.bridge,'observation',only_fresh):
            self.bridge.prepare_pair(self.mapping,{},True)

    def test_observed_pause_cannot_authorize_writes_after_policy_changes(self):
        self.abs.set(25);pair,raw=self.bridge.read_pair(self.mapping)
        self.bridge._mapping_cycle_active=True;self.bridge.list_mappings(observe=True)
        self.bridge.config['paused_mappings']={self.mapping['abs_id']:'user pause after observation'}
        with self.assertRaises(sync.MappingPaused):self.bridge.apply(self.mapping,pair,raw,'abs_to_bo',None)
        self.assertFalse(self.abs.writes or self.bo.writes)

    def test_edited_original_mapping_fields_cannot_hide_behind_unchanged_stored_fingerprint(self):
        self.abs.set(25);pair,raw=self.bridge.read_pair(self.mapping)
        original=copy.deepcopy(self.bridge.config['mappings'][0])
        self.bridge.config['mappings'][0]=dict(original,folder='/audiobooks/Changed')
        with self.assertRaises((RuntimeError,ValueError)):
            self.bridge.apply(self.mapping,pair,raw,'abs_to_bo',None)
        self.assertFalse(self.abs.writes or self.bo.writes)


if __name__=='__main__':unittest.main()
