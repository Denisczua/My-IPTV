"""Fetch complete provider indexes, audit every URL, retain validated snapshots."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import urllib.request
from urllib.parse import urlsplit
from filter_ru_ua import ROOT, DATA, atomic, now, parse, save_json

CACHE = ROOT / '.tools' / 'movie-sources'
AUDIT = DATA / 'movie-source-audit.json'


def download(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={'User-Agent': 'My-IPTV-Movies/2.0'})
    with opener.open(request, timeout=35) as response:
        return response.read(32_000_001), response.geturl(), response.headers.get('Last-Modified')


def cache_path(url):
    return CACHE / (hashlib.sha256(url.encode()).hexdigest() + '.m3u')


def parse_source(text):
    """Account for explicit no-stream placeholders and non-OTTPlayer transports."""
    blocks, omitted, current = [], [], []
    lines = text.lstrip('\ufeff').splitlines()
    if not lines or not lines[0].startswith('#EXTM3U'):
        raise ValueError('Missing source M3U header')
    def finish(block):
        if not block:
            return
        if block[-1] == '[NO PUBLIC STREAM]' or block[-1].startswith(('srt://', 'mms://')):
            omitted.append({'block': block, 'reason': 'No public URL or unsupported OTTPlayer transport'})
        else:
            blocks.extend(parse('#EXTM3U\n'+'\n'.join(block)+'\n'))
    for line in lines[1:]:
        if line.startswith('#EXTINF:'):
            finish(current)
            current = [line]
        elif current and line.strip():
            current.append(line)
    finish(current)
    if not blocks:
        raise ValueError('No usable source entries')
    return blocks, omitted


def fetch_one(item):
    provider, url = item
    record = {'provider': provider, 'url': url, 'checked_at': now()}
    try:
        raw, resolved, modified = download(url)
        if len(raw) > 32_000_000:
            raise ValueError('Source exceeds 32 MB limit')
        text = raw.decode('utf-8-sig')
        entries, omitted = parse_source(text)
        atomic(cache_path(url), text)
        record.update(ok=True, effective_url=resolved, entries=len(entries),
                      sha256=hashlib.sha256(raw).hexdigest(), last_modified=modified)
        record['omitted_records'] = omitted
    except Exception as error:
        record.update(ok=False, error_type=type(error).__name__,
                      http_status=getattr(error, 'code', None))
    return record


def refresh():
    config = json.loads((DATA/'movie-providers.json').read_text(encoding='utf-8'))
    docs, discovered = [], []
    for provider in config['providers']:
        record = {'provider': provider['name'], 'repository': provider['repository'], 'checked_at': now()}
        try:
            raw, _, _ = download(f"https://raw.githubusercontent.com/{provider['repository']}/HEAD/README.md")
            text = raw.decode('utf-8-sig')
            atomic(CACHE / (provider['repository'].replace('/', '-') + '-README.md'), text)
            record['ok'] = True
            # Only documented public playlist URLs belonging to this provider.
            host = provider['repository'].split('/')[0].lower()+'.github.io'
            for url in re.findall(r'https://[^\s<>\)\]"\x60]+', text):
                if urlsplit(url).hostname == host and '/radio/' not in url and '/media/' not in url and re.search(r'/(all|index|full|complete|playlist)\.m3u8?$', url, re.I):
                    if url not in provider['urls']:
                        discovered.append((provider['name'], url))
        except Exception as error:
            record.update(ok=False, error_type=type(error).__name__)
        docs.append(record)
    requested = [(p['name'], u) for p in config['providers'] for u in p['urls']]
    for source in json.loads((ROOT/'channels.json').read_text(encoding='utf-8')).get('sources',[]):
        if source.get('enabled') and not any(u==source['url'] for _,u in requested):
            requested.append(('Primary: '+source['name'],source['url']))
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(fetch_one, requested+discovered))
    # Complete metadata for identity, genre and language; never truncate silently.
    for name in ('channels', 'feeds'):
        raw, _, _ = download(f'https://iptv-org.github.io/api/{name}.json')
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) < 1000:
            raise ValueError('Incomplete IPTV-org metadata')
        atomic(CACHE / f'{name}.json', raw.decode('utf-8-sig'))
    audit = {'at': now(), 'documentation': docs, 'sources': records,
             'providers_available': len({r['provider'] for r in records if r['ok']} & {p['name'] for p in config['providers']}),
             'entries_downloaded': sum(r.get('entries', 0) for r in records if r['ok'])}
    save_json(AUDIT, audit)
    return audit


def entries(audit):
    for source in audit['sources']:
        path = cache_path(source['url'])
        if source['ok'] and path.exists():
            text = path.read_text(encoding='utf-8-sig')
            header = text.splitlines()[0]
            for block in parse_source(text)[0]:
                yield source['provider'], source['url'], header, block


if __name__ == '__main__':
    result = refresh()
    print(json.dumps(result, ensure_ascii=True, indent=2))
