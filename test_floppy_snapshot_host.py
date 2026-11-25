"""The host wrapper isolates Floppy's settings and preserves the complete last snapshot on failure."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import floppy_evidence
import floppy_snapshot_host as host


class FloppySnapshotHostTests(unittest.TestCase):
    def test_standalone_validation_matches_the_image_format(self):
        values = ('https://pine/', 'Jörg', 'Änne', 'http://tracking/')
        self.assertEqual(host.scope(*values), floppy_evidence.scope(*values))
        self.assertEqual(host.MAX_BYTES, floppy_evidence.MAX_BYTES)

    def test_private_floppy_process_avoids_maintenance_and_preserves_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            django = Path(tmp) / 'django'
            django.mkdir()
            (django/'__init__.py').touch()
            (django/'conf.py').write_text('class Settings: TESTING = False\nsettings = Settings()\n')
            (django/'db.py').write_text(
                'import json, os\n'
                'from types import SimpleNamespace\n'
                'from .conf import settings\n'
                'assert settings.TESTING is True\n'
                "connections={'default':SimpleNamespace(vendor=os.environ['VENDOR'],"
                "settings_dict={'OPTIONS':json.loads(os.environ['DB_OPTIONS'])})}\n")
            # Exercise real fd-based output and an exporter using future imports unchanged.
            script = (
                "from __future__ import annotations\nimport json, os, sys\n"
                "from django.db import connections\n"
                "os.write(1,b'framework notice\\n')\nprint('exporter diagnostic',file=sys.stderr)\n"
                "print(json.dumps(connections['default'].settings_dict['OPTIONS']))\n")
            for vendor, options, expected in [
                ('sqlite', {'transaction_mode':'IMMEDIATE'}, {'transaction_mode':'DEFERRED'}),
                ('sqlite', {}, {}),  # Django versions using SQLite's implicit DEFERRED default
                ('postgresql', {'application_name':'floppy'}, {'application_name':'floppy'})
            ]:
                with self.subTest(vendor=vendor,options=options):
                    env=dict(os.environ, PYTHONPATH=tmp, VENDOR=vendor, DB_OPTIONS=json.dumps(options))
                    result=subprocess.run([sys.executable,'-c',host.FLOPPY_PROCESS],input=script,
                                          capture_output=True,env=env,text=True,check=True)
                    self.assertEqual(json.loads(result.stdout),expected)
                    self.assertIn('exporter diagnostic',result.stderr)
                    self.assertIn('framework notice',result.stderr)

    def test_failed_export_preserves_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'floppy.json'
            output.write_text('previous')
            with patch.object(host.subprocess, 'run', side_effect=[
                    subprocess.CompletedProcess([],0,stdout=b'image exporter'),
                    subprocess.CalledProcessError(1,'docker exec')]) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    host.refresh(output, 'web','floppy','http://pine','listener','target','http://relay')
                cmd = run.call_args.args[0]
                self.assertEqual(cmd[:7], ['docker','exec','-i','floppy','python','-c',host.FLOPPY_PROCESS])
                self.assertEqual(run.call_args.kwargs['input'],b'image exporter')
            self.assertEqual(output.read_text(),'previous')
            self.assertEqual(list(Path(tmp).iterdir()),[output])


if __name__ == '__main__':
    unittest.main()
