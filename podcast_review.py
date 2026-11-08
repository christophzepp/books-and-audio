"""Episode-scoped admission to status sync. No media-service writes during review."""
import math

from podcast_status_sync import StatusCoordinator, state_key, version_receipt
from podcast_sync import PodcastCoordinator, encode, fingerprint, guard, has_pine_history, same
from podcast_variant_proof import VariantProof, native_binding


PREFIX = '_pine_transition:'


class ReviewError(ValueError):
    """Only static, user-facing diagnostics belong here."""


def reported_proof(c, mapping, *, refresh=True, force_source=False):
    # A human can explicitly accept the current personal catalog duration. Pin
    # the same file, account, URLs, and native IDs on every subsequent read.
    checker = VariantProof(c.b)
    native = c.pine.inspect_episode_metadata(mapping['feed_url'], mapping['enclosure_url'], mapping['duration'])
    identity = native_binding(native, mapping, c.pine.identity)
    source = checker.source_snapshot(mapping, allow_cached=not refresh and not force_source, identity=identity)
    duration = native['duration']
    if (type(duration) not in (float, int) or not math.isfinite(duration) or not 1 <= duration <= 172800
            or abs(duration - mapping['duration']) <= 2):
        raise ReviewError('PinePods meldet keine verwendbare abweichende Laufzeit.')
    binding = fingerprint([source, identity])
    version = fingerprint([binding, duration])
    return dict(ready=True, fresh=refresh, validated_at=c.b.clock(), source_fingerprint=binding,
                proof_fingerprint=version, evidence_sha256=version, own_duration=duration,
                **{k: identity[k] for k in ('feed_url', 'enclosure_url', 'native_episode_id', 'native_user_id')})


def check_pending(bridge, key):
    keys = (key, state_key(key), '_pine_metadata:' + key)
    if (any(bridge.load(k).get('pending') for k in keys) or bridge.load(keys[2]).get('stage') == 'intent'
            or bridge.db.execute("SELECT 1 FROM operations WHERE book IN (?,?,?) AND status='pending'", keys).fetchone()):
        raise ReviewError('Für diese Folge ist noch eine Übertragung offen. Zuerst den laufenden Auftrag klären.')


def inspect(c, mapping, *, automatic=False):
    b, key = c.b, mapping['id']
    check_pending(b, key)
    if not c.pine or mapping.get('_pine_unavailable') or c.alias_record(mapping) is not None:
        raise ReviewError('Diese Folge hat keine eindeutig bestätigte PinePods-Zuordnung.')
    if mapping['jf']['id'] in c.jf.active_ids():
        raise ReviewError('Die Folge läuft gerade in Jellyfin. Nach der Wiedergabe erneut prüfen.')
    core, status = b.load(key), b.load(state_key(key))
    prior = b.load(PREFIX + key)
    full = not prior and (has_pine_history(core) or key in c.history)
    local_id = fingerprint([mapping['fingerprint'], mapping['jf']['fingerprint']])
    if core.get('mapping', local_id) != local_id or (full and not core.get('pine_raw')):
        raise ReviewError('Der frühere Positionsabgleich lässt sich dieser Fassung nicht sicher zuordnen.')
    proof = c.variant_proof(mapping, refresh=True, apply=automatic)
    if proof.get('ready') and proof.get('fresh'):
        mode = prior.get('mode', 'measured')
    elif automatic or status.get('binding') or prior:
        raise ReviewError('Die Audiofassung wird automatisch erneut geprüft. Ein aktueller Nachweis fehlt noch.')
    else:
        proof = reported_proof(c, mapping)
        mode = 'reported'
    mapping = dict(mapping, _pine_selected=False)
    sc = StatusCoordinator(c)
    binding = sc.binding(mapping, proof)
    if status.get('binding', binding) != binding:
        raise ReviewError('Die Identität der früher freigegebenen Folge hat sich geändert.')
    states, raw = sc.read(mapping, proof)
    if not same(states['abs'], states['jf']):
        raise ReviewError('Die lokalen Hörstände in Audiobookshelf und Jellyfin weichen voneinander ab. Diese zuerst abgleichen.')
    if full:
        old = core['pine_raw']
        previous = core.get('baseline', {})
        pine_fingerprint = fingerprint([mapping['feed_url'], mapping['enclosure_url'], mapping['duration'], c.accounts['pinepods']])
        if (old.get('native_episode_id') != proof['native_episode_id'] or old.get('native_user_id') != proof['native_user_id']
                or set(previous) != set(states) or previous['pinepods'].get('fingerprint') != pine_fingerprint
                or any(previous[k].get('fingerprint') != states[k].get('fingerprint') for k in ('abs','jf'))):
            raise ReviewError('Die aktuelle Folge stimmt nicht mit der früheren PinePods-Zuordnung überein.')
    previous = status.get('baseline') or core.get('baseline', {})
    if any(v.get('status') != 'not_started' and not v.get('missing') and states[k].get('missing')
           for k,v in previous.items() if k in states):
        raise ReviewError('Ein früher vorhandener Hörstand ist verschwunden. Bitte den betroffenen Dienst prüfen.')
    if automatic and (not full or status or len({v['status'] for v in states.values()}) != 1):
        raise ReviewError('Die vorhandenen Hörstände erfordern eine Entscheidung.')
    signature = fingerprint([key, binding, version_receipt(proof), mode, raw, status, prior,
                             {k:v for k,v in core.items() if k != 'last_error'}])
    public = dict(episode_id=key, title=mapping['title'], show=mapping['show'], duration=mapping['duration'],
                  pine_duration=proof['own_duration'], mode=mode, digest=signature,
                  states={k:v['status'] for k,v in states.items()},
                  sources=[k for k,v in states.items() if not v.get('missing') or len({s['status'] for s in states.values()}) == 1])
    return public, (core, status, prior, local_id, binding, proof, states, raw)


