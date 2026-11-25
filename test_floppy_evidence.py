import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from floppy_evidence import MAX_AGE, attach_floppy_evidence, digest, scope
from tracking_delivery import lookup_key


class FloppyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'floppy.json'
        self.scope = dict(upstream='http://pine', username='listener', floppy_user='target', relay_url='http://relay')
        self.key = digest(['feed', 'source-enclosure'])
        self.wire_key = lookup_key('feed', 'actual-wire-enclosure')
        self.now = 1_800_000
        self.stamp = datetime.fromtimestamp(self.now-30, timezone.utc).isoformat()
        self.entry = dict(seconds=100, completed=True, end_at=self.now-30, updated_at=self.now-5)
        self.snapshot = dict(version=1, observed_at=self.now, synced_at=self.now-2, cursor=9876,
                             scope=scope(**self.scope), episodes={self.wire_key:self.entry})
        self.evidence = dict(state='ok', target=dict(state='unverified'),
            source=dict(completed=True,total=100,position=88,observed_at=self.now-15,
                        updated_at=self.now-30,present_in_last_scan=True),
            event=dict(stage='offered',cursor=9876,ready_at=self.now-15,action_timestamp=self.stamp),
            delivery_match=dict(lookup_hash=self.key,unique=True,wire_hash=self.wire_key,wire_unique=True,
                                wire_position=100,wire_timestamp=self.stamp))

    def report(self, save=True, **kwargs):
        if save:
            self.path.write_text(json.dumps(self.snapshot))
        report=attach_floppy_evidence(self.evidence,self.path,now=self.now,**(self.scope|kwargs))
        self.assertNotIn('delivery_match', report)
        return report['target']['state']

    def test_matching_exact_wire_identity_and_floppy_end_timestamp_confirms_current_state(self):
        self.assertEqual(self.report(), 'verified')
        self.assertEqual(self.key, lookup_key('feed', 'source-enclosure'))
        self.assertEqual(self.report(floppy_user='other'), 'unverified')
        self.assertEqual(self.report(username='other'), 'unverified')
        self.assertEqual(self.report(relay_url='http://other-relay'), 'unverified')

    def test_no_older_generation_progress_identity_or_cursor_can_confirm(self):
        replacements = [
            ('source','present_in_last_scan',False), ('source','total',110), ('source','updated_at',self.now-3),
            ('source','observed_at',self.now-MAX_AGE-1), ('event','stage','blocked'),('event','cursor',9877),
            ('delivery_match','unique',False),('delivery_match','wire_unique',False),
            ('delivery_match','wire_position',98),('delivery_match','wire_hash','another release'),
            ('delivery_match','wire_timestamp','invalid')]
        for area, key, value in replacements:
            with self.subTest(area=area,key=key):
                old=self.evidence[area][key]
                self.evidence[area][key]=value
                self.assertEqual(self.report(),'unverified')
                self.evidence[area][key]=old
        self.entry['end_at']-=100
        self.assertEqual(self.report(),'unverified')

    def test_missing_expired_or_malformed_snapshot_leaves_only_transport_evidence(self):
        self.assertEqual(self.report(save=False), 'unverified')
        self.path.write_text('{')
        self.assertEqual(self.report(save=False), 'unverified')
        self.snapshot['observed_at']=self.now-MAX_AGE-1
        self.assertEqual(self.report(),'unverified')
        for snapshot in [None, {}, ['bad']]:
            self.snapshot=snapshot
            self.assertEqual(self.report(),'unverified')

    def test_unfinished_wire_position_respects_stock_floppy_completion_boundary(self):
        self.evidence['source'].update(completed=False,position=98)
        self.evidence['delivery_match']['wire_position']=94
        self.entry.update(completed=False,seconds=94,end_at=None)
        self.assertEqual(self.report(),'verified')
        self.entry['seconds']=98
        self.assertEqual(self.report(),'unverified')

    def test_missing_new_wire_manifest_requires_matching_source_enclosure_and_episode_time(self):
        self.evidence['delivery_match']=dict(lookup_hash=self.key,unique=True)
        self.evidence['event'].update(stage='acknowledged',cursor=None)
        self.assertEqual(self.report(),'unverified')
        self.snapshot['episodes'][self.key]=copy.deepcopy(self.entry)
        self.assertEqual(self.report(),'verified')
        # Historical target progress predating this generation is not evidence even at equal seconds.
        self.snapshot['episodes'][self.key]['updated_at']=self.now-32
        self.assertEqual(self.report(),'unverified')
