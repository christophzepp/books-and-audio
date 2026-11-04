import copy
import json
import unittest
from unittest.mock import patch

import pine_identity_alias as alias


def fixture():
    mapping={'id':'_podcast:pod:ep','podcast_id':'pod','episode_id':'ep','abs_library':'library',
        'fingerprint':'a'*64,'jf':{'fingerprint':'b'*64},'feed_url':'https://show.example/feed',
        'enclosure_url':'https://media.example/audio.mp3?rss_browser=ABS-PRIVATE','duration':1000.4}
    account={'base_url':'http://pinepods:8040','username':'reader','device_id':'book-progress-sync',
             'protocol':'gpodder-v2','server_version':'0.9.0','native_user_id':2,'completion_margin_seconds':60}
    source={k:mapping[k] for k in ('podcast_id','episode_id','abs_library','feed_url','enclosure_url','duration')}
    source.update(mapping_fingerprint=mapping['fingerprint'],jellyfin_fingerprint=mapping['jf']['fingerprint'],guid='PRIVATE-RSS-GUID')
    target={'user_id':2,'podcast_id':18,'episode_id':123,'feed_url':source['feed_url'],
            'enclosure_url':'https://media.example/audio.mp3?rss_browser=PINE-PRIVATE',
            'duration':1000,'guid':'PRIVATE-RSS-GUID'}
    history=alias.source_history_evidence(source,[])
    evidence={'method':'personal_native_database_guid','observed_at':1793550000,
              'source_report_sha256':'c'*64,'native_report_sha256':'d'*64,
              'bindings_sha256':alias.binding_evidence_fingerprint(source,target,account,history)}
    record={'schema':1,'kind':'pine_episode_alias','enabled':True,'revision':1,'mapping_id':mapping['id'],
            'source':source,'target':target,'account':account,'source_history':history,'evidence':evidence,
            'evidence_sha256':alias.fingerprint(evidence)}
    native={'episode_id':123,'user_id':2,'podcast_id':18,'duration':1000,'expected_duration':1000.4,
            'account':copy.deepcopy(account),'downloaded':False,'position':None,'completed':False,
            'metadata':{'episodeid':123,'podcastid':18,'feedurl':target['feed_url'],
                'episodeurl':target['enclosure_url'],'episodeduration':1000,'is_youtube':False,
                'is_downloaded':False,'completed':False,'listenduration':None}}
    return record,mapping,account,native


def rehash(record):
    record['evidence']['bindings_sha256']=alias.binding_evidence_fingerprint(
        record['source'],record['target'],record['account'],record['source_history'])
    record['evidence_sha256']=alias.fingerprint(record['evidence'])


