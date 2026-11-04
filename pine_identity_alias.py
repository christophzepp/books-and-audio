"""Pure validation of explicitly reviewed, episode-specific PinePods aliases.

No I/O, state writes, discovery, enrollment or title matching. A GUID proof is
recorded during separate personal-account DB inspection; runtime revalidates
the exact native IDs/URLs/duration through the official metadata-only API.
Checksums bind the reviewed record; they are not digital signatures or an
authorization mechanism. Only a separately authorized installer may create a
record. Callers must retain its fingerprint in baselines and transfer journals.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from urllib.parse import urlsplit

NAMESPACE = '_pine_alias:'
RECORD_KEYS = {'schema','kind','enabled','revision','mapping_id','source','target',
               'account','source_history','evidence','evidence_sha256'}
SOURCE_KEYS = {'podcast_id','episode_id','abs_library','mapping_fingerprint','jellyfin_fingerprint',
               'feed_url','enclosure_url','guid','duration'}
TARGET_KEYS = {'user_id','podcast_id','episode_id','feed_url','enclosure_url','guid','duration'}
ACCOUNT_KEYS = {'base_url','username','device_id','protocol','server_version',
                'native_user_id','completion_margin_seconds'}
EVIDENCE_KEYS = {'method','observed_at','source_report_sha256','native_report_sha256',
                 'bindings_sha256'}


class AliasError(ValueError):
    """Messages are fixed literals and never contain private URLs or GUIDs."""


def require(value,message):
    if not value:raise AliasError(message)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),
                                    ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def fields(value,keys,message):require(type(value) is dict and set(value)==keys,message)


def text(value,limit=8192):
    require(type(value) is str and 0<len(value)<=limit
            and not any(ord(c)<32 or ord(c)==127 for c in value),'Invalid alias text')
    return value


def identifier(value):
    text(value,200)
    require(not any(c in value for c in '/\\:'),'Invalid alias identifier')
    return value


def positive_id(value):
    require(type(value) is int and 0<value<=2147483647,'Invalid alias native ID')
    return value


def duration(value):
    require(type(value) in (int,float) and math.isfinite(value) and 1<=value<=172800,
            'Invalid alias duration')
    return value


def checksum(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None,
            'Invalid alias evidence checksum')
    return value


def url(value,*,origin=False):
    text(value)
    try:
        parsed=urlsplit(value);parsed.port
    except ValueError:raise AliasError('Invalid alias URL') from None
    require(parsed.scheme in ('http','https') and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.fragment and '\\' not in value
            and not any(c.isspace() for c in value),'Invalid alias URL')
    if origin:require(parsed.path in ('','/') and not parsed.query,'Invalid alias account origin')
    return value


def account_identity(value):
    fields(value,ACCOUNT_KEYS,'Invalid alias account schema')
    url(value['base_url'],origin=True);text(value['username'],255);text(value['device_id'],100)
    require(value['username'].strip()==value['username']
            and not any(c in value['username'] for c in '/:')
            and re.fullmatch('[A-Za-z0-9_-]{1,100}',value['device_id']) is not None,
            'Invalid alias account identity')
    require(value['protocol']=='gpodder-v2' and value['server_version']=='0.9.0',
            'Alias protocol or server version changed')
    require(positive_id(value['native_user_id'])>1,'Alias requires a personal native account')
    require(type(value['completion_margin_seconds']) is int and 1<=value['completion_margin_seconds']<=3600,
            'Invalid alias completion margin')


def binding_evidence_fingerprint(source,target,account,source_history):
    """Used by a separate reviewed installer; does not create an approval."""
    return fingerprint({'source':source,'target':target,'account':account,'source_history':source_history})


def source_history_evidence(source,actions):
    """Pin all action kinds for the exact old URL pair; never merge histories.

    The client must supply its complete, unaggregated since=0 feed result. The
    response's moving server timestamp is deliberately not part of the proof.
    Exact duplicate actions remain in the sorted list and therefore its count.
    """
    require(type(actions) is list and len(actions)<25000,'Incomplete alias source action history')
    selected=[]
    for action in actions:
        require(type(action) is dict,'Invalid alias source action history')
        url(action.get('podcast'));url(action.get('episode'))
        if action['podcast']==source['feed_url'] and action['episode']==source['enclosure_url']:
            text(action.get('action'),30)
            try:selected.append(json.dumps(action,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False))
            except (TypeError,ValueError):raise AliasError('Invalid alias source action history') from None
    return {'count':len(selected),'sha256':fingerprint({'feed_url':source['feed_url'],
        'enclosure_url':source['enclosure_url'],'actions':sorted(selected)})}


def verify_source_history(record,actions):
    require(source_history_evidence(record['source'],actions)==record['source_history'],
            'PinePods alias original-URL history changed; review required')


def validate_record(record):
    fields(record,RECORD_KEYS,'Invalid alias record schema')
    require(type(record['schema']) is int and record['schema']==1
            and record['kind']=='pine_episode_alias','Unsupported alias record')
    require(record['enabled'] is True,'Reviewed PinePods alias is disabled')
    positive_id(record['revision'])
    source,target,account,evidence=(record[k] for k in ('source','target','account','evidence'))
    fields(source,SOURCE_KEYS,'Invalid alias source schema');fields(target,TARGET_KEYS,'Invalid alias target schema')
    account_identity(account)
    for key in ('podcast_id','episode_id','abs_library'):identifier(source[key])
    require(record['mapping_id']=='_podcast:'+source['podcast_id']+':'+source['episode_id'],
            'Alias mapping identity differs')
    for key in ('mapping_fingerprint','jellyfin_fingerprint'):checksum(source[key])
    for key in ('feed_url','enclosure_url'):url(source[key]);url(target[key])
    for key in ('user_id','podcast_id','episode_id'):positive_id(target[key])
    require(target['user_id']==account['native_user_id'] and target['user_id']>1,
            'Alias native account differs')
    text(source['guid']);text(target['guid'])
    require(source['guid']==target['guid'],'Alias lacks exact personal RSS GUID evidence')
    duration(source['duration']);duration(target['duration'])
    require(abs(source['duration']-target['duration'])<=2,'Alias durations differ')
    require((source['feed_url'],source['enclosure_url'])!=(target['feed_url'],target['enclosure_url']),
            'An exact identity needs no alias')
    history=record['source_history']
    fields(history,{'count','sha256'},'Invalid alias source history proof')
    require(type(history['count']) is int and 0<=history['count']<25000,'Invalid alias source history count')
    checksum(history['sha256'])
    fields(evidence,EVIDENCE_KEYS,'Invalid alias evidence schema')
    require(evidence['method']=='personal_native_database_guid','Unreviewed alias evidence method')
    require(type(evidence['observed_at']) in (int,float) and math.isfinite(evidence['observed_at'])
            and 0<evidence['observed_at']<=253402300799,'Invalid alias evidence time')
    for key in ('source_report_sha256','native_report_sha256','bindings_sha256'):checksum(evidence[key])
    require(evidence['bindings_sha256']==binding_evidence_fingerprint(source,target,account,history),
            'Alias evidence does not bind this source and target')
    checksum(record['evidence_sha256'])
    require(record['evidence_sha256']==fingerprint(evidence),'Alias evidence hash changed')
    return record


def state_key(mapping_id):
    text(mapping_id,500)
    parts=mapping_id.split(':')
    require(len(parts)==3 and parts[0]=='_podcast','Invalid alias state identity')
    identifier(parts[1]);identifier(parts[2])
    return NAMESPACE+mapping_id


def candidate_route(record,mapping,*,abs_guid,account):
    """Return a read-only candidate route, not permission to transfer progress.

    The caller may inspect these exact URLs using the existing PinePods client;
    it must then pass that fresh result to resolve_alias before using the route.
    """
    validate_record(record);account_identity(account)
    require(account==record['account'],'PinePods alias account changed')
    require(type(mapping) is dict and type(mapping.get('jf')) is dict,'Alias needs a verified shared-file mapping')
    source=record['source'];target=record['target']
    require(mapping.get('id')==record['mapping_id'],'PinePods alias belongs to another episode')
    require(all(mapping.get(key)==source[key] for key in ('podcast_id','episode_id','abs_library','feed_url','enclosure_url'))
            and mapping.get('fingerprint')==source['mapping_fingerprint']
            and mapping['jf'].get('fingerprint')==source['jellyfin_fingerprint'],
            'PinePods alias source identity changed')
    require(duration(mapping.get('duration'))==source['duration'] and text(abs_guid)==source['guid'],
            'PinePods alias source duration or RSS GUID changed')
    require(not mapping.get('_pine_unavailable'),'PinePods alias source is unavailable or ambiguous')
    return {'native_verified':False,'feed_url':target['feed_url'],'enclosure_url':target['enclosure_url'],
            'duration':source['duration'],'native_duration':target['duration'],
            'native_user_id':target['user_id'],'native_podcast_id':target['podcast_id'],
            'native_episode_id':target['episode_id'],'alias_fingerprint':fingerprint(record),
            'alias_revision':record['revision']}


def resolve_alias(record,mapping,*,abs_guid,account,native):
    """Resolve only after fresh native ownership/URL/duration readback.

    `native` is exactly the inspector shape, not a guessed episode ID or a
    gpodder receipt. Returned URLs are for BOTH native Pine and gpodder calls;
    callers must not overwrite the original ABS mapping with this route.
    """
    route=candidate_route(record,mapping,abs_guid=abs_guid,account=account)
    require(type(native) is dict and type(native.get('metadata')) is dict,
            'PinePods alias needs fresh native metadata')
    meta=native['metadata'];target=record['target']
    for key in ('user_id','podcast_id','episode_id'):
        require(positive_id(native.get(key))==target[key],'PinePods alias native identity changed')
    account_identity(native.get('account'))
    require(native['account']==account,'PinePods alias native account readback changed')
    require(duration(native.get('duration'))==target['duration']
            and duration(native.get('expected_duration'))==route['duration'],
            'PinePods alias native duration changed')
    require(native.get('downloaded') is False and meta.get('is_downloaded') is False,
            'PinePods alias downloaded media needs separate verification')
    require(positive_id(meta.get('episodeid'))==target['episode_id']
            and positive_id(meta.get('podcastid'))==target['podcast_id']
            and meta.get('feedurl')==target['feed_url'] and meta.get('episodeurl')==target['enclosure_url']
            and duration(meta.get('episodeduration'))==target['duration'] and meta.get('is_youtube') is False,
            'PinePods alias native media readback changed')
    if 'episodeguid' in meta:
        require(meta['episodeguid']==target['guid'],'PinePods alias native RSS GUID changed')
    route['native_verified']=True
    return copy.deepcopy(route)
