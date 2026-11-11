"""Conservative language catalogue and transactional OTTPlayer playlist generation."""
import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import re
import tempfile
import urllib.request
from contextlib import contextmanager
import ru_ua_categories as categories
import ru_ua_content as content_filter

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
SOURCE = ROOT / 'playlist.m3u'
DB = DATA / 'ru-ua-state.json'
LANG_URLS = {lang: f'https://iptv-org.github.io/iptv/languages/{lang}.m3u'
             for lang in ('rus', 'ukr')}
FEEDS_URL = 'https://iptv-org.github.io/api/feeds.json'


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.iptv-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_json(path, data):
    atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def json_text(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + '\n'


def status_counts(db):
    result = dict.fromkeys(('STABLE_CZ', 'UNSTABLE_CZ', 'GEO_BLOCKED_CZ', 'OFFLINE', 'UNKNOWN_CZ'), 0)
    for channel in db['channels'].values():
        if channel.get('languages'):
            result[channel['status']] += 1
    return result


def record_failure(kind, error):
    """Keep the last good report and outputs; failures get a history entry."""
    report = {'at': now(), 'kind': kind, 'completed': False,
              'accepted_results': 0, 'previous_outputs_retained': True,
              'error_type': type(error).__name__}
    try:
        path = DATA / 'reports' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-failed.json')
        save_json(path, report)
    except OSError:
        pass


def transaction(files):
    """Rollback ordinary I/O failures. GitHub only commits after script success."""
    old = {p: p.read_text(encoding='utf-8') if p.exists() else None for p in files}
    changed = []
    try:
        for path, text in files.items():
            atomic(path, text)
            changed.append(path)
    except BaseException:
        for path in reversed(changed):
            if old[path] is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, old[path])
        raise


@contextmanager
def lock():
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / '.ru-ua.lock').open('a+b') as stream:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            if not stream.read(1):
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def attrs(line):
    return dict(re.findall(r'([\w-]+)="([^"]*)"', line))


def parse(text):
    lines = text.lstrip('\ufeff').splitlines()
    if not lines or not lines[0].startswith('#EXTM3U'):
        raise ValueError('Missing #EXTM3U')
    blocks, block = [], []
    for line in lines[1:]:
        if not line.strip():
            continue
        if line.startswith('#EXTINF:'):
            if block:
                raise ValueError('EXTINF without stream URL')
            if not re.match(r'^#EXTINF:-?\d+(?:\.\d+)?(?:\s|,)', line):
                raise ValueError('Invalid EXTINF duration')
            quoted, separator = False, None
            for i, char in enumerate(line):
                if char == '"':
                    quoted = not quoted
                elif char == ',' and not quoted:
                    separator = i
                    break
            if separator is None or not line[separator+1:].strip():
                raise ValueError('Missing EXTINF channel name')
            block = [line]
        elif line.startswith('#'):
            if block:
                block.append(line)
            # Global options are preserved separately by render().
        else:
            if not block or not re.match(r'^(https?|rtsp|rtmp|udp)://', line):
                raise ValueError('Orphan or unsupported stream URL')
            block.append(line)
            blocks.append(block)
            block = []
    if block:
        raise ValueError('Truncated channel')
    if not blocks:
        raise ValueError('Empty playlist')
    return blocks


def key(block):
    # Category duplicates in the generated M3U are not new manual streams.
    normalized = [re.sub(r'\s+group-title="[^"]*"', '', block[0])]
    normalized.extend(s for s in block[1:] if not s.startswith('#EXTGRP:'))
    return hashlib.sha256('\n'.join(normalized).encode()).hexdigest()


def load():
    if not DB.exists():
        return {'version': 1, 'channels': {}, 'cycle': None}
    db = json.loads(DB.read_text(encoding='utf-8'))
    if db.get('version') != 1 or not isinstance(db.get('channels'), dict):
        raise ValueError('Invalid state database')
    # Migrate old category-sensitive keys without discarding any observations.
    remap, merged = {}, {}
    for ident, channel in db['channels'].items():
        new_id = key(channel['block'])
        remap[ident] = new_id
        if new_id in merged:
            for observation in channel['history']:
                if observation not in merged[new_id]['history']:
                    merged[new_id]['history'].append(observation)
            merged[new_id]['history'].sort(key=lambda h: h['at'])
        else:
            merged[new_id] = channel
    db['channels'] = merged
    if db.get('cycle'):
        for field in ('remaining', 'checked'):
            db['cycle'][field] = list(dict.fromkeys(remap[x] for x in db['cycle'][field]))
        db['cycle']['observations'] = {remap[k]: v for k, v in db['cycle']['observations'].items()}
    return db


