"""Bounded MP3 duration evidence for metadata repair, never progress writes.

The caller enforces a wall-clock deadline in a subprocess, including DNS.
Publisher URLs enter via stdin only; no credential store or application client
is imported. Only structural frame-count headers provide duration evidence.
Matching duration still does not prove equal content or advertisement layout.
"""
import datetime as dt
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import shutil
import signal
import stat
import sys
import tempfile
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit

sys.dont_write_bytecode=True
import requests

MAX_PREFIX=1024*1024
MAX_MEDIA=256*1024*1024
MIN_FREE=1024*1024*1024
CACHE_MAX_AGE=30*86400


def etag_evidence(value):
    """Retain a digest only; weak/malformed validators never enable reuse."""
    if value is None:return {'etag_kind':'missing','strong_etag_sha256':None}
    if not isinstance(value,str):return {'etag_kind':'invalid','strong_etag_sha256':None}
    if value.startswith('W/'):return {'etag_kind':'weak','strong_etag_sha256':None}
    if not re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]{0,1022}"',value):
        return {'etag_kind':'invalid','strong_etag_sha256':None}
    return {'etag_kind':'strong','strong_etag_sha256':hashlib.sha256(value.encode('utf-8')).hexdigest()}


def public_url(url):
    if (not isinstance(url,str) or not 1<=len(url)<=8192 or '\\' in url
            or any(c.isspace() or ord(c)<32 or ord(c)==127 for c in url)):
        raise ValueError('invalid_public_url')
    parsed=urlsplit(url)
    if (parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or parsed.port not in (None,80,443)
            or '%' in parsed.hostname):
        raise ValueError('invalid_public_url')
    host=parsed.hostname
    try:
        address=ipaddress.ip_address(host)
        records=socket.getaddrinfo(host,parsed.port or (443 if parsed.scheme=='https' else 80),
                                  type=socket.SOCK_STREAM,flags=socket.AI_NUMERICHOST)
        addresses=[address]
    except ValueError:
        records=socket.getaddrinfo(host,parsed.port or (443 if parsed.scheme=='https' else 80),type=socket.SOCK_STREAM)
        addresses=[ipaddress.ip_address(row[4][0]) for row in records]
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError('nonpublic_source')
    return records


@contextmanager
def pinned_dns(url, records):
    """Pin the validated addresses for this hop inside the isolated worker.

    Request URL/Host/TLS name are retained, so certificate verification stays
    enabled. A second DNS resolution cannot redirect to a private address.
    """
    parsed=urlsplit(url); expected_port=parsed.port or (443 if parsed.scheme=='https' else 80)
    def canonical(host):
        if isinstance(host,bytes):host=host.decode('ascii')
        return host.encode('idna').decode('ascii').lower().rstrip('.')
    expected_host=canonical(parsed.hostname)
    original=socket.getaddrinfo
    def pinned(host,port,family=0,type=0,proto=0,flags=0):
        if canonical(host)!=expected_host or int(port)!=expected_port:
            raise ValueError('unexpected_dns_target')
        selected=[row for row in records if (not family or row[0]==family) and (not type or row[1]==type)
                  and (not proto or row[2]==proto)]
        if not selected:raise ValueError('validated_dns_family_unavailable')
        return selected
    socket.getaddrinfo=pinned
    try:yield
    finally:socket.getaddrinfo=original


def integer(data):
    return int.from_bytes(data, 'big')