def commit(c, mapping, public, snapshot, source, automatic):
    b, key = c.b, mapping['id']
    core, status, prior, local_id, binding, proof, states, raw = snapshot
    if source not in public['sources']:
        raise ReviewError('Bitte einen vorhandenen Hörstand als Quelle wählen.')
    note = prior or dict(mode=public['mode'], approved_by='automatic' if automatic else 'user',
                         binding=binding, version=version_receipt(proof), previous_core=core)
    if not automatic:
        note = dict(note, approved_by='user')
    new_core = dict(mapping=local_id, baseline={k:states[k] for k in ('abs','jf')}, pine_raw=None)
    new_status = dict(schema=2, binding=binding, version=version_receipt(proof), baseline=states, raw=raw, enrolled=True)
    if len(set(public['states'].values())) > 1:
        new_status['reviewed_source'] = dict(peer=source, snapshot=fingerprint(raw))
    # Worker owns the coordinator DB. Recheck state under a write lock on
    # commit so a stale preview/retry cannot erase a transfer or a new baseline.
    with b.db:
        b.db.execute('BEGIN IMMEDIATE')
        check_pending(b, key)
        if (b.load(key), b.load(state_key(key)), b.load(PREFIX + key)) != (core, status, prior):
            raise ReviewError('Die Folge wurde inzwischen geändert. Bitte erneut prüfen.')
        for k,v in ((PREFIX+key,note),(key,new_core),(state_key(key),new_status)):
            b.db.execute('INSERT OR REPLACE INTO state(id,value) VALUES(?,?)',(k,encode(v)))
    return states


def review(bridge, key, *, record=None, source=None):
    if not isinstance(key,str) or not key.startswith('_podcast:') or len(key)>1200:
        raise ReviewError('Ungültige Folge.')
    guard(bridge)
    c = PodcastCoordinator(bridge)
    c.account_guard(False)
    mappings, _ = c.mappings()
    m = next((m for m in mappings if m['id']==key), None)
    if m is None:
        raise ReviewError('Die Folge ist nicht mehr eindeutig in beiden lokalen Bibliotheken vorhanden.')
    public, snapshot = inspect(c, m)
    if record is not None:
        if public['digest'] != record['digest']:
            raise ReviewError('Laufzeit, Zuordnung oder Hörstand hat sich geändert. Bitte erneut prüfen.')
        commit(c, m, public, snapshot, source, False)
    return public