class AliasTests(unittest.TestCase):
    def setUp(self):self.record,self.mapping,self.account,self.native=fixture()

    def candidate(self):
        return alias.candidate_route(self.record,self.mapping,abs_guid='PRIVATE-RSS-GUID',account=self.account)

    def resolve(self):
        return alias.resolve_alias(self.record,self.mapping,abs_guid='PRIVATE-RSS-GUID',account=self.account,native=self.native)

    def test_exact_reviewed_different_query_route_preserves_original_abs_mapping(self):
        before=copy.deepcopy((self.record,self.mapping,self.account,self.native))
        candidate=self.candidate();self.assertFalse(candidate['native_verified'])
        result=self.resolve();self.assertTrue(result['native_verified'])
        self.assertEqual(result['enclosure_url'],self.record['target']['enclosure_url'])
        self.assertEqual(result['feed_url'],self.record['target']['feed_url'])
        self.assertEqual(result['alias_fingerprint'],alias.fingerprint(self.record))
        self.assertEqual(result['native_episode_id'],123)
        self.assertEqual(before,(self.record,self.mapping,self.account,self.native))
        result['feed_url']='changed';self.assertEqual(self.record,before[0])

    def test_strict_record_schema_boolean_revision_and_evidence_are_required(self):
        for update in ({'schema':True},{'schema':2},{'enabled':1},{'enabled':False},{'revision':True},
                       {'revision':0},{'extra':'ignored?'},{'kind':'feed_alias'},
                       {'evidence_sha256':'0'*64},{'mapping_id':'_podcast:other:ep'}):
            with self.subTest(update=update),self.assertRaises(alias.AliasError):
                alias.validate_record(dict(self.record,**update))

    def test_changed_binding_requires_new_evidence_even_when_top_hash_unchanged(self):
        for part,key,value in [('target','enclosure_url','https://media.example/audio.mp3?rss_browser=THIRD'),
                              ('target','episode_id',124),('source','mapping_fingerprint','e'*64)]:
            changed=copy.deepcopy(self.record);changed[part][key]=value
            with self.subTest(part=part,key=key),self.assertRaises(alias.AliasError):alias.validate_record(changed)

    def test_no_guid_or_fuzzy_title_substitution_can_authorize_alias(self):
        for changed in ('',None,'OTHER-GUID'):
            record=copy.deepcopy(self.record);record['target']['guid']=changed;rehash(record)
            with self.subTest(changed=changed),self.assertRaises(alias.AliasError):alias.validate_record(record)
        record=copy.deepcopy(self.record);record['source']['title']='Same title';rehash(record)
        with self.assertRaises(alias.AliasError):alias.validate_record(record)

    def test_current_source_file_jellyfin_url_and_guid_must_all_match(self):
        original=copy.deepcopy(self.mapping)
        for key,value in [('fingerprint','e'*64),('feed_url','https://show.example/other'),
                          ('enclosure_url',self.record['target']['enclosure_url']),('duration',1000.5),
                          ('jf',{'fingerprint':'f'*64}),('_pine_unavailable','source_identity_ambiguous')]:
            self.mapping=dict(original,**{key:value})
            with self.subTest(key=key),self.assertRaises(alias.AliasError):self.candidate()
        self.mapping=original
        with self.assertRaises(alias.AliasError):
            alias.candidate_route(self.record,self.mapping,abs_guid='DIFFERENT',account=self.account)

    def test_same_query_pairs_reordered_are_not_silently_accepted_as_exact_current_source(self):
        self.mapping['enclosure_url']='https://media.example/audio.mp3?b=2&a=1'
        self.record['source']['enclosure_url']='https://media.example/audio.mp3?a=1&b=2'
        rehash(self.record)
        with self.assertRaises(alias.AliasError):self.candidate()

    def test_native_identity_ownership_version_download_and_duration_drift_block(self):
        original=copy.deepcopy(self.native)
        changes=[{'user_id':3},{'user_id':True},{'episode_id':124},{'podcast_id':17},
                 {'duration':1001},{'expected_duration':1000},{'downloaded':True},
                 {'account':dict(self.account,server_version='0.9.1')},
                 {'account':dict(self.account,native_user_id=3)}]
        for update in changes:
            self.native=dict(original,**update)
            with self.subTest(update=update),self.assertRaises(alias.AliasError):self.resolve()

    def test_native_metadata_exact_target_and_optional_guid_rechecked(self):
        original=copy.deepcopy(self.native)
        for key,value in [('episodeurl',self.mapping['enclosure_url']),('feedurl','https://show.example/new'),
                          ('episodeid',124),('podcastid',17),('episodeduration',1001),
                          ('is_youtube',True),('is_downloaded',True),('episodeguid','CHANGED')]:
            self.native=copy.deepcopy(original);self.native['metadata'][key]=value
            with self.subTest(key=key),self.assertRaises(alias.AliasError):self.resolve()

    def test_personal_account_and_two_second_duration_guard_cannot_be_bypassed(self):
        for change in ({'user_id':1},{'duration':1003},{'duration':True},{'duration':float('nan')}):
            record=copy.deepcopy(self.record);record['target'].update(change)
            if not any(type(v) is float and v!=v for v in change.values()):rehash(record)
            with self.subTest(change=change),self.assertRaises(alias.AliasError):alias.validate_record(record)
        self.account=dict(self.account,username='other')
        with self.assertRaises(alias.AliasError):self.candidate()

    def test_feed_migration_routes_native_and_gpodder_together_only_for_reviewed_episode(self):
        self.record['target']['feed_url']='https://other.publisher.example/reviewed-feed'
        self.native['metadata']['feedurl']=self.record['target']['feed_url'];rehash(self.record)
        resolved=self.resolve()
        self.assertEqual(resolved['feed_url'],self.record['target']['feed_url'])
        self.mapping=dict(self.mapping,id='_podcast:pod:new-episode',episode_id='new-episode')
        with self.assertRaises(alias.AliasError):self.candidate()

    def test_errors_never_echo_private_urls_guids_or_parameter_values(self):
        self.record['target']['guid']='MISMATCH'
        with self.assertRaises(alias.AliasError) as error:self.resolve()
        for private in ('PRIVATE','https://','rss_browser','MISMATCH'):
            self.assertNotIn(private,str(error.exception))

    def test_module_performs_no_file_or_network_operations(self):
        with patch('builtins.open',side_effect=AssertionError('Unexpected I/O')):
            self.resolve()
        self.assertEqual(alias.state_key(self.mapping['id']),'_pine_alias:_podcast:pod:ep')
        for value in ('_podcast:pod:../ep','_podcast:pod:ep:other','_book:pod:ep'):
            with self.assertRaises(alias.AliasError):alias.state_key(value)


if __name__=='__main__':unittest.main()
