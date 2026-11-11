"""Resumable direct-CZ video checks. No proxy, geo bypass or paid services."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import filter_ru_ua as catalog

MIN_INTERVAL = 5 * 3600
MAX_AGE = 7 * 86400


class Inconclusive(Exception):
    pass


class GeoBlocked(Exception):
    pass


class StreamFailure(ValueError):
    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


def fetch(url, headers=None, limit=2_000_000, deadline=None):
    if not url.startswith(('http://', 'https://')):
        raise Inconclusive('Non-HTTP transport needs a separate checker')
    started = time.monotonic()
    request = urllib.request.Request(url, headers=headers or {'User-Agent': 'My-IPTV-CZ/1.0'})
    try:
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with direct.open(request, timeout=10) as response:
            data = bytearray()
            while len(data) < limit:
                if deadline and time.monotonic() >= deadline:
                    raise TimeoutError('Stream time budget exceeded')
                chunk = response.read(min(65536, limit - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            result = bytes(data)
            # Catch HTML regional error pages even if the server returned HTTP 200.
            if 'text/html' in response.headers.get('Content-Type', ''):
                if geo_message(result):
                    raise GeoBlocked('Regional restriction message in HTML')
                raise StreamFailure('HTML response instead of media')
            return result, response.geturl(), time.monotonic() - started
    except urllib.error.HTTPError as error:
        body = error.read(8192)
        if error.code == 451 or (error.code == 403 and geo_message(body)):
            raise GeoBlocked(f'HTTP {error.code}: regional restriction') from None
        # A bare 403 may be authentication, expiry or hotlink protection, not geo.
        raise StreamFailure(f'HTTP {error.code}', http_status=error.code) from None


def geo_message(body):
    return bool(re.search(rb'geo.?block|not available in your (country|region|location)|'
                          rb'not available in (this|your) territory|regional restriction|'
                          rb'country.{0,30}(blocked|restricted)', body.lower()))


def cz_identity():
    observations = []
    for url, country_key, ip_key in (
        ('https://ipapi.co/json/', 'country_code', 'ip'),
        ('https://ipwho.is/', 'country_code', 'ip'),
    ):
        body, _, _ = fetch(url, limit=65536)
        info = json.loads(body)
        if info.get(country_key) != 'CZ' or not info.get(ip_key):
            raise Inconclusive('Direct public IP is not confirmed as CZ by both providers')
        observations.append(info[ip_key])
    if len(set(observations)) != 1:
        raise Inconclusive('IP geolocation providers disagree on the public IP')
    # Keep proof of consistent egress without publishing the home IP.
    return hashlib.sha256(observations[0].encode()).hexdigest()


def connection(block):
    a = catalog.attrs(block[0])
    headers = {'User-Agent': a.get('http-user-agent', 'Mozilla/5.0 My-IPTV-CZ/1.0')}
    for attr, name in [('http-referrer', 'Referer'), ('http-referer', 'Referer'),
                       ('http-origin', 'Origin'), ('http-cookie', 'Cookie')]:
        if a.get(attr):
            headers[name] = a[attr]
    for line in block[1:-1]:
        if line.startswith('#EXTVLCOPT:'):
            option, _, value = line[len('#EXTVLCOPT:'):].partition('=')
            names = {'http-user-agent': 'User-Agent', 'http-referrer': 'Referer',
                     'http-referer': 'Referer', 'http-origin': 'Origin', 'http-cookie': 'Cookie'}
            if option in names:
                headers[names[option]] = value
            else:
                raise Inconclusive('Unsupported VLC stream option; original entry retained')
        elif line.startswith('#KODIPROP:'):
            option, _, value = line[len('#KODIPROP:'):].partition('=')
            if option in ('inputstream.adaptive.stream_headers', 'inputstream.adaptive.manifest_headers'):
                headers.update(dict(urllib.parse.parse_qsl(value)))
            else:
                raise Inconclusive('Unsupported Kodi/DRM option; original entry retained')
        elif not line.startswith(('#EXTGRP:', '#EXTVLCOPT:', '#EXTHTTP:')):
            raise Inconclusive('Unsupported stream option; original entry retained')
        if line.startswith('#EXTHTTP:'):
            headers.update(json.loads(line[len('#EXTHTTP:'):]))
    url, separator, suffix = block[-1].partition('|')
    if separator:
        headers.update(dict(urllib.parse.parse_qsl(suffix)))
    if any(not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", k) for k in headers):
        raise ValueError('Unsafe HTTP header name')
    if any('\r' in str(v) or '\n' in str(v) for v in headers.values()):
        raise ValueError('Unsafe HTTP header')
    return url, headers


def hls_sample(body, base, headers, deadline, depth=0):
    if depth > 4:
        raise ValueError('HLS nesting exceeds limit')
    lines = body.decode('utf-8-sig').splitlines()
    if not lines or lines[0].strip() != '#EXTM3U':
        raise ValueError('Invalid HLS manifest')
    if any(line.startswith('#EXT-X-BYTERANGE:') or (line.startswith('#EXT-X-MAP:') and 'BYTERANGE=' in line) for line in lines):
        raise Inconclusive('Byte-range HLS needs a separate sample checker')
    variants = []
    for i, line in enumerate(lines):
        if line.startswith('#EXT-X-STREAM-INF:'):
            next_url = next((s.strip() for s in lines[i+1:] if s.strip() and not s.startswith('#')), None)
            if not next_url:
                raise ValueError('HLS variant has no URI')
            bandwidth = re.search(r'BANDWIDTH=(\d+)', line)
            resolution = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
            pixels = int(resolution[1])*int(resolution[2]) if resolution else 0
            variants.append((pixels, int(bandwidth[1]) if bandwidth else 0, next_url))
    if variants:
        # Select advertised best rendition; only FFprobe can confirm actual 4K/HDR.
        target = urllib.parse.urljoin(base, max(variants)[2])
        nested, resolved, _ = fetch(target, headers, deadline=deadline)
        return hls_sample(nested, resolved, headers, deadline, depth+1)
    segments = []
    resources = set()
    for line in lines:
        if line.startswith('#EXT-X-KEY:') and 'METHOD=NONE' not in line:
            if 'METHOD=AES-128' not in line or ('KEYFORMAT=' in line and 'KEYFORMAT="identity"' not in line):
                raise Inconclusive('DRM or unsupported HLS encryption; no bypass attempted')
            match = re.search(r'URI="([^"]+)"', line)
            if not match:
                raise ValueError('HLS key URI missing')
            target = urllib.parse.urljoin(base, match[1])
            if target not in resources:
                key, _, _ = fetch(target, headers, limit=17, deadline=deadline)
                if len(key) != 16:
                    raise ValueError('Invalid AES-128 key')
                resources.add(target)
        if line.startswith('#EXT-X-MAP:'):
            match = re.search(r'URI="([^"]+)"', line)
            if not match:
                raise ValueError('HLS initialization URI missing')
            target = urllib.parse.urljoin(base, match[1])
            if target not in resources:
                data, _, _ = fetch(target, headers, deadline=deadline)
                if not data:
                    raise ValueError('Empty HLS initialization data')
                resources.add(target)
        if line.strip() and not line.startswith('#'):
            segments.append(urllib.parse.urljoin(base, line.strip()))
    if len(set(segments)) < 3:
        raise Inconclusive('Fewer than three distinct HLS segments available')
    total, elapsed = 0, 0.0
    for segment in segments[-3:]:
        data, _, seconds = fetch(segment, headers, limit=1_000_000, deadline=deadline)
        if len(data) < 188:
            raise ValueError('Empty or truncated HLS media segment')
        total += len(data)
        elapsed += seconds
    return {'playback_url': base, 'segments': 3, 'sample_bytes': total,
            'download_bytes_per_second': round(total / max(elapsed, .001))}


def video_properties(info):
    streams = info.get('streams', [])
    if not streams:
        return {}
    stream = streams[0]
    frames = info.get('frames', [])
    first = frames[0] if frames else {}
    width, height = stream.get('width', first.get('width', 0)), stream.get('height', first.get('height', 0))
    pix_fmt = stream.get('pix_fmt', first.get('pix_fmt', ''))
    bits = int(stream.get('bits_per_raw_sample') or 0)
    match = re.search(r'(?:p|gbrp)(10|12|16)(?:le|be)?$', pix_fmt)
    if match:
        bits = max(bits, int(match[1]))
    if pix_fmt.startswith(('p010', 'p016')):
        bits = max(bits, 10 if pix_fmt.startswith('p010') else 16)
    transfer = stream.get('color_transfer', first.get('color_transfer', 'unknown'))
    primaries = stream.get('color_primaries', first.get('color_primaries', 'unknown'))
    side_data = stream.get('side_data_list', []) + [s for frame in frames for s in frame.get('side_data_list', [])]
    types = [s.get('side_data_type', '').lower() for s in side_data]
    hdr = []
    if bits >= 10 and primaries == 'bt2020':
        if transfer == 'arib-std-b67':
            hdr = ['HLG']
        elif transfer == 'smpte2084':
            if any('smpte2094-40' in s or 'hdr10+' in s for s in types):
                hdr = ['HDR10+']
            elif any('mastering display metadata' in s for s in types) and any('content light level metadata' in s for s in types):
                hdr = ['HDR10']
    return {'width': width, 'height': height, 'pix_fmt': pix_fmt, 'bit_depth': bits,
            'color_transfer': transfer, 'color_primaries': primaries,
            'hdr_types': hdr, 'side_data_types': sorted(set(types)),
            'evidence': 'FFprobe stream and decoded frame metadata'}


def probe_video(url, headers, ffprobe, environment, deadline):
    if not ffprobe:
        return {}
    command = [ffprobe, '-v', 'error', '-rw_timeout', '10000000',
               '-headers', ''.join(f'{k}: {v}\r\n' for k, v in headers.items()),
               '-select_streams', 'v:0', '-read_intervals', '%+4', '-show_streams', '-show_frames',
               '-show_entries', 'stream=width,height,pix_fmt,color_transfer,color_primaries,bits_per_raw_sample:stream_side_data:frame=width,height,pix_fmt,color_transfer,color_primaries:frame_side_data',
               '-of', 'json', url]
    try:
        run = subprocess.run(command, capture_output=True, text=True, errors='replace',
                             timeout=max(1, min(20, deadline-time.monotonic())), env=environment)
        if run.returncode or run.stderr.strip():
            return {}
        return video_properties(json.loads(run.stdout))
    except (ValueError, subprocess.TimeoutExpired):
        return {}


def check(channel, ffmpeg, ffprobe=None):
    started = time.monotonic()
    result = {'at': catalog.now(), 'region': 'CZ', 'result': 'unknown'}
    try:
        deadline = started + 90
        url, headers = connection(channel['block'])
        body, resolved, seconds = fetch(url, headers, limit=262144, deadline=deadline)
        if body.lstrip(b'\xef\xbb\xbf').startswith(b'#EXTM3U'):
            sample = hls_sample(body, resolved, headers, deadline)
            url = sample.pop('playback_url')
            result.update(sample)
        elif not body:
            raise StreamFailure('Empty stream response')
        else:
            result.update(sample_bytes=len(body), download_bytes_per_second=round(len(body)/max(seconds, .001)))
        environment = {k: v for k, v in os.environ.items() if 'proxy' not in k.lower()}
        properties = probe_video(url, headers, ffprobe, environment, deadline)
        header_text = ''.join(f'{k}: {v}\r\n' for k, v in headers.items())
        command = [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-xerror',
                   '-err_detect', 'explode', '-rw_timeout', '10000000',
                   '-headers', header_text, '-i', url, '-t', '12', '-map', '0:v:0',
                   '-an', '-progress', 'pipe:1', '-f', 'null', '-']
        decoding_started = time.monotonic()
        run = subprocess.run(command, capture_output=True, text=True, errors='replace',
                             timeout=max(1, min(30, deadline-time.monotonic())), env=environment)
        frames = [int(v) for v in re.findall(r'^frame=(\d+)', run.stdout, re.M)]
        times = [int(v) for v in re.findall(r'^out_time_us=(\d+)', run.stdout, re.M)]
        if run.returncode or run.stderr.strip() or not frames or max(frames) < 12 or not times or max(times) < 10_000_000:
            raise StreamFailure('Video decoding failed or fewer than 10 seconds decoded')
        decoding_elapsed = time.monotonic()-decoding_started
        if decoding_elapsed > 24:
            raise StreamFailure('Media delivery too slow for this playback sample')
        result.update(result='ok', decoded_frames=max(frames), decoded_seconds=max(times)/1e6,
                      decoding_wall_seconds=round(decoding_elapsed, 2), video=properties)
    except GeoBlocked as error:
        result.update(result='geo', reason=str(error))
    except Inconclusive as error:
        result.update(reason=str(error))
    except StreamFailure as error:
        result.update(result='fail', reason=str(error))
        if error.http_status is not None:
            result['http_status'] = error.http_status
    except (TimeoutError, subprocess.TimeoutExpired):
        result.update(result='fail', reason='Media request or decoder timeout')
    except Exception as error:
        # Do not write exception URLs, query tokens or FFmpeg stderr into public logs.
        result.update(result='fail', reason=type(error).__name__ + ': media check failed')
    result['elapsed_seconds'] = round(time.monotonic() - started, 2)
    return result


def timestamp(value):
    return datetime.fromisoformat(value).timestamp()


def classify(channel, clock=None):
    clock = time.time() if clock is None else clock
    history = [h for h in channel['history'] if h.get('region') == 'CZ'
               and h.get('egress_sha256') and clock-timestamp(h['at']) <= MAX_AGE]
    if not history:
        return 'UNKNOWN_CZ'
    # Only independently spaced observations count toward promotion or removal.
    spaced = []
    for h in sorted(history, key=lambda h: h['at']):
        if not spaced or timestamp(h['at']) - timestamp(spaced[-1]['at']) >= MIN_INTERVAL:
            spaced.append(h)
    results = [h['result'] for h in spaced if h['result'] != 'unknown']
    if not results:
        return 'UNKNOWN_CZ'
    if len(results) >= 2 and results[-2:] == ['geo', 'geo']:
        return 'GEO_BLOCKED_CZ'
    if len(results) >= 3 and results[-3:] == ['fail'] * 3:
        return 'OFFLINE'
    if len(results) >= 3 and results[-3:] == ['ok'] * 3:
        return 'STABLE_CZ'
    # One failure does not remove an established working channel.
    if channel['status'] == 'STABLE_CZ' and results[-1] in ('fail', 'geo'):
        if len(results) >= 2 and results[-2] == 'ok':
            return 'STABLE_CZ'
    if 'ok' in results and any(x in ('fail', 'geo') for x in results):
        return 'UNSTABLE_CZ'
    return 'UNKNOWN_CZ'


def run(args):
    if not args.confirm_direct_cz:
        raise Inconclusive('Run on the Windows PC in CZ with --confirm-direct-cz; disable VPN/proxy first')
    ffmpeg = shutil.which(args.ffmpeg)
    ffprobe = shutil.which(args.ffprobe)
    if not ffmpeg:
        raise Inconclusive('FFmpeg is required; no playback evidence will be invented')
    identity = cz_identity()
    db = catalog.build(catalog.load(), args.refresh_metadata)
    channels = db['channels']
    cycle = db.get('cycle')
    if cycle is None:
        eligible = [ident for ident, c in channels.items() if catalog.content_filter.eligible(c) and
                    (not c['history'] or time.time()-timestamp(c['history'][-1]['at']) >= MIN_INTERVAL)]
        cycle = {'started_at': catalog.now(), 'remaining': eligible, 'checked': [], 'observations': {}}
    remaining = [ident for ident in cycle['remaining'] if catalog.content_filter.eligible(channels[ident])]
    started = time.monotonic()
    checked = []
    # Bounded parallelism; each batch has distinct origin hosts.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while remaining and len(checked) < args.max_channels and time.monotonic()-started < args.budget_seconds-100:
            batch, hosts = [], set()
            for ident in remaining:
                host = urllib.parse.urlsplit(channels[ident]['block'][-1]).hostname
                if host not in hosts:
                    batch.append(ident)
                    hosts.add(host)
                if len(batch) >= min(args.workers, args.max_channels-len(checked)):
                    break
            results = list(pool.map(lambda ident: check(channels[ident], ffmpeg, ffprobe), batch))
            for ident, result in zip(batch, results):
                result['egress_sha256'] = identity
                channels[ident]['history'].append(result)
                cycle['observations'][ident] = result
                remaining.remove(ident)
                checked.append(ident)
            time.sleep(args.pause_seconds)
    if cz_identity() != identity:
        raise Inconclusive('Public CZ IP changed during checks; output and database retained')
    cycle['remaining'] = remaining
    cycle['checked'].extend(checked)
    db['cycle'] = cycle if remaining else None
    became_unavailable, recovered, newly_stable, lost_stable = [], [], [], []
    before = {ident: c['status'] for ident, c in channels.items()}
    publication = {'main_updated': False, 'main_file_entries': catalog.existing_entries('playlist-ru-ua.m3u'),
                   'testing_file_entries': catalog.existing_entries('playlist-ru-ua-testing.m3u'),
                   'note': 'Incomplete cycle: previous playlists retained'}
    files = {}
    if not remaining:
        for ident, channel in channels.items():
            had_stable = channel.get('ever_stable', False) or before[ident] == 'STABLE_CZ'
            channel['status'] = classify(channel)
            if before[ident] == 'STABLE_CZ' and channel['status'] != 'STABLE_CZ':
                lost_stable.append(ident)
                if channel['status'] in ('UNSTABLE_CZ', 'OFFLINE', 'GEO_BLOCKED_CZ'):
                    became_unavailable.append(ident)
            if before[ident] != 'STABLE_CZ' and channel['status'] == 'STABLE_CZ':
                (recovered if had_stable else newly_stable).append(ident)
            if channel['status'] == 'STABLE_CZ':
                channel['ever_stable'] = True
        publication = catalog.publish(db, files)
    report = {'at': catalog.now(), **db['counts'], 'streams_checked': len(checked),
              'cycle_remaining': len(remaining), 'cycle_complete': not remaining,
              'statuses': catalog.status_counts(db),
              'became_unavailable': became_unavailable, 'recovered': recovered,
              'newly_stable': newly_stable, 'lost_stable_status': lost_stable,
              'check_outcomes': dict(Counter(cycle['observations'][i]['result'] for i in checked)),
              **publication}
    report.update(catalog.categories.statistics(db, catalog.DATA))
    report_path = catalog.DATA / 'reports' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
    files.update({catalog.DB: catalog.json_text(db), catalog.DATA / 'ru-ua-report.json': catalog.json_text(report),
                  catalog.DATA / 'review-needed.json': catalog.json_text(db['review_needed']),
                  report_path: catalog.json_text(report)})
    catalog.transaction(files)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-direct-cz', action='store_true')
    parser.add_argument('--refresh-metadata', action='store_true')
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--ffprobe', default='ffprobe')
    parser.add_argument('--workers', type=int, choices=range(1, 5), default=2)
    parser.add_argument('--max-channels', type=int, default=120)
    parser.add_argument('--budget-seconds', type=int, default=1500)
    parser.add_argument('--pause-seconds', type=float, default=1)
    args = parser.parse_args()
    if args.max_channels < 1 or args.budget_seconds < 101 or args.pause_seconds < 0:
        parser.error('Invalid check limits')
    run(args)


if __name__ == '__main__':
    try:
        with catalog.lock():
            main()
    except Exception as error:
        catalog.record_failure('cz-check', error)
        raise
