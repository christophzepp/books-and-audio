"""Keep observations of unresolved identities on the state volume, separate from transfer journals."""
import json

from maintenance_settings import podcast_window

PREFIX = '_podcast_conflict:'
CATALOG_REASONS = {'feed_missing', 'feed_ambiguous', 'episode_missing', 'episode_ambiguous',
                   'episode_identity_unresolved'}
SOURCE_REASONS = {'source_identity_ambiguous', 'source_identity_missing'}


class PodcastConflicts:
    def __init__(self, coordinator, apply):
        self.c = coordinator
        self.b = coordinator.b
        self.apply = apply
        self.entries = {key[len(PREFIX):]: json.loads(value) for key, value in self.b.db.execute(
            'SELECT id,value FROM state WHERE id GLOB ?', (PREFIX + '*',))}
        active, self.window = podcast_window(self.b.config, self.b.clock())
        # Oldest receipts get the next turn even if a large backlog spans
        # multiple short windows. Ties retain the previous cycle's order.
        eligible = (key for key, row in self.entries.items() if row.get('window') != self.window)
        self.due = set(sorted(eligible, key=lambda key:self.entries[key].get('window') or '')[:3]) if active else set()

    def discard(self, key):
        if self.apply and self.entries.pop(key, None) is not None:
            with self.b.db:
                self.b.db.execute('DELETE FROM state WHERE id=?', (PREFIX + key,))

    def pending(self, key, core, status):
        from podcast_status_sync import state_key
        meta = '_pine_metadata:' + key
        return (any(core.get(k) for k in ('pending', 'candidate', 'manual_choice'))
                or any(status.get(k) for k in ('pending', 'candidate', 'reviewed_source'))
                or self.b.load(meta).get('stage') == 'intent'
                or self.b.db.execute("SELECT 1 FROM operations WHERE book IN (?,?,?) AND status='pending' LIMIT 1",
                                     (key, state_key(key), meta)).fetchone())

    def signature(self, mapping, reason):
        """Use the current cycle's personal service observations. Fail open to actual checks on uncertainty."""
        from pinepods_client import EpisodeUnavailable
        from podcast_status_sync import state_key
        from podcast_sync import fingerprint, same
        c, key = self.c, mapping['id']
        core = self.b.load(key)
        status = self.b.load(state_key(key))
        if self.pending(key, core, status) or not c.pine:
            return None
        alias = self.b.load('_pine_alias:' + key)
        transition = self.b.load('_pine_transition:' + key)
        inputs = [mapping['fingerprint'], mapping['jf']['fingerprint'], mapping.get('abs_guid'),
                  mapping.get('feed_url'), mapping.get('enclosure_url'), mapping.get('_pine_unavailable'),
                  mapping['title'], c.accounts, c.cfg, core, status, alias, transition]
        with c.observing():
            states, local = c.read(dict(mapping, _pine_selected=False))
            # Re-sampling after a run may race a new local playback. Never
            # memoize an unprocessed local edit (including a missing row).
            baseline = core.get('baseline') or {}
            for peer in ('abs', 'jf'):
                if peer in baseline and (not same(states[peer], baseline[peer])
                        or states[peer].get('missing') != baseline[peer].get('missing')):
                    return None
            if reason == 'variant_identity_changed' and not same(states['abs'], states['jf']):
                return None
            inputs.append(local)
            if reason in SOURCE_REASONS:
                return fingerprint(inputs) if reason == mapping.get('_pine_unavailable') else None
            if reason == 'alias_missing':
                return fingerprint(inputs) if not alias else None
            if getattr(c.pine, '_observation_only', False) is not True:
                return None
            if reason in ('duration_mismatch', 'variant_identity_changed'):
                inputs.extend([self.b.load('_pine_variant_proof:' + key), self.b.load('_pine_metadata:' + key)])
            route = None
            try:
                if transition.get('mode') == 'personal_route':
                    route = transition['route']
                    if 'old_actions' in route:
                        inputs.append(c.pine._episode_actions(mapping['feed_url'], mapping['enclosure_url']))
                else:
                    route = c.pine_route(dict(mapping))
                feed, enclosure = route['feed_url'], route['enclosure_url']
                native = c.pine.inspect_episode_metadata(feed, enclosure, mapping['duration'])
                if reason == 'duration_mismatch' and native['duration'] != 0:
                    # A real alternate cut still uses the dedicated duration/proof queue.
                    return None
                if reason in CATALOG_REASONS:
                    return None  # A missing or ambiguous identity has just become readable.
                inputs.extend([native, c.pine._episode_actions(feed, enclosure)])
            except EpisodeUnavailable as exc:
                if exc.reason != reason:
                    return None
                inputs.append(exc.reason)
                if exc.reason.startswith('episode_') and route is not None:
                    user = c.pine._ensure_native()
                    _, by_url, _ = c.pine._native_catalog(feed, user)
                    _, rows = c.pine._catalog_data[(feed, user)]
                    keys = ('Episodeid', 'Episodeurl', 'Episodetitle', 'Episodepubdate', 'Episodeduration')
                    candidates = set(by_url.get(enclosure, ())) | set(by_url.get('', ()))
                    inputs.append([[rows[i].get(k) for k in keys] for i in sorted(candidates)])
        return fingerprint(inputs)

    def observe(self, mapping, reason):
        # Unsupported/failed provider reads cannot justify using an old blocked row.
        from abs_client import RequestError as AbsRequestError
        from pinepods_client import RequestError
        try:
            return self.signature(mapping, reason)
        except (AbsRequestError, RequestError, RuntimeError, ValueError, KeyError):
            return None

    def remembered(self, mapping):
        entry = self.entries.get(mapping['id'])
        if not self.apply or not entry:
            return None
        if mapping['id'] in self.due and podcast_window(self.b.config, self.b.clock()) == (True, self.window):
            # Claim on the NVMe before opening any media. A restart may not
            # retry an unchanged structural conflict throughout the same night.
            entry['window'] = self.window
            self.b.save(PREFIX + mapping['id'], entry)
            return None
        signature = self.observe(mapping, entry['reason'])
        if signature is None or signature != entry['signature']:
            return None
        return dict(entry['row'])

    def record(self, mapping, row):
        if not self.apply:
            return
        reason = mapping.get('_structural_conflict') or mapping.get('_pine_reason_code')
        if row.get('progress_status') not in ('unchanged', 'blocked') or reason not in (
                CATALOG_REASONS | SOURCE_REASONS | {'alias_missing', 'duration_mismatch', 'variant_identity_changed'}):
            self.discard(mapping['id'])
            return
        signature = self.observe(mapping, reason)
        if signature is None:
            self.discard(mapping['id'])
            return
        active, date = podcast_window(self.b.config, self.b.clock())
        row['can_retry_conflict'] = True
        value = dict(reason=reason, signature=signature, row=dict(row), window=date if active else None)
        key = mapping['id']
        if value != self.entries.get(key):
            self.b.save(PREFIX + key, value)
            self.entries[key] = value


def valid_retry(data):
    if not isinstance(data, dict) or set(data) != {'type', 'id'} or not isinstance(data['id'], str):
        return False
    if data['type'] == 'listenarr':
        key = data['id']
        return key.isascii() and key.isdigit() and not key.startswith('0') and len(key) <= 20
    if data['type'] != 'podcast':
        return False
    parts = data['id'].split(':')
    return (len(parts) == 3 and parts[0] == '_podcast'
            and all(1 <= len(value) <= 200 and all(ch not in '/\\' and ord(ch) >= 32 for ch in value)
                    for value in parts[1:]))
