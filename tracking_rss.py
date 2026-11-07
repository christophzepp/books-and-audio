"""Exact RSS identities for the original Floppy GPodder importer.

The optional ``url`` identity is the feed's own GUID. Stock Floppy already
indexes that value as an episode candidate, including non-URL RSS GUIDs.
No title or duration matching and no media requests are performed here.
"""
from __future__ import annotations
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit
import requests

MAX_FEED = 16 * 1024 * 1024


def valid_url(value):
    try:
        p=urlsplit(value)
        return (isinstance(value,str) and len(value)<=8192 and p.scheme in ('https','http')
                and p.hostname and not p.username and not p.password and not p.fragment
                and not any(ord(c)<33 or ord(c)==127 for c in value))
    except (ValueError,TypeError):return False


def parse_feed(body):
    if not isinstance(body,bytes) or len(body)>MAX_FEED or b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
        raise ValueError('tracking_invalid_feed')
    try:root=ET.fromstring(body)
    except ET.ParseError:raise ValueError('tracking_invalid_feed') from None
    entries=[]
    for node in root.iter():
        if node.tag.rsplit('}',1)[-1] not in ('item','entry'):continue
        guid=None;urls=[]
        for child in node:
            tag=child.tag.rsplit('}',1)[-1]
            if tag in ('guid','id') and child.text:
                guid=child.text.strip()
            if tag=='enclosure':urls.append(child.attrib.get('url'))
            if tag=='link' and child.attrib.get('rel')=='enclosure':urls.append(child.attrib.get('href'))
        urls=list(dict.fromkeys(u for u in urls if valid_url(u)))
        if len(urls)==1:
            if guid is not None and (not guid or len(guid)>8192 or any(ord(c)<32 for c in guid)):
                raise ValueError('tracking_invalid_feed_identity')
            entries.append({'episode':urls[0],'guid':guid})
    if len(entries)>100000:raise ValueError('tracking_feed_too_large')
    return entries


class TrackingResolver:
    def __init__(self, aliases=(), request=None):
        self.aliases=list(aliases);self.request=request;self.cache={}

    def begin_poll(self):
        # A feed may contain a new episode or a rotated enclosure on the next
        # poll. Cache only within one batch, never for the process lifetime.
        self.cache.clear()

    def _fetch(self,feed):
        if not valid_url(feed):raise ValueError('tracking_invalid_feed_url')
        session=requests.Session();session.trust_env=False
        try:
            url=feed
            for _ in range(5):
                response=(self.request or session.request)('GET',url,headers={'User-Agent':'Books-Audio-Tracking/2'},
                    timeout=(5,20),allow_redirects=False,stream=True)
                try:
                    if response.status_code in (301,302,303,307,308):
                        url=urljoin(url,response.headers.get('Location',''))
                        if not valid_url(url):raise ValueError('tracking_invalid_feed_redirect')
                        continue
                    if response.status_code!=200:raise ValueError('tracking_feed_unavailable')
                    chunks=[];size=0
                    for chunk in response.iter_content(65536):
                        size+=len(chunk)
                        if size>MAX_FEED:raise ValueError('tracking_feed_too_large')
                        chunks.append(chunk)
                    return parse_feed(b''.join(chunks))
                finally:response.close()
            raise ValueError('tracking_feed_redirect_limit')
        except requests.RequestException:raise ValueError('tracking_feed_unavailable') from None
        finally:session.close()

    def resolve(self,action):
        feed=action['podcast'];url=action['episode']
        if feed not in self.cache:self.cache[feed]=self._fetch(feed)
        entries=self.cache[feed]
        matches=[x for x in entries if x['episode']==url]
        if action.get('guid') and matches and any(x.get('guid') != action['guid'] for x in matches):
            raise ValueError('tracking_rss_identity_changed')
        if not matches:
            evidence=[a for a in self.aliases if a.get('feed')==feed and a.get('old')==url]
            guids={a.get('guid') for a in evidence if a.get('guid')}
            if action.get('guid'):guids.add(action['guid'])
            new_urls={a.get('new') for a in evidence if a.get('new')}
            matches=[x for x in entries if x['episode'] in new_urls or x.get('guid') in guids]
        if len(matches)!=1:raise ValueError('tracking_rss_identity_unresolved')
        match=matches[0];result=dict(action,episode=match['episode'])
        if match.get('guid'):
            if sum(x.get('guid')==match['guid'] for x in entries)!=1:raise ValueError('tracking_rss_identity_ambiguous')
            result['guid']=match['guid'];result['url']=match['guid']
        return result
