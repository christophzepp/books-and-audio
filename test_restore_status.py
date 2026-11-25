import json
import os
from pathlib import Path
import tempfile
import unittest

from restore_status import CHECKS, COUNTS, read_report


class RestoreDashboardTest(unittest.TestCase):
    def test_only_public_fields_of_consistent_reports_reach_dashboard(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(read_report(root)['state'], 'missing')
            target = root / 'restore-check' / 'last.json'
            target.parent.mkdir()
            report = dict(schema=1, checked_at=1700000000, ok=True, checks=dict.fromkeys(CHECKS, True),
                          counts=dict.fromkeys(COUNTS, 0), error=None, extra='private diagnostic')
            target.write_text(json.dumps(report))
            self.assertEqual(read_report(root)['state'], 'passed')
            self.assertNotIn('extra', read_report(root))
            report['checks']['accounts'] = False
            target.write_text(json.dumps(report))
            self.assertEqual(read_report(root)['state'], 'unreadable')
            report.update(ok=False, error='accounts')
            target.write_text(json.dumps(report))
            self.assertEqual(read_report(root)['error'], 'accounts')
            target.unlink()
            target.symlink_to(root / 'private')
            self.assertEqual(read_report(root)['state'], 'unreadable')
            target.unlink()
            os.mkfifo(target)
            self.assertEqual(read_report(root)['state'], 'unreadable')
