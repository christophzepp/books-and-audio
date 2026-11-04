"""Pure deployment-policy and mocked-process tests; no Docker/server access."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import watch_deployment as watcher


ABS_ID = '00000000-0000-4000-8000-000000000042'
IMAGE = 'sha256:' + 'c' * 64


def row():
    return {'abs_id': 'bookorbit:14', 'audio_source': 'BookOrbit',
            'audio_source_id': '14', 'audio_provider_book_id': '14',
            'ebook_source': 'BookOrbit', 'ebook_source_id': '57',
            'user_id': 1, 'sync_mode': 'audiobook'}


def config(preparation=True):
    result = {'deployment': {
        'audiobookshelf': {'image': 'sha256:' + 'a' * 64, 'media_source': '/media/audiobooks'},
        'bookorbit': {'image': 'sha256:' + 'b' * 64, 'media_source': '/media/audiobooks', 'version': '3.5.0'},
    }, 'mappings': [{'abs_id': ABS_ID, 'bo_id': 14}],
        'paused_mappings': {ABS_ID: 'Vorbereitung des begleiteten E-Books'}}
    if preparation:
        result['bookbridge_policy'] = {'phase': 'preparation', 'image': IMAGE,
                                      'paused_abs_id': ABS_ID, 'mapping': row()}
    return result


def containers(preparation=True):
    cfg = config(preparation)
    result = []
    for name in ('audiobookshelf', 'bookorbit'):
        result.append({'Name': '/' + name, 'Image': cfg['deployment'][name]['image'],
                       'State': {'Status': 'running'},
                       'Mounts': [{'Destination': '/audiobooks', 'Source': '/media/audiobooks'}],
                       'Config': {'Env': ['APP_VERSION=3.5.0']}})
    if preparation:
        result.append({'Name': '/bookbridge', 'Image': IMAGE, 'State': {'Status': 'running'}})
    return result


def native_config():
    cfg = config()
    cfg['mappings'][0]['folder'] = '/audiobooks/Example'
    cfg['bookbridge_policy'] = {'phase': 'native_readalong', 'image': IMAGE,
                               'abs_id': ABS_ID, 'mapping': row()}
    cfg['native_readalong'] = {ABS_ID: {'bo_id': 14, 'overlay_file_id': 81,
                                     'overlay_path': '/audiobooks/Example/readalong.epub',
                                     'overlay_size': 12345}}
    cfg['paused_mappings'] = {}
    return cfg


def native_snapshot():
    return {'count': 1, 'rows': [row()],
            'writer_checks': {key: True for key in watcher.WRITER_CHECK_FIELDS}}


def native_containers():
    result = containers()
    result[-1]['Config'] = {'Env': []}
    return result


class PolicyTests(unittest.TestCase):
    def test_import_has_no_io(self):
        spec = importlib.util.spec_from_file_location('isolated_watcher', watcher.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch('subprocess.run', side_effect=AssertionError('process at import')), \
                patch.object(Path, 'read_text', side_effect=AssertionError('read at import')), \
                patch.object(Path, 'write_text', side_effect=AssertionError('write at import')):
            spec.loader.exec_module(module)
        self.assertTrue(callable(module.main))

    def test_default_policy_preserves_zero_mapping_rule(self):
        cfg = config(False)
        self.assertIsNone(watcher.preparation_policy(cfg))
        watcher.validate_bookbridge_snapshot({'count': 0, 'rows': []}, cfg)
        for snapshot in ({'count': 1, 'rows': []}, {'count': 1, 'rows': [row()]},
                         {'count': 0, 'rows': [row()]}):
            with self.subTest(snapshot=snapshot), self.assertRaises(watcher.DeploymentError):
                watcher.validate_bookbridge_snapshot(snapshot, cfg)

    def test_preparation_accepts_empty_or_exact_reviewed_mapping(self):
        for snapshot in ({'count': 0, 'rows': []}, {'count': 1, 'rows': [row()]}):
            watcher.validate_bookbridge_snapshot(snapshot, config())

    def test_int_and_decimal_string_ids_are_equivalent(self):
        actual = row()
        actual.update(audio_source_id=14, audio_provider_book_id=14, ebook_source_id=57, user_id='1')
        watcher.validate_bookbridge_snapshot({'count': 1, 'rows': [actual]}, config())

    def test_each_supported_mode_must_match_explicit_expectation(self):
        for mode in watcher.SYNC_MODES:
            cfg, actual = config(), row()
            cfg['bookbridge_policy']['mapping']['sync_mode'] = mode
            actual['sync_mode'] = mode
            watcher.validate_bookbridge_snapshot({'count': 1, 'rows': [actual]}, cfg)
            actual['sync_mode'] = next(x for x in watcher.SYNC_MODES if x != mode)
            with self.assertRaises(watcher.DeploymentError):
                watcher.validate_bookbridge_snapshot({'count': 1, 'rows': [actual]}, cfg)

    def test_each_identity_field_is_checked(self):
        changes = {'abs_id': 'bookorbit:15', 'audio_source': 'Audiobookshelf',
                   'audio_source_id': '15', 'audio_provider_book_id': '15',
                   'ebook_source': 'Other', 'ebook_source_id': '58', 'user_id': 2,
                   'sync_mode': 'audiobook_only'}
        for field, value in changes.items():
            actual = row()
            actual[field] = value
            with self.subTest(field=field), self.assertRaises(watcher.DeploymentError):
                watcher.validate_bookbridge_snapshot({'count': 1, 'rows': [actual]}, config())

    def test_unknown_missing_duplicate_and_incomplete_rows_fail_closed(self):
        unknown = dict(row(), credential='must not be accepted')
        missing = row(); del missing['audio_provider_book_id']
        for snapshot in ({'count': 2, 'rows': [row(), row()]}, {'count': 1, 'rows': []},
                         {'count': 0, 'rows': [row()]}, {'count': 1, 'rows': [unknown]},
                         {'count': 1, 'rows': [missing]}, {'count': 1, 'rows': [None]}):
            with self.subTest(snapshot=snapshot), self.assertRaises(watcher.DeploymentError):
                watcher.validate_bookbridge_snapshot(snapshot, config())

    def test_snapshot_schema_rejects_type_confusion_and_extra_fields(self):
        for snapshot in (None, [], {}, {'count': True, 'rows': []}, {'count': '0', 'rows': []},
                         {'count': -1, 'rows': []}, {'count': 0, 'rows': None},
                         {'count': 0, 'rows': [], 'extra': 1}):
            with self.subTest(snapshot=snapshot), self.assertRaises(watcher.DeploymentError):
                watcher.validate_bookbridge_snapshot(snapshot, config())

    def test_identity_normalization_rejects_noncanonical_numbers(self):
        for value in (None, True, False, 14.0, 0, -1, '014', '14.0', ' 14', '14 ', '+14', ''):
            actual = row(); actual['audio_source_id'] = value
            with self.subTest(value=value), self.assertRaises(watcher.DeploymentError):
                watcher.normalize_bookbridge_row(actual)

    def test_invalid_policy_phase_keys_and_image_are_rejected(self):
        variants = [None, {}, [], dict(config()['bookbridge_policy'], phase='native'),
                    dict(config()['bookbridge_policy'], extra='unsafe'),
                    dict(config()['bookbridge_policy'], image='bookbridge:latest')]
        missing = config()['bookbridge_policy']; del missing['mapping']
        variants.append(missing)
        for policy in variants:
            cfg = config(); cfg['bookbridge_policy'] = policy
            with self.subTest(policy=policy), self.assertRaises(watcher.DeploymentError):
                watcher.preparation_policy(cfg)

    def test_pause_must_be_present_valid_and_for_corresponding_pair(self):
        for pauses in (None, {}, [], {ABS_ID: ''}, {ABS_ID: '   '}, {ABS_ID: True},
                       {ABS_ID: 'line\nbreak'}, {'different': 'pause'}):
            cfg = config(); cfg['paused_mappings'] = pauses
            with self.subTest(pauses=pauses), self.assertRaises(watcher.DeploymentError):
                watcher.preparation_policy(cfg)
        for mappings in ([], [{'abs_id': ABS_ID, 'bo_id': 15}],
                         [{'abs_id': ABS_ID, 'bo_id': 14}] * 2, [None]):
            cfg = config(); cfg['mappings'] = mappings
            with self.subTest(mappings=mappings), self.assertRaises(watcher.DeploymentError):
                watcher.preparation_policy(cfg)

    def test_empty_row_count_does_not_bypass_pause_requirement(self):
        cfg = config(); cfg['paused_mappings'] = {}
        with self.assertRaises(watcher.DeploymentError):
            watcher.validate_bookbridge_snapshot({'count': 0, 'rows': []}, cfg)

    def test_expected_row_itself_must_be_consistent_and_bookorbit_only(self):
        for field, value in (('audio_provider_book_id', '15'), ('audio_source', 'bookorbit'),
                             ('abs_id', 'an-abs-id'), ('sync_mode', 'bidirectional')):
            cfg = config(); cfg['bookbridge_policy']['mapping'][field] = value
            with self.subTest(field=field), self.assertRaises(watcher.DeploymentError):
                watcher.preparation_policy(cfg)


class NativePolicyTests(unittest.TestCase):
    def test_native_handoff_accepts_retained_mapping_without_preparation_pause(self):
        cfg = native_config()
        before = copy.deepcopy(cfg)
        watcher.validate_containers(native_containers(), cfg)
        watcher.validate_bookbridge_snapshot(native_snapshot(), cfg)
        self.assertEqual(cfg, before)

    def test_native_requires_exactly_one_retained_mapping(self):
        for count, rows in ((0, []), (1, []), (2, [row(), row()])):
            snapshot = native_snapshot(); snapshot.update(count=count, rows=rows)
            with self.subTest(count=count, rows=rows), self.assertRaises(watcher.DeploymentError):
                watcher.validate_bookbridge_snapshot(snapshot, native_config())

    def test_native_mapping_keeps_every_preparation_identity_field(self):
        changes = {'abs_id': 'bookorbit:15', 'audio_source': 'Audiobookshelf',
                   'audio_source_id': '15', 'audio_provider_book_id': '15',
                   'ebook_source': 'Other', 'ebook_source_id': '58', 'user_id': 2,
                   'sync_mode': 'audiobook_only'}
        for field, value in changes.items():
            snapshot = native_snapshot(); snapshot['rows'][0][field] = value
            with self.subTest(field=field), self.assertRaises(watcher.DeploymentError):
                watcher.validate_bookbridge_snapshot(snapshot, native_config())

    def test_native_policy_rejects_unknown_fields_phase_and_unpinned_image(self):
        cfg = native_config()
        for policy in (dict(cfg['bookbridge_policy'], phase='other'),
                       dict(cfg['bookbridge_policy'], phase='native'),
                       dict(cfg['bookbridge_policy'], paused_abs_id=ABS_ID),
                       dict(cfg['bookbridge_policy'], image='bookbridge:latest')):
            bad = native_config(); bad['bookbridge_policy'] = policy
            with self.subTest(policy=policy), self.assertRaises(watcher.DeploymentError):
                watcher.bookbridge_policy(bad)
        bad = native_config(); del bad['bookbridge_policy']['abs_id']
        with self.assertRaises(watcher.DeploymentError):
            watcher.bookbridge_policy(bad)

    def test_native_requires_unique_corresponding_abs_audio_pair(self):
        for mappings in (None, [], [None], [{'abs_id': 'other', 'bo_id': 14}],
                         [{'abs_id': ABS_ID, 'bo_id': 15}],
                         native_config()['mappings'] * 2):
            cfg = native_config(); cfg['mappings'] = mappings
            with self.subTest(mappings=mappings), self.assertRaises(watcher.DeploymentError):
                watcher.bookbridge_policy(cfg)
        for identity in (None, True, '', ' ' + ABS_ID, ABS_ID + '\n'):
            cfg = native_config(); cfg['bookbridge_policy']['abs_id'] = identity
            with self.subTest(identity=identity), self.assertRaises(watcher.DeploymentError):
                watcher.bookbridge_policy(cfg)

    def test_native_requires_matching_valid_overlay_pin(self):
        for pins in (None, [], {}, {'other': native_config()['native_readalong'][ABS_ID]}):
            cfg = native_config(); cfg['native_readalong'] = pins
            with self.subTest(pins=pins), self.assertRaises(watcher.DeploymentError):
                watcher.bookbridge_policy(cfg)
        changes = {'bo_id': 15, 'overlay_file_id': True, 'overlay_size': 0,
                   'overlay_path': '/audiobooks/Other/readalong.epub', 'extra': 'secret'}
        for field, value in changes.items():
            cfg = native_config(); cfg['native_readalong'][ABS_ID][field] = value
            with self.subTest(field=field), self.assertRaises(watcher.DeploymentError):
                watcher.bookbridge_policy(cfg)
        cfg = native_config(); del cfg['mappings'][0]['folder']
        with self.assertRaises(watcher.DeploymentError):
            watcher.bookbridge_policy(cfg)

    def test_every_writer_check_must_be_explicit_boolean_true(self):
        for field in watcher.WRITER_CHECK_FIELDS:
            for value in (False, None, 0, 1, 'true', 'secret credential'):
                snapshot = native_snapshot(); snapshot['writer_checks'][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(watcher.DeploymentError):
                    watcher.validate_bookbridge_snapshot(snapshot, native_config())
            snapshot = native_snapshot(); del snapshot['writer_checks'][field]
            with self.subTest(missing=field), self.assertRaises(watcher.DeploymentError):
                watcher.validate_bookbridge_snapshot(snapshot, native_config())
        for checks in (None, [], dict(native_snapshot()['writer_checks'], secret='not allowed')):
            snapshot = native_snapshot(); snapshot['writer_checks'] = checks
            with self.subTest(checks=checks), self.assertRaises(watcher.DeploymentError):
                watcher.validate_bookbridge_snapshot(snapshot, native_config())

    def test_native_writer_evidence_cannot_be_omitted_or_added_to_preparation(self):
        with self.assertRaises(watcher.DeploymentError):
            watcher.validate_bookbridge_snapshot({'count': 1, 'rows': [row()]}, native_config())
        with self.assertRaises(watcher.DeploymentError):
            watcher.validate_bookbridge_snapshot(native_snapshot(), config())


class ContainerTests(unittest.TestCase):
    def test_original_and_preparation_container_checks_pass(self):
        watcher.validate_containers(containers(False), config(False))
        watcher.validate_containers(containers(), config())

    def test_missing_extra_or_duplicate_containers_are_not_accepted(self):
        good = containers()
        for actual in ([], good[:-1], good + [good[0]], [good[0], good[0], good[2]], None):
            with self.subTest(actual=actual), self.assertRaises(watcher.DeploymentError):
                watcher.validate_containers(actual, config())

    def test_each_pinned_image_and_running_state_is_checked(self):
        for index in range(3):
            for field in ('image', 'state'):
                actual = containers()
                if field == 'image': actual[index]['Image'] = 'sha256:' + 'f' * 64
                else: actual[index]['State']['Status'] = 'exited'
                with self.subTest(index=index, field=field), self.assertRaises(watcher.DeploymentError):
                    watcher.validate_containers(actual, config())

    def test_media_mounts_and_bookorbit_version_remain_checked(self):
        for index in range(2):
            actual = containers(); actual[index]['Mounts'][0]['Source'] = '/wrong'
            with self.assertRaises(watcher.DeploymentError):
                watcher.validate_containers(actual, config())

    def test_native_environment_must_not_supply_annotation_credentials(self):
        for env in ([], ['BOOKORBIT_KOSYNC_USER=', 'BOOKORBIT_KOSYNC_KEY='],
                    ['ABS_ENABLED=true', 'BOOKORBIT_ENABLED=true']):
            actual = native_containers(); actual[-1]['Config']['Env'] = env
            watcher.validate_containers(actual, native_config())
        for key in ('BOOKORBIT_KOSYNC_USER', 'BOOKORBIT_KOSYNC_KEY'):
            for env in ([key + '=secret'], [key + '=', key + '=secret'], [key], [key + '= ']):
                actual = native_containers(); actual[-1]['Config']['Env'] = env
                with self.subTest(env=env), self.assertRaises(watcher.DeploymentError):
                    watcher.validate_containers(actual, native_config())
        # The existing preparation phase deliberately keeps its old behavior.
        actual = containers(); actual[-1]['Config'] = {'Env': ['BOOKORBIT_KOSYNC_KEY=secret']}
        watcher.validate_containers(actual, config())

    def test_native_environment_evidence_cannot_be_missing_or_malformed(self):
        for settings in (None, {}, {'Env': None}, {'Env': 'secret'}, {'Env': [1]}):
            actual = native_containers(); actual[-1]['Config'] = settings
            with self.subTest(settings=settings), self.assertRaises(watcher.DeploymentError):
                watcher.validate_containers(actual, native_config())
        with self.assertRaises(watcher.DeploymentError):
            watcher.validate_containers(containers(), native_config())
            actual = containers(); actual[index]['Mounts'] *= 2
            with self.assertRaises(watcher.DeploymentError):
                watcher.validate_containers(actual, config())
        for env in ([], ['APP_VERSION=3.6.0'], ['APP_VERSION=3.5.0'] * 2, None, [1]):
            actual = containers(); actual[1]['Config']['Env'] = env
            with self.subTest(env=env), self.assertRaises(watcher.DeploymentError):
                watcher.validate_containers(actual, config())


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        (self.root / 'state').mkdir()

    def runner(self, preparation=True, rows=None):
        return Mock(side_effect=[SimpleNamespace(stdout=json.dumps(containers(preparation))),
                                 SimpleNamespace(stdout=json.dumps({'count': len(rows or []), 'rows': rows or []}))])

    def run_main(self, cfg, runner):
        (self.root / 'config/config.json').write_text(json.dumps(cfg))
        code = watcher.main(self.root, runner=runner, clock=lambda: 1000)
        return code, json.loads((self.root / 'state/deployment.json').read_text())

    def test_main_writes_private_atomic_success_gate(self):
        runner = self.runner(rows=[row()])
        code, gate = self.run_main(config(), runner)
        self.assertEqual(code, 0)
        self.assertEqual(gate, {'checked_at': 1000, 'ok': True, 'bookbridge_ok': False,
                                'podcasts_ok': False, 'podcasts_reason': 'Podcast deployment is not configured',
                                'pinepods_ok': False, 'pinepods_reason': 'PinePods image is not pinned',
                                'bookbridge_reason': 'BookBridge deployment is not configured',
                                'folder_gate_ok': False, 'folder_reason': 'Folder deployment is not configured'})
        self.assertEqual((self.root / 'state/deployment.json').stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / 'state/deployment.tmp').exists())
        self.assertEqual(runner.call_args_list[0].args[0],
                         ['docker', 'inspect', 'audiobookshelf', 'bookorbit', 'bookbridge'])
        for call in runner.call_args_list:
            self.assertTrue(call.kwargs['check'])
            self.assertEqual(call.kwargs['timeout'], 20)

    def test_default_uses_original_two_container_inspection(self):
        runner = self.runner(False)
        code, gate = self.run_main(config(False), runner)
        self.assertEqual(code, 0)
        self.assertTrue(gate['ok'])
        self.assertEqual(runner.call_args_list[0].args[0], ['docker', 'inspect', 'audiobookshelf', 'bookorbit'])

    def test_native_main_uses_boolean_snapshot_and_writes_no_settings_to_gate(self):
        runner = Mock(side_effect=[SimpleNamespace(stdout=json.dumps(native_containers())),
                                 SimpleNamespace(stdout=json.dumps(native_snapshot()))])
        code, gate = self.run_main(native_config(), runner)
        self.assertEqual(code, 0)
        self.assertEqual(gate, {'checked_at': 1000, 'ok': True, 'bookbridge_ok': False,
                                'podcasts_ok': False, 'podcasts_reason': 'Podcast deployment is not configured',
                                'pinepods_ok': False, 'pinepods_reason': 'PinePods image is not pinned',
                                'bookbridge_reason': 'BookBridge deployment is not configured',
                                'folder_gate_ok': False, 'folder_reason': 'Folder deployment is not configured'})
        self.assertEqual(runner.call_args_list[1].args[0], watcher.snapshot_command(True, 1))

    def test_native_rejection_never_echoes_credentials(self):
        snapshot = native_snapshot(); snapshot['writer_checks']['global_abs_disabled'] = 'secret credential'
        runner = Mock(side_effect=[SimpleNamespace(stdout=json.dumps(native_containers())),
                                 SimpleNamespace(stdout=json.dumps(snapshot))])
        code, gate = self.run_main(native_config(), runner)
        self.assertEqual(code, 1)
        self.assertFalse(gate['ok'])
        self.assertNotIn('secret', json.dumps(gate))

    def test_native_environment_failure_never_echoes_value_or_reads_database(self):
        actual = native_containers()
        actual[-1]['Config']['Env'] = ['BOOKORBIT_KOSYNC_KEY=secret credential']
        runner = Mock(return_value=SimpleNamespace(stdout=json.dumps(actual)))
        code, gate = self.run_main(native_config(), runner)
        self.assertEqual(code, 1)
        self.assertFalse(gate['ok'])
        self.assertNotIn('secret', json.dumps(gate))
        self.assertEqual(runner.call_count, 1)

    def test_native_invalid_pin_fails_before_any_process(self):
        cfg = native_config(); cfg['native_readalong'][ABS_ID]['bo_id'] = 15
        runner = Mock()
        code, gate = self.run_main(cfg, runner)
        self.assertEqual(code, 1)
        self.assertFalse(gate['ok'])
        runner.assert_not_called()

    def test_invalid_policy_stops_before_any_process(self):
        cfg = config(); cfg['paused_mappings'] = {}
        runner = Mock()
        code, gate = self.run_main(cfg, runner)
        self.assertEqual(code, 1)
        self.assertFalse(gate['ok'])
        runner.assert_not_called()

    def test_failed_or_malformed_process_never_yields_partial_success(self):
        failures = [subprocess.CalledProcessError(1, ['secret-command'], output='secret stdout', stderr='secret stderr'),
                    subprocess.TimeoutExpired(['secret-command'], 20),
                    RuntimeError('secret arbitrary failure')]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                runner = self.runner()
                runner.side_effect = [SimpleNamespace(stdout=json.dumps(containers())), failure]
                code, gate = self.run_main(config(), runner)
                self.assertEqual(code, 1)
                self.assertFalse(gate['ok'])
                self.assertNotIn('secret', json.dumps(gate))
        for output in ('not json secret', '{}', 'null', '{"count":1,"rows":[]}'):
            runner = Mock(side_effect=[SimpleNamespace(stdout=json.dumps(containers())), SimpleNamespace(stdout=output)])
            code, gate = self.run_main(config(), runner)
            self.assertEqual(code, 1)
            self.assertFalse(gate['ok'])
            self.assertNotIn('secret', json.dumps(gate))

    def test_failed_container_snapshot_prevents_database_command(self):
        runner = Mock(return_value=SimpleNamespace(stdout='[]'))
        code, gate = self.run_main(config(), runner)
        self.assertEqual(code, 1)
        self.assertFalse(gate['ok'])
        self.assertEqual(runner.call_count, 1)

    def test_snapshot_script_reads_only_selected_fields_without_changing_sqlite(self):
        database = self.root / 'database.db'
        conn = sqlite3.connect(database)
        columns = ','.join(sorted(watcher.ROW_FIELDS))
        conn.execute('CREATE TABLE books (' + columns + ',secret_token)')
        values = [row()[name] for name in sorted(watcher.ROW_FIELDS)] + ['never output me']
        conn.execute('INSERT INTO books VALUES (' + ','.join('?' for _ in values) + ')', values)
        conn.commit(); conn.close()
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        command = watcher.snapshot_command(True)
        self.assertEqual(command[:5], ['docker', 'exec', 'bookbridge', 'python', '-c'])
        script = command[-1].replace('file:/data/database.db?mode=ro', database.as_uri() + '?mode=ro')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(script, {})
        result = json.loads(output.getvalue())
        watcher.validate_bookbridge_snapshot(result, config())
        self.assertNotIn('never output me', output.getvalue())
        self.assertEqual(set(result['rows'][0]), watcher.ROW_FIELDS)
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)
        self.assertIn("c.execute('BEGIN')", script)
        self.assertIn('mode=ro', script)
        self.assertIn('query_only=ON', script)

    def test_native_sqlite_snapshot_emits_only_checks_and_never_changes_database(self):
        database = self.root / 'native.db'
        conn = sqlite3.connect(database)
        columns = ','.join(sorted(watcher.ROW_FIELDS))
        conn.execute('CREATE TABLE books (' + columns + ')')
        values = [row()[name] for name in sorted(watcher.ROW_FIELDS)]
        conn.execute('INSERT INTO books VALUES (' + ','.join('?' for _ in values) + ')', values)
        conn.execute('CREATE TABLE settings (key,value)')
        conn.execute('CREATE TABLE user_credentials (user_id,key,value)')
        for key in ('ABS_ENABLED', 'BOOKORBIT_ENABLED'):
            conn.execute('INSERT INTO settings VALUES (?,?)', (key, 'false'))
            conn.execute('INSERT INTO user_credentials VALUES (?,?,?)', (1, key, 'false'))
        for key in ('ABS_KEY', 'BOOKORBIT_PASSWORD'):
            conn.execute('INSERT INTO settings VALUES (?,?)', (key, 'never output me'))
            conn.execute('INSERT INTO user_credentials VALUES (?,?,?)', (1, key, 'never output me'))
        conn.commit()
        script = watcher.snapshot_command(True, 1)[-1].replace(
            'file:/data/database.db?mode=ro', database.as_uri() + '?mode=ro')

        def read_snapshot():
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(script, {})
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)
            self.assertNotIn('never output me', output.getvalue())
            snapshot = json.loads(output.getvalue())
            self.assertTrue(all(type(v) is bool for v in snapshot['writer_checks'].values()))
            return snapshot

        try:
            watcher.validate_bookbridge_snapshot(read_snapshot(), native_config())
            for table in ('settings', 'user_credentials'):
                for key in ('ABS_ENABLED', 'BOOKORBIT_ENABLED'):
                    for value in ('true', '', None):
                        conn.execute('UPDATE ' + table + ' SET value=? WHERE key=?', (value, key)); conn.commit()
                        with self.subTest(table=table, key=key, value=value), self.assertRaises(watcher.DeploymentError):
                            watcher.validate_bookbridge_snapshot(read_snapshot(), native_config())
                    conn.execute('UPDATE ' + table + ' SET value=? WHERE key=?', ('false', key)); conn.commit()
            # Even another user's credential can activate the independent
            # annotation relay, so it must be included in the absence check.
            for key in ('BOOKORBIT_KOSYNC_USER', 'BOOKORBIT_KOSYNC_KEY'):
                for table in ('settings', 'user_credentials'):
                    if table == 'settings':
                        conn.execute('INSERT INTO settings VALUES (?,?)', (key, 'encrypted:never output me'))
                    else:
                        conn.execute('INSERT INTO user_credentials VALUES (?,?,?)', (2, key, 'encrypted:never output me'))
                    conn.commit()
                    with self.subTest(table=table, key=key), self.assertRaises(watcher.DeploymentError):
                        watcher.validate_bookbridge_snapshot(read_snapshot(), native_config())
                    conn.execute('DELETE FROM ' + table + ' WHERE key=?', (key,)); conn.commit()
            conn.execute("DELETE FROM user_credentials WHERE key='ABS_ENABLED'"); conn.commit()
            with self.assertRaises(watcher.DeploymentError):
                watcher.validate_bookbridge_snapshot(read_snapshot(), native_config())
        finally:
            conn.close()

    def test_native_snapshot_rejects_unsafe_or_missing_user_id(self):
        for user_id in (True, '1; secret', 0, '01'):
            with self.subTest(user_id=user_id), self.assertRaises(watcher.DeploymentError):
                watcher.snapshot_command(True, user_id)
        with self.assertRaises(watcher.DeploymentError):
            watcher.snapshot_command(False, 1)


if __name__ == '__main__':
    unittest.main()