def frame(data, offset):
    """Decode only MPEG Layer III, rejecting reserved/free-format headers."""
    if offset < 0 or offset + 4 > len(data): return None
    bits = integer(data[offset:offset + 4])
    version, layer = (bits >> 19) & 3, (bits >> 17) & 3
    bitrate, rate = (bits >> 12) & 15, (bits >> 10) & 3
    if bits >> 21 != 0x7ff or version == 1 or layer != 1 or bitrate in (0, 15) or rate == 3:
        return None
    rates = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
    kbps = ((0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
            if version == 3 else (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160))[bitrate]
    samples = 1152 if version == 3 else 576
    frequency = rates[version][rate]
    length = (samples * kbps * 1000 // (8 * frequency)) + ((bits >> 9) & 1)
    return dict(version=version, sample_rate=frequency, samples=samples,
                frame_bytes=length, mono=((bits >> 6) & 3) == 3)


def header_duration(data, total_bytes=None):
    """Header evidence only, never a complete stream/identity validation."""
    unknown = dict(known=False, reason='no_valid_frame_count_header', full_stream_verified=False)
    start = 0
    while data[start:start + 3] == b'ID3':
        if start + 10 > len(data) or any(x & 128 for x in data[start + 6:start + 10]):
            return dict(unknown, reason='invalid_or_truncated_id3')
        size = 0
        for value in data[start + 6:start + 10]: size = (size << 7) | value
        footer = 10 if data[start + 3] == 4 and data[start + 5] & 16 else 0
        start += 10 + size + footer
        if start >= len(data): return dict(unknown, reason='id3_exceeds_prefix')
    for offset in range(start, min(start + 8192, len(data) - 4)):
        first = frame(data, offset)
        if first is None: continue
        at = offset
        chain = []
        for _ in range(3):
            part = frame(data, at)
            if part is None or (part['version'], part['sample_rate']) != (first['version'], first['sample_rate']): break
            chain.append(part); at += part['frame_bytes']
        if len(chain) != 3: continue
        end = offset + first['frame_bytes']
        xoffset = offset + (21 if first['mono'] else 36) if first['version'] == 3 else offset + (13 if first['mono'] else 21)
        marker, frames, byte_count = None, None, None
        if data[xoffset:xoffset + 4] in (b'Xing', b'Info'):
            if xoffset + 12 > min(end, len(data)): return dict(unknown, reason='truncated_frame_count_tag')
            flags = integer(data[xoffset + 4:xoffset + 8])
            if flags & ~15 or not flags & 1: return dict(unknown, reason='invalid_or_missing_frame_count_flag')
            cursor = xoffset + 8
            frames = integer(data[cursor:cursor + 4]); cursor += 4
            if flags & 2:
                if cursor + 4 > min(end, len(data)): return dict(unknown, reason='truncated_frame_count_tag')
                byte_count = integer(data[cursor:cursor + 4]); cursor += 4
            if flags & 4: cursor += 100
            if flags & 8: cursor += 4
            if cursor > min(end, len(data)): return dict(unknown, reason='truncated_frame_count_tag')
            marker = data[xoffset:xoffset + 4].decode('ascii')
        elif data[offset + 36:offset + 40] == b'VBRI':
            cursor = offset + 36
            if cursor + 26 > min(end, len(data)): return dict(unknown, reason='truncated_vbri')
            if integer(data[cursor + 4:cursor + 6]) != 1: return dict(unknown, reason='unsupported_vbri_version')
            byte_count = integer(data[cursor + 10:cursor + 14])
            frames = integer(data[cursor + 14:cursor + 18])
            entries = integer(data[cursor + 18:cursor + 20])
            width = integer(data[cursor + 22:cursor + 24])
            if not entries or width not in (2, 4) or cursor + 26 + entries * width > min(end, len(data)):
                return dict(unknown, reason='invalid_vbri_table')
            marker = 'VBRI'
        if marker is None:
            return dict(unknown, reason='mpeg_audio_without_frame_count_tag')
        duration = frames * first['samples'] / first['sample_rate']
        if not frames or not 1 <= duration <= 172800:
            return dict(unknown, reason='implausible_frame_count')
        if byte_count is not None:
            # A zero or impossible byte declaration cannot validate a tag.
            if byte_count <= first['frame_bytes'] or total_bytes is not None and byte_count + offset > total_bytes:
                return dict(unknown, reason='frame_count_bytes_contradict_response')
            bitrate = byte_count * 8 / duration
            if not 4000 <= bitrate <= 400000:
                return dict(unknown, reason='implausible_declared_bitrate')
            if total_bytes is not None and total_bytes - offset - byte_count > 128:
                # A preserved old Xing header after added audio must not be
                # mistaken for the duration of the served body. At most one
                # standard ID3v1 footer is allowed; larger tags stay unknown.
                return dict(unknown, reason='declared_audio_bytes_do_not_cover_response',
                    candidate_frame_count_duration_seconds=round(duration, 6),
                    bytes_outside_declared_audio=total_bytes - offset - byte_count)
        return dict(known=True, source=marker + '_frame_count', duration_seconds=round(duration, 6),
            frame_count=frames, sample_rate=first['sample_rate'], audio_offset=offset,
            declared_audio_bytes=byte_count, total_response_bytes=total_bytes,
            response_bytes_after_declared_audio=total_bytes - offset - byte_count
                if total_bytes is not None and byte_count is not None else None,
            encoder_delay_adjusted=False, full_stream_verified=False)
    return unknown


def fetch_prefix(url, *, max_bytes=MAX_PREFIX, seconds=9):
    if type(max_bytes) is not int or not 4096 <= max_bytes <= MAX_PREFIX: raise ValueError('Invalid read bound')
    deadline = time.monotonic() + seconds
    session = requests.Session(); session.trust_env = False; session.auth = None
    session.headers.clear()
    session.headers.update({'User-Agent': 'PodcastDurationDiagnostic/1.0', 'Accept-Encoding': 'identity',
                            'Range': 'bytes=0-' + str(max_bytes - 1)})
    try:
        for hops in range(4):
            if time.monotonic() >= deadline: raise TimeoutError('Header deadline')
            records=public_url(url); session.cookies.clear()
            with pinned_dns(url,records), session.get(url, allow_redirects=False, stream=True, timeout=(3, 4)) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('Location')
                    if not location: raise ValueError('Missing redirect')
                    following = urljoin(url, location)
                    if urlsplit(url).scheme == 'https' and urlsplit(following).scheme != 'https':
                        raise ValueError('HTTPS downgrade refused')
                    url = following; continue
                if response.status_code not in (200, 206): raise ValueError('Header unavailable')
                mime = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                if mime not in ('audio/mpeg', 'audio/mp3', 'application/octet-stream') or response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                    raise ValueError('Unsupported media encoding')
                total = None; expected_prefix = None
                length=response.headers.get('Content-Length','')
                declared_length=int(length) if re.fullmatch(r'[0-9]{1,12}',length) else None
                if response.status_code == 206:
                    match = re.fullmatch(r'bytes 0-([0-9]{1,12})/([0-9]{1,12})', response.headers.get('Content-Range', ''))
                    if not match: raise ValueError('Invalid response range')
                    last, total = map(int, match.groups())
                    if not 0 <= last < max_bytes or total <= last: raise ValueError('Unexpected response range')
                    expected_prefix = last + 1
                    if declared_length is not None and declared_length!=expected_prefix:
                        raise ValueError('Contradicting response length')
                else:
                    length = response.headers.get('Content-Length', '')
                    if re.fullmatch(r'[0-9]{1,12}', length): total = int(length)
                    if total == 0: raise ValueError('Empty media')
                    expected_prefix = min(total, max_bytes) if total is not None else None
                chunks = []; count = 0
                for block in response.iter_content(chunk_size=16384):
                    if time.monotonic() >= deadline: raise TimeoutError('Header deadline')
                    part = block[:max_bytes - count]
                    chunks.append(part); count += len(part)
                    if count == max_bytes: break
                if count == 0 or expected_prefix is not None and count != expected_prefix:
                    raise ValueError('Incomplete media prefix')
                return b''.join(chunks), dict(bytes_read=count, response_total_bytes=total,
                    content_length_header=declared_length,
                    **etag_evidence(response.headers.get('ETag')),
                    range_honoured=response.status_code == 206, redirects=hops,
                    fetched_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                    source_url_sha256=hashlib.sha256(url.encode()).hexdigest(),
                    complete_body_read=total is not None and count == total)
        raise ValueError('Redirect limit')
    finally: session.close()


def download_full(url,directory):
    """One bounded full candidate in caller-owned private temporary storage."""
    if (directory.is_symlink() or not directory.is_dir() or directory.resolve()!=directory
            or directory.stat().st_mode & 0o077 or shutil.disk_usage(directory).free<MIN_FREE):
        raise ValueError('temporary_storage_unavailable')
    session=requests.Session();session.trust_env=False;session.auth=None
    session.headers.clear();session.headers.update({'User-Agent':'PodcastDurationDiagnostic/1.0','Accept-Encoding':'identity'})
    path=directory/'candidate.mp3';deadline=time.monotonic()+44
    try:
        for hops in range(4):
            if time.monotonic()>=deadline:raise TimeoutError()
            records=public_url(url);session.cookies.clear()
            with pinned_dns(url,records),session.get(url,allow_redirects=False,stream=True,timeout=(3,4)) as response:
                if response.status_code in (301,302,303,307,308):
                    location=response.headers.get('Location')
                    if not location:raise ValueError('missing_redirect')
                    following=urljoin(url,location)
                    if urlsplit(url).scheme=='https' and urlsplit(following).scheme!='https':raise ValueError('https_downgrade')
                    url=following;continue
                if response.status_code!=200:raise ValueError('media_unavailable')
                mime=response.headers.get('Content-Type','').split(';',1)[0].strip().lower()
                if mime not in ('audio/mpeg','audio/mp3','application/octet-stream') or response.headers.get('Content-Encoding','identity').lower()!='identity':
                    raise ValueError('unsupported_media')
                length=response.headers.get('Content-Length','')
                total=int(length) if re.fullmatch(r'[0-9]{1,12}',length) else None
                if total is not None and not 0<total<=MAX_MEDIA:raise ValueError('media_size_limit')
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                digest=hashlib.sha256();prefix_digest=hashlib.sha256();count=0;prefix_count=0
                with os.fdopen(fd,'wb') as handle:
                    for block in response.iter_content(chunk_size=65536):
                        if time.monotonic()>=deadline:raise TimeoutError()
                        count+=len(block)
                        if count>MAX_MEDIA:raise ValueError('media_size_limit')
                        handle.write(block);digest.update(block)
                        prefix=block[:MAX_PREFIX-prefix_count]
                        if prefix:prefix_digest.update(prefix);prefix_count+=len(prefix)
                if not count or total is not None and count!=total:raise ValueError('incomplete_media')
                return path,{'bytes_read':count,'response_total_bytes':total,'actual_bytes':count,
                    'content_length_header':total,'prefix_sha256':prefix_digest.hexdigest(),
                    'prefix_bytes':prefix_count,**etag_evidence(response.headers.get('ETag')),
                    'full_download':True,'complete_body_read':True,'redirects':hops,
                    'fetched_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                    'source_url_sha256':hashlib.sha256(url.encode()).hexdigest()},digest.hexdigest()
        raise ValueError('redirect_limit')
    finally:session.close()


def packet_measurement(path):
    """Sum every demuxed MP3 packet duration; never extrapolate file bitrate."""
    binary=shutil.which('ffprobe')
    if not binary or not Path(binary).is_absolute():raise ValueError('ffprobe_unavailable')
    process=None
    try:
        process=subprocess.Popen([binary,'-v','error','-max_alloc','33554432','-protocol_whitelist','file',
            '-f','mp3','-select_streams','a:0','-show_entries','packet=duration_time,size:packet_side_data=',
            '-of','compact=p=0:nk=0',str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        duration=Decimal(0);count=0;audio_bytes=0
        while True:
            line=process.stdout.readline(4097)
            if not line:break
            if len(line)>4096 or count>=1000000:raise ValueError('packet_output_limit')
            line=line.strip()
            if not line:continue
            fields={}
            for part in line.decode('ascii').split('|'):
                if not part:continue
                key,separator,value=part.partition('=')
                if not separator or key in fields or key not in ('duration_time','size'):
                    raise ValueError('unexpected_packet_fields')
                fields[key]=value
            if set(fields)!={'duration_time','size'}:raise ValueError('missing_packet_fields')
            if not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,9})?',fields['duration_time']) or not fields['size'].isdigit():
                raise ValueError('invalid_packet_numbers')
            seconds=Decimal(fields['duration_time']);size=int(fields['size'])
            if not Decimal(0)<seconds<=Decimal('0.2') or not 1<=size<=65536:
                raise ValueError('invalid_mp3_packet')
            count+=1;duration+=seconds;audio_bytes+=size
            if duration>172800 or audio_bytes>MAX_MEDIA:raise ValueError('packet_measurement_limit')
        returncode=process.wait(timeout=2)
        if returncode or process.stderr.read(1) or count<3 or duration<1 or audio_bytes>path.stat().st_size:
            raise ValueError('incomplete_mp3_demux')
        return {'known':True,'source':'full_mp3_packets','duration_seconds':float(duration),
            'packet_count':count,'demuxed_audio_bytes':audio_bytes,'full_demux_completed':True,
            'full_decode_checked':False,'full_stream_verified':False,'duration_extrapolated':False}
    finally:
        if process is not None:
            if process.poll() is None:process.kill()
            process.wait(timeout=3)
            for pipe in (process.stdout,process.stderr):
                if pipe is not None:pipe.close()


def evidence_hash(result):
    return hashlib.sha256(json.dumps({k:v for k,v in result.items() if k!='evidence_sha256'},
        sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def cache_gate(previous,url,fetched,prefix_sha256,*,require_fresh):
    """A fresh representation validator is mandatory on every reuse."""
    if not isinstance(previous,dict):return 'no_previous_full_evidence',None
    if (previous.get('known') is not True or not isinstance(previous.get('header'),dict)
            or previous['header'].get('source')!='full_mp3_packets' or 'full_evidence' in previous
            or not valid_result(previous,url)):return 'invalid_previous_full_evidence',None
    old=previous['fetch']; old_prefix=previous.get('prefix_sha256')
    if (old.get('etag_kind')!='strong' or fetched.get('etag_kind')!='strong'
            or not re.fullmatch('[0-9a-f]{64}',str(old.get('strong_etag_sha256','')))
            or not re.fullmatch('[0-9a-f]{64}',str(fetched.get('strong_etag_sha256','')))):
        return 'strong_validator_unavailable',None
    if old['strong_etag_sha256']!=fetched['strong_etag_sha256']:return 'validator_changed',None
    size=old.get('actual_bytes')
    if (type(size) is not int or old.get('response_total_bytes')!=size or old.get('content_length_header')!=size
            or fetched.get('response_total_bytes')!=size
            or fetched.get('bytes_read')!=min(size,MAX_PREFIX)
            or fetched.get('content_length_header')!=(fetched['bytes_read'] if fetched.get('range_honoured') is True else size)):
        return 'representation_length_changed_or_unproven',None
    if (not re.fullmatch('[0-9a-f]{64}',str(old_prefix or '')) or old.get('prefix_sha256')!=old_prefix
            or old.get('prefix_bytes')!=min(size,MAX_PREFIX) or prefix_sha256!=old_prefix):
        return 'prefix_changed_or_unproven',None
    if (not re.fullmatch('[0-9a-f]{64}',str(old.get('source_url_sha256','')))
            or fetched.get('source_url_sha256')!=old['source_url_sha256']):
        return 'final_source_changed_or_unproven',None
    try:
        original=dt.datetime.fromisoformat(old['fetched_at']);fresh=dt.datetime.fromisoformat(fetched['fetched_at'])
        if original.tzinfo is None or fresh.tzinfo is None:raise ValueError()
        age=(fresh-original).total_seconds()
        now=dt.datetime.now(dt.timezone.utc)
        if not 0<=age<=CACHE_MAX_AGE or (fresh-now).total_seconds()>60:
            return 'full_evidence_expired',None
        if require_fresh and not -60<=(now-fresh).total_seconds()<=60:
            return 'fresh_validation_expired',None
    except (ValueError,TypeError,KeyError,OverflowError):return 'evidence_time_invalid',None
    receipt={'validated_at':fetched['fetched_at'],'full_measurement_at':old['fetched_at'],
        'previous_evidence_sha256':previous['evidence_sha256'],
        'strong_etag_sha256':fetched['strong_etag_sha256'],'representation_bytes':size,
        'prefix_sha256':prefix_sha256,'source_url_sha256':hashlib.sha256(url.encode()).hexdigest(),
        'final_source_url_sha256':fetched['source_url_sha256']}
    return 'verified',receipt


def worker_result(url,*,full_probe=False,directory=None,previous_evidence=None):
    """Only invoked inside the dedicated, timeout-limited child process."""
    if full_probe:
        path,fetched,digest=download_full(url,directory)
        header=packet_measurement(path)
    else:
        data, fetched = fetch_prefix(url)
        header = header_duration(data, fetched['response_total_bytes'])
        digest=hashlib.sha256(data).hexdigest()
    result = {'known':header['known'], 'header':header, 'fetch':fetched,
              'full_stream_verified':False,
              'source_url_sha256':hashlib.sha256(url.encode()).hexdigest(),
              ('full_audio_sha256' if full_probe else 'prefix_sha256'):digest}
    if full_probe:result['prefix_sha256']=fetched.get('prefix_sha256')
    if header['known']: result['duration_seconds']=header['duration_seconds']
    else: result['reason']=header['reason']
    if not full_probe and not header['known'] and previous_evidence is not None:
        outcome,receipt=cache_gate(previous_evidence,url,fetched,digest,require_fresh=True)
        result['cache_check']=outcome
        if receipt is not None:
            result.update(known=True,duration_seconds=previous_evidence['duration_seconds'],
                header={'known':True,'source':'validated_full_mp3_cache',
                    'duration_seconds':previous_evidence['duration_seconds'],'full_stream_verified':False},
                full_audio_sha256=previous_evidence['full_audio_sha256'],full_evidence=previous_evidence,
                previous_evidence_sha256=previous_evidence['evidence_sha256'],cache_validation=receipt)
            result.pop('reason',None)
    result['evidence_sha256']=evidence_hash(result)
    return result


def _valid_result(result, url):
    if (not isinstance(result,dict) or type(result.get('known')) is not bool
            or result.get('source_url_sha256')!=hashlib.sha256(url.encode()).hexdigest()
            or result.get('full_stream_verified') is not False): return False
    header=result.get('header'); fetched=result.get('fetch')
    if not isinstance(header,dict) or not isinstance(fetched,dict) or header.get('known')!=result['known']: return False
    full=header.get('source')=='full_mp3_packets'
    cached=header.get('source')=='validated_full_mp3_cache'
    if type(fetched.get('bytes_read')) is not int or not 0<fetched['bytes_read']<=(MAX_MEDIA if full else MAX_PREFIX): return False
    if full and (header.get('full_demux_completed') is not True or header.get('duration_extrapolated') is not False
                 or header.get('full_decode_checked') is not False or fetched.get('complete_body_read') is not True
                 or fetched.get('full_download') is not True or fetched.get('actual_bytes')!=fetched['bytes_read']
                 or type(header.get('packet_count')) is not int or not 3<=header['packet_count']<=1000000
                 or not re.fullmatch('[0-9a-f]{64}',str(result.get('full_audio_sha256','')))):return False
    if result['known']:
        value=result.get('duration_seconds')
        if (type(value) not in (int,float) or not math.isfinite(value) or not 1<=value<=172800
                or header.get('duration_seconds')!=value
                or header.get('source') not in ('Xing_frame_count','Info_frame_count','VBRI_frame_count','full_mp3_packets','validated_full_mp3_cache')): return False
    if cached:
        if result['known'] is not True:return False
        previous=result.get('full_evidence')
        outcome,receipt=cache_gate(previous,url,fetched,result.get('prefix_sha256'),require_fresh=False)
        if (outcome!='verified' or result.get('cache_check')!='verified' or result.get('cache_validation')!=receipt
                or result.get('previous_evidence_sha256')!=previous['evidence_sha256']
                or result.get('duration_seconds')!=previous['duration_seconds']
                or result.get('full_audio_sha256')!=previous['full_audio_sha256']):return False
    digest=result.get('evidence_sha256')
    expected=evidence_hash(result)
    return digest==expected


def valid_result(result,url):
    try:return _valid_result(result,url)
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError,RecursionError):return False


def private_temp_root(value):
    """Accept only an existing private directory owned by this process user."""
    path=Path(value)
    if not path.is_absolute() or path.resolve(strict=True)!=path:raise ValueError()
    info=path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o700
            or info.st_uid!=os.geteuid()):raise ValueError()
    return path


def measure(url, timeout_seconds=12.0, *, full_probe=False,previous_evidence=None,temp_root=None):
    """Return bounded evidence or an explicit safe failure; never expose URLs.

    subprocess timeout also covers a stalled resolver/redirect/header read.
    No shell is used and the URL is passed exclusively through child's stdin.
    """
    if (not isinstance(url,str) or not 1<=len(url)<=8192 or '\\' in url
            or any(c.isspace() or ord(c)<32 or ord(c)==127 for c in url)
            or type(timeout_seconds) not in (int,float) or not math.isfinite(timeout_seconds)
            or type(full_probe) is not bool or not 1<=timeout_seconds<=(45 if full_probe else 12)):
        return {'known':False,'reason':'invalid_probe_input'}
    executable=Path(sys.executable)
    script=Path(__file__).resolve()
    if not executable.is_absolute() or not script.is_file(): return {'known':False,'reason':'probe_runtime_unavailable'}
    # Parent owns cleanup, so the candidate is removed even if child/ffprobe
    # must be killed for a wall timeout. The private process group includes
    # ffprobe and prevents an abandoned reader from surviving the timeout.
    try:
        base=private_temp_root(temp_root) if temp_root is not None else None
        temporary_context=tempfile.TemporaryDirectory(prefix='pine-media-probe-',dir=base)
    except (OSError,ValueError,TypeError):return {'known':False,'reason':'probe_storage_unavailable'}
    with temporary_context as temporary:
        directory=Path(temporary).resolve()
        if full_probe and shutil.disk_usage(directory).free<MIN_FREE:return {'known':False,'reason':'probe_storage_unavailable'}
        process=None
        try:
            payload={'url':url,'full_probe':full_probe,'directory':str(directory) if full_probe else None,
                     'previous_evidence':previous_evidence if not full_probe else None}
            encoded=json.dumps(payload,allow_nan=False).encode()
            if len(encoded)>65536:return {'known':False,'reason':'invalid_probe_input'}
            process=subprocess.Popen([str(executable),str(script),'--worker'],stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True,
                env={'PYTHONDONTWRITEBYTECODE':'1','PATH':os.environ.get('PATH','/usr/bin:/bin'),'LANG':'C.UTF-8'})
            output,_=process.communicate(input=encoded,timeout=timeout_seconds)
            if process.returncode or not 0<len(output)<=32768:return {'known':False,'reason':'probe_failed'}
            result=json.loads(output)
            if result=={'known':False,'reason':'probe_failed'}:return result
            if not valid_result(result,url):return {'known':False,'reason':'invalid_probe_result'}
            if result['header'].get('source')=='validated_full_mp3_cache':
                outcome,_=cache_gate(result['full_evidence'],url,result['fetch'],result.get('prefix_sha256'),require_fresh=True)
                if outcome!='verified':return {'known':False,'reason':'stale_cached_probe_result'}
            return result
        except subprocess.TimeoutExpired:return {'known':False,'reason':'probe_timeout'}
        except Exception:return {'known':False,'reason':'probe_process_failed'}
        finally:
            if process is not None:
                if process.poll() is None:
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                try:process.wait(timeout=3)
                except (OSError,subprocess.TimeoutExpired):pass
                for pipe in (process.stdin,process.stdout):
                    if pipe is not None:pipe.close()


if __name__=='__main__':
    try:
        if sys.argv[1:]!=['--worker']: raise ValueError()
        data=sys.stdin.buffer.read(65537)
        if len(data)>65536: raise ValueError()
        payload=json.loads(data)
        if not isinstance(payload,dict) or set(payload)!={'url','full_probe','directory','previous_evidence'} or type(payload['full_probe']) is not bool: raise ValueError()
        directory=Path(payload['directory']) if payload['full_probe'] else None
        result=worker_result(payload['url'],full_probe=payload['full_probe'],directory=directory,previous_evidence=payload['previous_evidence'])
    except Exception: result={'known':False,'reason':'probe_failed'}
    print(json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False))