def language_index(refresh=False):
    index, urls = {}, {}
    for lang, url in LANG_URLS.items():
        path = DATA / f'iptv-org-{lang}.m3u'
        if refresh:
            with urllib.request.urlopen(url, timeout=30) as response:
                content = response.read().decode('utf-8-sig')
            parse(content)  # Never replace a good snapshot with an invalid response.
            atomic(path, content)
        for block in parse(path.read_text(encoding='utf-8-sig')):
            channel_id = attrs(block[0]).get('tvg-id', '').split('@')[0]
            if channel_id:
                index.setdefault(channel_id, set()).add(lang)
            urls.setdefault(block[-1], set()).add(lang)
    return index, urls


def feed_languages(refresh=False):
    path = DATA / 'iptv-org-feed-languages.json'
    if refresh:
        with urllib.request.urlopen(FEEDS_URL, timeout=30) as response:
            feeds = json.load(response)
        if not isinstance(feeds, list) or len(feeds) < 1000:
            raise ValueError('Incomplete feed metadata')
        projected = {f"{f['channel']}@{f['id']}": f['languages'] for f in feeds}
        save_json(path, {'source': FEEDS_URL, 'retrieved_at': now(), 'feeds': projected})
    return json.loads(path.read_text(encoding='utf-8'))['feeds']


def languages(block, index, urls, overrides, feeds=None):
    a = attrs(block[0])
    channel_id = a.get('tvg-id', '').split('@')[0]
    override = overrides.get(block[-1], overrides.get(channel_id))
    if override is not None:
        if not override.get('evidence') or any(x not in ('rus', 'ukr') for x in override['languages']):
            raise ValueError('Language override requires evidence and supported languages')
        return override['languages'], override['evidence']
    explicit = a.get('tvg-language', '').lower()
    tokens = {x.strip() for x in re.split('[;,/]', explicit) if x.strip()}
    aliases = {'ru': 'rus', 'rus': 'rus', 'russian': 'rus', 'русский': 'rus',
               'uk': 'ukr', 'ukr': 'ukr', 'ukrainian': 'ukr', 'українська': 'ukr'}
    if tokens:
        found = {aliases[x] for x in tokens if x in aliases}
        # Multilingual feeds with additional languages need an explicit review.
        if found and not all(x in aliases for x in tokens):
            return [], 'multilingual: review predominance'
        return (sorted(found) if all(x in aliases for x in tokens) else []), 'tvg-language'
    full_id = a.get('tvg-id', '')
    if feeds and full_id in feeds and feeds[full_id]:
        values = set(feeds[full_id])
        target = values & {'rus', 'ukr'}
        if target and values - {'rus', 'ukr'}:
            return [], 'multilingual: review predominance'
        return sorted(target), 'IPTV-org feeds.languages'
    found = urls.get(block[-1], set()) or index.get(channel_id, set())
    return sorted(found), 'IPTV-org language playlists' if found else 'unresolved'


def build(db, refresh=False):
    text = SOURCE.read_text(encoding='utf-8-sig')
    blocks = parse(text)  # Whole file; no line limit.
    index, urls = language_index(refresh)
    feeds = feed_languages(refresh)
    content_metadata = content_filter.metadata(DATA, save_json, now, refresh)
    content_overrides = json.loads((DATA/'content-overrides.json').read_text(encoding='utf-8'))
    override_path = DATA / 'language-overrides.json'
    overrides = json.loads(override_path.read_text(encoding='utf-8'))
    counts = Counter(processed=len(blocks))
    review = {'language': [], 'specialization': []}
    channels = db['channels']
    for channel in channels.values():
        channel['in_source'] = False
    for block in blocks:
        langs, evidence = languages(block, index, urls, overrides, feeds)
        content_decision, content_evidence = content_filter.decision(block, content_metadata, content_overrides)
        counts['excluded_specialized_all_languages'] += content_decision == 'exclude'
        ident = key(block)
        if ident in channels:
            channels[ident].update(block=block, languages=langs, evidence=evidence, in_source=True)
        if not langs:
            counts['unresolved'] += evidence == 'unresolved' or evidence.startswith('multilingual:')
            counts['no_language_evidence'] += evidence == 'unresolved'
            counts['other_or_review'] += evidence != 'unresolved'
            counts['multilingual_review'] += evidence.startswith('multilingual:')
            counts['known_other'] += evidence not in ('unresolved',) and not evidence.startswith('multilingual:')
            if evidence == 'unresolved' or evidence.startswith('multilingual:'):
                review['language'].append({'block': block, 'reason': evidence,
                                          'feed_languages': feeds.get(attrs(block[0]).get('tvg-id', ''), [])})
            continue
        counts['russian'] += 'rus' in langs
        counts['ukrainian'] += 'ukr' in langs
        counts['both'] += len(langs) == 2
        counts['excluded_specialized_ru_ua'] += content_decision == 'exclude'
        counts['specialization_review_ru_ua'] += content_decision == 'review'
        channel = channels.setdefault(ident, {'block': block, 'history': [], 'status': 'UNKNOWN_CZ'})
        channel.update(languages=langs, evidence=evidence, in_source=True)
    # Import manually added entries verbatim; they still need CZ verification.
    for filename in ('playlist-ru-ua-manual.m3u', 'playlist-ru-ua.m3u', 'playlist-ru-ua-testing.m3u'):
        path = ROOT / filename
        if path.exists():
            for block in parse(path.read_text(encoding='utf-8-sig')):
                ident = key(block)
                if ident in channels:
                    continue
                langs, evidence = languages(block, index, urls, overrides, feeds)
                channels[ident] = {'block': block, 'history': [], 'status': 'UNKNOWN_CZ',
                                  'languages': langs, 'evidence': evidence,
                                  'manual': True, 'in_source': False}
    for channel in channels.values():
        langs, language_evidence = languages(channel['block'], index, urls, overrides, feeds)
        channel.update(languages=langs, evidence=language_evidence)
        decision, evidence = content_filter.decision(channel['block'], content_metadata, content_overrides)
        channel.update(content_decision=decision, content_evidence=evidence)
        if decision == 'review':
            review['specialization'].append({'block': channel['block'], 'reason': evidence})
    db['review_needed'] = review
    db['counts'] = dict(counts)
    db['source_sha256'] = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    db['catalogued_at'] = now()
    return db


