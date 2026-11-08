"""A cycle's observed mappings never authorize a post-journal audio write."""
import copy
import json
import unittest
from unittest.mock import patch

from jellyfin_audio import TICKS
from revision import mapping_fingerprint
import test_jellyfin_transactions as fixture


class AudioMappingCASTests(unittest.TestCase):
    cycle=fixture.Transactions.cycle

    def setUp(self):
        fixture.Transactions.setUp(self)
        self.bridge.config['mappings']=[]
        with self.bridge.db:
            self.bridge.db.execute('INSERT INTO auto_mappings VALUES (?,?,?)',
                (self.mapping['abs_id'],self.mapping['bo_id'],json.dumps(self.mapping)))
        self.abs.set(10);self.bo.set(10)
        self.jf.data.update(PlaybackPositionTicks=10*TICKS,PlayCount=1)
        result=self.cycle();self.assertFalse(result['blocked'],result)
        self.assertFalse(self.abs.writes or self.bo.writes or self.jf.writes)
        self.key='_jellyfin_audio:'+self.mapping['abs_id']

    def inject_at_journal(self,source,change):
        if source=='jellyfin':
            self.jf.data.update(PlaybackPositionTicks=45*TICKS)
            stage='sending_abs_position'
        else:
            self.abs.set(25);self.bo.set(25)
            stage='sending_jellyfin_0'
        original=self.bridge.save;injected=[]
        def save(ident,value):
            original(ident,value)
            if ident==self.key and (value.get('pending') or {}).get('stage')==stage and not injected:
                if change=='mapping':
                    changed=copy.deepcopy(self.mapping);changed['bo_id']+=1
                    changed['fingerprint']=mapping_fingerprint(changed)
                    with self.bridge.db:
                        self.bridge.db.execute('UPDATE auto_mappings SET bo_id=?,value=? WHERE id=?',
                            (changed['bo_id'],json.dumps(changed),self.mapping['abs_id']))
                else:self.bridge.config['paused_mappings']={self.mapping['abs_id']:'user pause during journal'}
                injected.append(True)
        with patch.object(self.bridge,'save',side_effect=save):result=self.cycle()
        self.assertTrue(injected)
        self.assertFalse(self.abs.writes or self.bo.writes or self.jf.writes)
        saved=self.bridge.load(self.key)
        self.assertEqual(saved['pending']['stage'],stage)
        self.assertEqual(saved['pending']['goal']['position'],45 if source=='jellyfin' else 25)
        self.assertTrue(all(row['position']==10 for row in saved['baseline'].values()))
        self.assertFalse(self.bridge._mapping_cycle_active)
        self.assertIsNone(self.bridge._mapping_cycle_cache)
        if change=='mapping':
            self.assertEqual(len(result['blocked']),1,result)
            self.assertIn('Dateizuordnung',result['blocked'][0]['reason'])
        else:
            self.assertEqual(len(result['paused']),1,result)
            self.assertFalse(result['blocked'])
        return result

    def test_mapping_change_after_jellyfin_journal_sends_no_jellyfin_post(self):
        self.inject_at_journal('abs','mapping')

    def test_pause_after_jellyfin_journal_sends_no_jellyfin_post(self):
        self.inject_at_journal('abs','pause')

    def test_mapping_change_after_abs_journal_sends_no_abs_or_bookorbit_post(self):
        self.inject_at_journal('jellyfin','mapping')

    def test_pause_after_abs_journal_sends_no_abs_or_bookorbit_post(self):
        self.inject_at_journal('jellyfin','pause')


if __name__=='__main__':unittest.main()
