"""Reuse a reviewed feed move for exact new enclosure URLs, without title matching.

The existing per-episode GUID reviews establish the feed pair and personal
podcast ID. They do not authorize another enclosure or discard old actions.
No configuration, subscription, media or playback state is written here.
"""
import json

from pine_identity_alias import AliasError, validate_record, source_history_evidence


class ReviewedFeeds:
    def __init__(self, bridge, pine):
        self.b, self.pine = bridge, pine
        self.records = None
        self.pods = None

    def resolve(self, mapping):
        original = mapping['feed_url']
        if self.records is None:
            self.records = []
            for key, raw in self.b.db.execute("SELECT id,value FROM state WHERE id LIKE '_pine_alias:%'"):
                try:
                    row = json.loads(raw)
                    validate_record(row)
                    if key != '_pine_alias:' + row['mapping_id']:
                        continue
                    self.records.append(row)
                except (ValueError, TypeError, KeyError):
                    continue
        anchors = [r for r in self.records
                   if r['source']['podcast_id'] == mapping['podcast_id']
                   and r['source']['feed_url'] == original
                   and r['target']['feed_url'] != original
                   and r['account'] == self.pine.identity]
        if not anchors:
            return original
        if self.pods is None:
            user = self.pine._ensure_native()
            self.pods = self.pine._request('GET', '/api/data/return_pods/' + str(user), native=True)['pods']
            if (not isinstance(self.pods, list) or len(self.pods) > 10000
                    or any(not isinstance(p, dict) or type(p.get('podcastid')) is not int
                           or p['podcastid'] <= 0 or not isinstance(p.get('feedurl'), str) for p in self.pods)
                    or len({p['podcastid'] for p in self.pods}) != len(self.pods)):
                raise AliasError('Personal podcast feed inventory is incomplete')
        # Coexisting editions stay distinct, even when a historical review
        # happens to connect one pair of episodes from them.
        if any(p['feedurl'] == original for p in self.pods):
            return original
        pairs = {(r['target']['podcast_id'], r['target']['feed_url']) for r in anchors}
        if len(pairs) != 1:
            raise AliasError('Reviewed podcast feed move is ambiguous')
        podcast_id, target = pairs.pop()
        hits = [p for p in self.pods if p['podcastid'] == podcast_id and p['feedurl'] == target]
        if len(hits) != 1 or sum(p['feedurl'] == target for p in self.pods) != 1:
            raise AliasError('Reviewed personal podcast feed is unavailable')
        # An old namespace with any action needs explicit reconciliation.
        # Never silently replace a previous listen, reset or deletion.
        if source_history_evidence(mapping, self.pine.actions(original)['actions'])['count']:
            raise AliasError('Original podcast feed has episode actions requiring review')
        native = self.pine.inspect_episode_metadata(target, mapping['enclosure_url'], mapping['duration'])
        if (native['podcast_id'] != podcast_id or native['user_id'] != self.pine.identity['native_user_id']
                or native['metadata']['episodeurl'] != mapping['enclosure_url']):
            raise AliasError('Reviewed feed does not contain the exact personal episode')
        for anchor in self.records:
            if (anchor['mapping_id'] != mapping['id'] and anchor['target']['episode_id'] == native['episode_id']
                    and anchor['target']['user_id'] == native['user_id']):
                raise AliasError('Personal podcast episode already belongs to another mapping')
        for key, raw in self.b.db.execute("SELECT id,value FROM state WHERE id LIKE '_podcast:%' OR id LIKE '_podcast_status:%'"):
            if key in (mapping['id'], '_podcast_status:' + mapping['id']):
                continue
            value = json.loads(raw)
            old = value.get('pine_raw') or (value.get('raw') or {}).get('pinepods') or {}
            if (old.get('native_episode_id') == native['episode_id']
                    and old.get('native_user_id') == native['user_id']):
                raise AliasError('Personal podcast episode already has another listening history')
        previous = self.b.load(mapping['id']).get('pine_raw') or {}
        if previous and (previous.get('native_episode_id') != native['episode_id']
                         or previous.get('native_user_id') != native['user_id']):
            raise AliasError('Previously enrolled podcast episode identity changed')
        mapping['_pine_reviewed_feed'] = True
        return target