def render(db, status):
    source_lines = SOURCE.read_text(encoding='utf-8-sig').splitlines()
    header = []
    for line in source_lines:
        if line.startswith('#EXTINF:'):
            break
        if line:
            header.append(line)
    overrides, oled = categories.settings(DATA)
    blocks = [categories.with_group(c['block'], group) for c in db['channels'].values()
              if content_filter.eligible(c) and c['status'] in status
              for group in categories.groups(c, overrides, oled)]
    text = '\n'.join(header + [line for b in blocks for line in b]) + '\n'
    if blocks:
        if len(parse(text)) != len(blocks):
            raise ValueError('Output validation failed')
    return text, len(blocks)


def publish(db, files=None):
    # Prepare and validate BOTH before touching either output.
    stable, count = render(db, {'STABLE_CZ'})
    testing, test_count = render(db, {'UNKNOWN_CZ', 'UNSTABLE_CZ'})
    writes = {}
    if count:
        writes[ROOT / 'playlist-ru-ua.m3u'] = stable
    if test_count:
        writes[ROOT / 'playlist-ru-ua-testing.m3u'] = testing
    if files is None:
        transaction(writes)
    else:
        files.update(writes)
    return {'stable_entries': count, 'testing_entries': test_count,
            'stable_streams': sum(content_filter.eligible(c) and c['status'] == 'STABLE_CZ' for c in db['channels'].values()),
            'testing_streams': sum(content_filter.eligible(c) and c['status'] in {'UNKNOWN_CZ', 'UNSTABLE_CZ'} for c in db['channels'].values()),
            'main_updated': bool(count),
            'main_file_entries': count if count else existing_entries('playlist-ru-ua.m3u'),
            'note': 'Existing output retained when replacement would be empty'}


def existing_entries(filename):
    path = ROOT / filename
    return len(parse(path.read_text(encoding='utf-8-sig'))) if path.exists() else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh-metadata', action='store_true')
    args = parser.parse_args()
    db = build(load(), args.refresh_metadata)
    # Reclassification requires a completed CZ cycle, but stale evidence expires
    # even while the Windows checker is switched off.
    import time
    for channel in db['channels'].values():
        successes = [h for h in channel['history'] if h.get('result') == 'ok']
        if channel['status'] == 'STABLE_CZ' and (not successes or time.time()-datetime.fromisoformat(successes[-1]['at']).timestamp() > 7*86400):
            channel['status'] = 'UNKNOWN_CZ'
    # A catalogue refresh must never promote channels or interrupt a probe cycle.
    report = {'at': now(), **db['counts'], 'catalogue_entries': len(db['channels'])}
    report['language_candidates'] = sum(bool(c.get('languages')) for c in db['channels'].values())
    report['eligible_candidates'] = sum(content_filter.eligible(c) for c in db['channels'].values())
    report['statuses'] = status_counts(db)
    report['streams_checked'] = 0
    report['cycle_remaining'] = len(db['cycle']['remaining']) if db.get('cycle') else 0
    report.update(categories.statistics(db, DATA))
    files = {}
    if not db.get('cycle'):
        report.update(publish(db, files))
    files.update({DB: json_text(db), DATA / 'ru-ua-report.json': json_text(report)})
    files[DATA / 'review-needed.json'] = json_text(db['review_needed'])
    report_file = DATA / 'reports' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-catalogue.json')
    files[report_file] = json_text(report)
    transaction(files)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        with lock():
            main()
    except Exception as error:
        record_failure('catalogue', error)
        raise
