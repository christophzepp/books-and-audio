import copy
import unittest

import test_podcast_sync as existing
from podcast_sync import choose_source, settings, TICKS
from podcast_status_sync import choose_status, StatusConflict


def state(position=0, status='not_started', missing=False):
    return dict(position=position, status=status, missing=missing, fingerprint='same-file')


class PreferenceTests(unittest.TestCase):
    def test_position_conflict_prefers_each_selected_service(self):
        states = dict(abs=state(40, 'in_progress'), jf=state(80, 'in_progress'),
                      pinepods=state(120, 'in_progress'), gpodder=state(120, 'in_progress'))
        with self.assertRaises(ValueError): choose_source(states, None)
        for primary in ('abs', 'jf', 'pinepods'):
            self.assertEqual(choose_source(states, None, primary_source=primary), primary)

    def test_unchanged_primary_does_not_override_new_backup_progress(self):
        before = {key: state(40, 'in_progress') for key in ('abs', 'jf', 'pinepods', 'gpodder')}
        after = copy.deepcopy(before); after['abs'] = state(80, 'in_progress')
        self.assertEqual(choose_source(after, before, primary_source='pinepods'), 'abs')

    def test_missing_or_initial_zero_primary_never_erases_progress(self):
        for missing in (False, True):
            states = dict(abs=state(40, 'in_progress'), jf=state(80, 'in_progress'),
                          pinepods=state(missing=missing))
            with self.assertRaises(ValueError): choose_source(states, None, primary_source='pinepods')

    def test_preference_does_not_bypass_identity_or_disappeared_state(self):
        before = {key: state(40, 'in_progress') for key in ('abs', 'jf', 'pinepods')}
        for patch in (dict(fingerprint='other-file'), dict(missing=True)):
            after = copy.deepcopy(before); after['pinepods'].update(patch)
            with self.assertRaises(ValueError): choose_source(after, before, primary_source='pinepods')

    def test_explicit_primary_reset_requires_observed_baseline(self):
        before = {key: state(40, 'in_progress') for key in ('abs', 'jf', 'pinepods')}
        after = copy.deepcopy(before); after['pinepods'] = state(); after['abs'] = state(80, 'in_progress')
        self.assertEqual(choose_source(after, before, primary_source='pinepods'), 'pinepods')

    def test_status_only_conflict_uses_preference_without_comparing_seconds(self):
        states = dict(abs=state(200, 'in_progress'), jf=state(200, 'in_progress'),
                      pinepods=state(600, 'finished'), gpodder=state(600, 'finished'))
        with self.assertRaises(StatusConflict): choose_status(states)
        for primary in ('abs', 'jf', 'pinepods'):
            self.assertEqual(choose_status(states, primary_source=primary), primary)
        states['jf']['status'] = 'finished'
        with self.assertRaises(StatusConflict): choose_status(states, primary_source='pinepods')

    def test_unsupported_or_disabled_primary_is_rejected(self):
        for primary in (None, 1, [], {}, 'bo', 'gpodder', 'floppy'):
            with self.assertRaises(ValueError):
                settings(type('Bridge', (), {'config': {'podcasts': {'primary_source': primary}}})())
        with self.assertRaises(ValueError):
            settings(type('Bridge', (), {'config': {'podcasts': {'primary_source': 'pinepods'}}})())


class CoordinatorTests(unittest.TestCase):
    setUp = existing.Tests.setUp
    write_gate = existing.Tests.write_gate
    load = existing.Tests.load
    save = existing.Tests.save
    write_record = existing.Tests.write_record
    cycle = existing.Tests.cycle
    okay = existing.Tests.okay
    add_pine = existing.Tests.add_pine

    def test_primary_pinepods_is_used_by_worker_and_does_not_repeat_writes(self):
        pine = self.add_pine(); self.cfg['podcasts']['primary_source'] = 'pinepods'
        self.okay(self.cycle())
        self.abs.set(40); self.jf.set(40)
        pine.state.update(position=80, status='in_progress', missing=False)
        self.okay(self.cycle())
        self.assertEqual(self.abs.raw['currentTime'], 80)
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'], 80*TICKS)
        before = len(self.abs.writes), len(self.jf.writes), len(pine.writes)
        self.okay(self.cycle())
        self.assertEqual(before, (len(self.abs.writes), len(self.jf.writes), len(pine.writes)))

    def test_primary_jellyfin_initial_conflict_and_new_abs_progress(self):
        self.cfg['podcasts']['primary_source'] = 'jf'
        self.abs.set(40); self.jf.set(80); self.okay(self.cycle())
        self.assertEqual(self.abs.raw['currentTime'], 80)
        self.abs.set(120); self.okay(self.cycle())
        self.assertEqual(self.jf.row['UserData']['PlaybackPositionTicks'], 120*TICKS)


if __name__ == '__main__': unittest.main()
