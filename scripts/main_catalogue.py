"""One canonical channel per main M3U; all alternative streams stay in the database."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import unicodedata

import filter_ru_ua as common
import movie_sources as sources
from ru_ua_categories import with_group

ROOT, DATA = common.ROOT, common.DATA
STATE = DATA/'main-catalogue.json'
PLAYLIST = ROOT/'playlist.m3u'
REVIEWS = DATA/'movie-reviews.json'
REPORT = ROOT/'playlist-movies-report.md'
MIN_INTERVAL = 600
MAX_AGE = 7*86400
GROUPS = ['КИНО — 4K UHD', 'КИНО — Full HD', 'КИНО — HD', 'КИНО — SD']


def name_of(block):
    quoted = False
    for i, char in enumerate(block[0]):
        if char == '"': quoted = not quoted
        if char == ',' and not quoted: return block[0][i+1:]
    raise ValueError('Missing channel name')


def normalize(name):
    name = unicodedata.normalize('NFKC', name).casefold().replace('ё', 'е')
    name = re.sub(r'\b(?:\d{3,4}[pi]|uhd|fhd|full\s*hd|hdtv|hd|sd|4k|8k|hevc|h[.]?26[45])\b', ' ', name)
    return re.sub(r'[^\w+]+', '', name, flags=re.U)


def stream_key(block):
    a = common.attrs(block[0])
    headers = {k:v for k,v in a.items() if k not in {'tvg-id','tvg-name','tvg-logo','tvg-language','group-title'}}
    material = [block[-1], headers, [s for s in block[1:-1] if not s.startswith('#EXTGRP:')]]
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Metadata:
    def __init__(self):
        self.channels = {c['id']:c for c in json.loads((sources.CACHE/'channels.json').read_text(encoding='utf-8'))}
        self.ids = {i.casefold():i for i in self.channels}
        self.names = defaultdict(set)
        for ident, channel in self.channels.items():
            for name in [channel['name']]+channel.get('alt_names', []):
                self.names[normalize(name)].add(ident)
        self.feeds, self.languages = {}, defaultdict(set)
        for feed in json.loads((sources.CACHE/'feeds.json').read_text(encoding='utf-8')):
            self.feeds[feed['channel']+'@'+feed['id']] = feed['languages']
            self.languages[feed['channel']].update(feed['languages'])
        self.reviews = json.loads(REVIEWS.read_text(encoding='utf-8'))
        discoveries=DATA/'personal-discoveries.json'
        self.discoveries=json.loads(discoveries.read_text(encoding='utf-8')) if discoveries.exists() else {}

    def identity(self, block):
        attrs = common.attrs(block[0])
        ident = attrs.get('tvg-id','').split('@')[0]
        alias = self.reviews.get('aliases',{}).get(normalize(name_of(block)))
        if alias: return alias
        if ident.casefold() in self.ids: return self.ids[ident.casefold()]
        matches = self.names[normalize(attrs.get('tvg-name') or name_of(block))]
        if len(matches) == 1: return next(iter(matches))
        # An unrecognised EPG identifier remains stronger than a shared logo/name.
        if ident:return ident
        corroboration=attrs.get('tvg-logo') or block[-1]
        return 'name:'+normalize(name_of(block))+':'+hashlib.sha256(corroboration.encode()).hexdigest()[:16]

    def describe(self, block, ident):
        attrs = common.attrs(block[0])
        channel = self.channels.get(ident,{})
        categories = set(channel.get('categories', []))
        review = self.reviews.get('channels',{}).get(ident,{})
        movie = 'movies' in categories or bool(re.search(r'кино|cinema|\bmovies?\b|фильм|\bfilms?\b',name_of(block),re.I)) or review.get('movie',False)
        genre_ok = review.get('movie') is True or ('movies' in categories and not categories & {'news','sports','music','general'})
        if 'movie' in review:
            movie=bool(review['movie']);genre_ok=bool(review['movie'])
        langs = self.feeds.get(attrs.get('tvg-id',''), self.languages.get(ident,set()))
        langs=set(langs)
        language_aliases={'ru':'rus','rus':'rus','russian':'rus','русский':'rus','uk':'ukr','ukr':'ukr','ukrainian':'ukr','українська':'ukr'}
        for token in re.split('[;,/]',attrs.get('tvg-language','').casefold()):
            if token.strip() in language_aliases:langs.add(language_aliases[token.strip()])
        blocked = 'xxx' in categories or attrs.get('group-title','').casefold() == 'xxx'
        if review.get('excluded') is True: blocked = True
        return {'movie_candidate': movie, 'movie_confirmed': genre_ok,
                'catalogue_languages': sorted(langs), 'adult_specialization': blocked,
                'name': review.get('name',channel.get('name',name_of(block)))}


def load():
    if not STATE.exists(): return {'version': 1, 'channels': {}, 'selected': {}}
    result = json.loads(STATE.read_text(encoding='utf-8'))
    if result.get('version') != 1 or not isinstance(result.get('channels'),dict): raise ValueError('Invalid main catalogue')
    return result


def status(variant, clock=None):
    clock = datetime.now(timezone.utc).timestamp() if clock is None else clock
    observations = [h for h in variant['history'] if h.get('region')=='CZ' and h.get('egress_sha256')
                    and 0 <= clock-datetime.fromisoformat(h['at']).timestamp() <= MAX_AGE]
    spaced = []
    for h in sorted(observations,key=lambda h:h['at']):
        if not spaced or datetime.fromisoformat(h['at']).timestamp()-datetime.fromisoformat(spaced[-1]['at']).timestamp() >= MIN_INTERVAL:
            spaced.append(h)
    outcomes = [h['result'] for h in spaced if h['result']!='unknown']
    if outcomes[-3:]==['ok']*3: return 'STABLE_CZ'
    if outcomes[-3:]==['fail']*3: return 'OFFLINE'
    if outcomes[-2:]==['geo']*2: return 'GEO_BLOCKED_CZ'
    if variant.get('was_stable') and outcomes and outcomes[-1] in ('fail','geo','quality_fail') and len(outcomes)>1 and outcomes[-2]=='ok':
        return 'STABLE_CZ'
    if 'quality_fail' in outcomes:return 'UNSTABLE_CZ'
    if 'ok' in outcomes and any(s in ('fail','geo') for s in outcomes): return 'UNSTABLE_CZ'
    return 'UNKNOWN_CZ'


def latest(variant):
    return next((h for h in reversed(variant['history']) if h['result']=='ok'), {})


def rights(ident, variant, metadata):
    rules = metadata.reviews.get('channels',{}).get(ident,{}).get('rights',[])
    evidence=next((r['evidence'] for r in rules if r.get('evidence') and variant['block'][-1] in r.get('urls',[])), '')
    if not evidence:
        records=getattr(metadata,'discoveries',{}).get(ident,[])
        evidence=next((r['evidence'] for r in records if r.get('evidence') and r.get('reviewed_at') and r.get('url')==variant['block'][-1]),'')
    return evidence


def add(db, metadata, block, provider, source_url='', baseline=False, header=''):
    ident = metadata.identity(block)
    described = metadata.describe(block,ident)
    channel = db['channels'].setdefault(ident, {'variants':{}, 'groups':[], 'baseline':False})
    described['catalogue_languages']=sorted(set(channel.get('catalogue_languages',[]))|set(described['catalogue_languages']))
    channel.update(described)
    if baseline:
        channel['baseline'] = True
        groups = common.attrs(block[0]).get('group-title','')
        for group in groups.split(';'):
            if group and group not in channel['groups']: channel['groups'].append(group)
    key = stream_key(block)
    variant = channel['variants'].setdefault(key, {'block':block,'history':[], 'sources':[], 'existing':False})
    if baseline: variant['existing'] = True
    provenance = {'provider':provider, 'url':source_url, 'header':header}
    if provenance not in variant['sources']: variant['sources'].append(provenance)
    variant['rights_evidence'] = rights(ident,variant,metadata)
    return ident,key


def collect(refresh=False):
    audit = sources.refresh() if refresh else json.loads(sources.AUDIT.read_text(encoding='utf-8'))
    if refresh:
        import personal_sources
        personal_sources.refresh()
    metadata, db = Metadata(), load()
    migrated={}
    for old_id,channel in db['channels'].items():
        for key,variant in channel['variants'].items():
            ident=metadata.identity(variant['block'])
            target=migrated.setdefault(ident,{**channel,'variants':{},'groups':channel['groups'][:]})
            if key in target['variants']:
                old=target['variants'][key]
                for sample in variant['history']:
                    if sample not in old['history']:old['history'].append(sample)
                old['history'].sort(key=lambda h:h['at'])
            else:target['variants'][key]=variant
            for group in channel['groups']:
                if group not in target['groups']:target['groups'].append(group)
            if old_id in db['selected']:db['selected'].setdefault(ident,db['selected'][old_id])
    db['channels']=migrated
    before = common.parse(PLAYLIST.read_text(encoding='utf-8-sig'))
    prior_identities = {metadata.identity(b) for b in before}
    # The first backup is immutable; last-good snapshots are additionally stored per publication.
    initial = ROOT/'backups/playlist-before-movies.m3u'
    if not initial.exists():
        initial.parent.mkdir(parents=True,exist_ok=True)
        initial.write_bytes(PLAYLIST.read_bytes())
    current = {}
    for block in before:
        ident,key = add(db,metadata,block,'existing playlist.m3u',baseline=True)
        current.setdefault(ident,key)
    db.setdefault('initial',{'at':common.now(),'entries':len(before),'selected':current.copy(),
                             'sha256':hashlib.sha256(PLAYLIST.read_bytes()).hexdigest()})
    baseline_urls = {s['url'] for s in json.loads((ROOT/'channels.json').read_text(encoding='utf-8')).get('sources',[]) if s.get('enabled')}
    scanned, movie_rows = 0, 0
    for provider,url,header,block in sources.entries(audit):
        scanned += 1
        ident = metadata.identity(block)
        details = metadata.describe(block,ident)
        # Complete indexes are scanned. Other-language films remain candidates only if language is ambiguous.
        # A nominally English channel may have a Russian audio rendition. Keep
        # all movie candidates; only the actual decoded audio decides admission.
        is_movie = details['movie_candidate']
        movie_rows += bool(is_movie)
        if is_movie or (url in baseline_urls and (not details['movie_candidate'] or ident in prior_identities)):
            add(db,metadata,block,provider,url,baseline=(url in baseline_urls and not details['movie_candidate']),header=header)
    for item in json.loads((ROOT/'channels.json').read_text(encoding='utf-8')).get('channels',[]):
        block = [f'#EXTINF:-1 tvg-logo="{item.get("logo","")}" group-title="{item.get("group","Favorites")}" tvg-language="{item.get("language","")}",{item["name"]}',item['url']]
        common.parse('#EXTM3U\n'+'\n'.join(block)+'\n')
        add(db,metadata,block,'channels.json manual',baseline=True)
    for ident,review in metadata.reviews.get('channels',{}).items():
        for rule in review.get('rights',[]):
            for url in rule.get('urls',[]):
                channel = db['channels'].get(ident)
                if channel:
                    original = next(iter(channel['variants'].values()))['block']
                    block = original[:-1]+[url]
                    add(db,metadata,block,'documented broadcaster variant',rule['evidence'])
    discoveries=DATA/'personal-discoveries.json'
    if discoveries.exists():
        for ident,records in json.loads(discoveries.read_text(encoding='utf-8')).items():
            if ident not in db['channels']:continue
            for record in records:
                if not record.get('evidence') or not record.get('reviewed_at'):continue
                ch=db['channels'][ident]
                logo=next((common.attrs(v['block'][0]).get('tvg-logo') for v in ch['variants'].values() if common.attrs(v['block'][0]).get('tvg-logo')),'')
                block=[f'#EXTINF:-1 tvg-id="{ident}" tvg-logo="{logo}" tvg-language="{record["language"]}",{ch["name"]}',record['url']]
                add(db,metadata,block,'official public player',record['evidence'])
    # Re-evaluate manual reviews for retained/temporarily missing alternatives as well.
    for ident,channel in db['channels'].items():
        for variant in channel['variants'].values():
            variant['rights_evidence'] = rights(ident,variant,metadata)
            variant['status'] = status(variant)
            variant['audio_reviews'] = metadata.reviews.get('channels',{}).get(ident,{}).get('audio_reviews',[])
    db['current'] = current
    db['scan'] = {'at':common.now(),'before_entries':len(before), 'before_identities':len(prior_identities),
                  'sources_processed':len(audit['sources']), 'providers_available':audit['providers_available'],
                  'source_entries':scanned,'movie_candidate_rows':movie_rows,
                  'primary_available':any(r['ok'] and r['url'] in baseline_urls for r in audit['sources'])}
    return db, metadata


def quality(variant):
    evidence = latest(variant)
    media = evidence.get('media',{})
    samples = variant['history'][-6:]
    reliability = sum(h['result']=='ok' for h in samples)/max(1,len(samples))
    compatible = media.get('video_codec') in ('h264','hevc','mpeg2video') and media.get('audio_codec') in ('aac','mp3','ac3','eac3','mp2')
    return (reliability, media.get('width',0)*media.get('height',0), compatible,
            media.get('bitrate',0), -(evidence.get('startup_seconds') or 999))


def resolution(media):
    width,height = media.get('width',0),media.get('height',0)
    if width >=3840 and height>=2160:return GROUPS[0]
    if width >=1920 and height>=1080:return GROUPS[1]
    if width >=1280 and height>=720:return GROUPS[2]
    if width and height:return GROUPS[3]
    raise ValueError('Unknown measured video resolution')


def eligible_movie(channel,variant):
    h = latest(variant)
    return bool(channel['movie_confirmed'] and not channel['adult_specialization'] and variant.get('rights_evidence')
            and variant['status']=='STABLE_CZ' and h.get('russian_audio_confirmed') is True
            and h.get('audio_decoded') is True and bool(h.get('media',{}).get('width'))
            and h.get('media',{}).get('russian_default',True))


def choose(db,metadata):
    selected, improved, added, removed = {}, [], [], []
    for ident,channel in db['channels'].items():
        variants = channel['variants']
        candidates = [(key,v) for key,v in variants.items() if eligible_movie(channel,v)] if channel['movie_candidate'] else [
            (key,v) for key,v in variants.items() if v.get('existing') and v['status']=='STABLE_CZ']
        if candidates:
            key,variant = max(candidates,key=lambda kv:quality(kv[1]))
            if ident in db['current'] and key != db['current'][ident]: improved.append(ident)
            elif ident not in db['current']: added.append(ident)
        elif channel['baseline'] and not channel.get('suppressed'):
            old_key = db['selected'].get(ident) or db['current'].get(ident)
            key = old_key if old_key in variants else next(k for k,v in variants.items() if v.get('existing'))
            variant = variants[key]
            if variant['status'] in ('OFFLINE','GEO_BLOCKED_CZ') and all(v['status'] in ('OFFLINE','GEO_BLOCKED_CZ') for v in variants.values()):
                removed.append(ident)
                channel['suppressed']=True
                continue
        else:
            continue
        channel['suppressed']=False
        block = variant['block'][:]
        original_groups = channel['groups']
        oled = metadata.reviews.get('channels',{}).get(ident,{}).get('oled',{})
        oled_ok = oled.get('approved') is True and oled.get('evidence') and oled.get('reviewed_at')
        if 'OLED' in original_groups or oled_ok:
            block = with_group(block,'OLED')
        elif eligible_movie(channel,variant):
            block = with_group(block,resolution(latest(variant)['media']))
        elif original_groups:
            block = with_group(block,';'.join(original_groups))
        selected[ident] = (key,block)
    # Mass source outages are a no-publication condition, not a reason to delete entries.
    if not db['scan']['primary_available'] or db['scan']['providers_available'] < 3:
        raise RuntimeError('Source availability gate: retaining main playlist')
    before = set(db['current'])
    lost = before-set(selected)
    if lost-set(removed) or len(lost)>max(2,int(len(before)*.02)):
        raise RuntimeError('Mass deletion guard: retaining main playlist')
    if not selected:raise ValueError('Empty main playlist prohibited')
    groups = Counter(common.attrs(b[0]).get('group-title','') for _,b in selected.values())
    ordered = sorted(selected.items(), key=lambda kv:(common.attrs(kv[1][1][0]).get('group-title','').casefold(),name_of(kv[1][1]).casefold().replace('ё','е')))
    old_header = PLAYLIST.read_text(encoding='utf-8-sig').splitlines()[0]
    # Retain current and source EPG URLs. These are programme data, not language evidence.
    epg = set()
    for header in [old_header]+[s.get('header','') for ch in db['channels'].values() for v in ch['variants'].values() for s in v['sources']]:
        a = common.attrs(header)
        for value in (a.get('x-tvg-url',''),a.get('url-tvg','')):
            epg.update(u.strip() for u in value.split(',') if u.strip())
    header = re.sub(r'\s+(?:x-tvg-url|url-tvg)="[^"]*"','',old_header)
    if epg:header += ' x-tvg-url="'+','.join(sorted(epg))+'"'
    # Preserve global HTTP/options preceding the first entry in the current main file.
    global_lines = []
    for line in PLAYLIST.read_text(encoding='utf-8-sig').splitlines()[1:]:
        if line.startswith('#EXTINF:'):break
        if line:global_lines.append(line)
    text='\n'.join([header]+global_lines+[line for _,(_,block) in ordered for line in block])+'\n'
    parsed=common.parse(text)
    if len(parsed)!=len(selected) or len({metadata.identity(b) for b in parsed})!=len(parsed):
        raise ValueError('Duplicate or corrupt output')
    db['selected']={ident:key for ident,(key,_) in selected.items()}
    original=db.get('initial',{'entries':db['scan']['before_entries'],'selected':db['current']})
    return text, {'before_channels':db['scan']['before_entries'], 'movie_channels_found':sum(ch['movie_confirmed'] and 'rus' in ch['catalogue_languages'] for ch in db['channels'].values()),
                  'movie_candidates_pending_language':sum(ch['movie_candidate'] and not ch['catalogue_languages'] for ch in db['channels'].values()),
                  'duplicates_removed':db['scan']['before_entries']-db['scan']['before_identities'],
                  'unique_movies_added':len(added),'existing_streams_improved':len(improved),
                  'confirmed_unavailable_removed':len(removed), 'after_channels':len(selected),
                  'initial_channels':original['entries'],
                  'duplicates_removed_since_initial':original['entries']-len(original['selected']),
                  'unique_movies_added_since_initial':sum(ident not in original['selected'] and eligible_movie(db['channels'][ident],db['channels'][ident]['variants'][key]) for ident,(key,_) in selected.items()),
                  'existing_streams_improved_since_initial':sum(ident in original['selected'] and key!=original['selected'][ident] and db['channels'][ident]['variants'][key]['status']=='STABLE_CZ' for ident,(key,_) in selected.items()),
                  'stable_movie_channels':sum(eligible_movie(db['channels'][ident],db['channels'][ident]['variants'][key]) for ident,(key,_) in selected.items()),
                  'archived_stream_variants':sum(len(ch['variants']) for ch in db['channels'].values()),
                  'movie_variants_with_russian_metadata':sum(len(ch['variants']) for ch in db['channels'].values() if ch['movie_confirmed'] and 'rus' in ch['catalogue_languages']),
                  'verified_movie_resolution_counts':dict(Counter(resolution(latest(db['channels'][ident]['variants'][key])['media']) for ident,(key,_) in selected.items() if eligible_movie(db['channels'][ident],db['channels'][ident]['variants'][key]))),
                  'main_groups':dict(groups),'added':added,'improved':improved,'removed':removed}


def report(db,summary):
    variants=[v for ch in db['channels'].values() if ch['movie_candidate'] for v in ch['variants'].values()]
    checks=Counter(v['status'] for v in variants)
    rows=['# Киноканалы в существующем playlist.m3u','',f'Обновлено: {common.now()} (UTC).', '',
          'Постоянный URL: https://raw.githubusercontent.com/Denisczua/My-IPTV/main/playlist.m3u','',
          '| Показатель | Значение |','|---|---:|']
    labels={'initial_channels':'Записей до интеграции','movie_channels_found':'Кино-кандидатов с русским языком по метаданным',
            'movie_candidates_pending_language':'Кандидатов с неясным языком',
            'duplicates_removed_since_initial':'Удалено повторных записей с начала интеграции',
            'unique_movies_added_since_initial':'Добавлено новых уникальных подтверждённых киноканалов',
            'existing_streams_improved_since_initial':'Выбрано улучшенных стабильных потоков для прежних каналов',
            'confirmed_unavailable_removed':'Удалено уникальных каналов после подтверждённых отказов за этот запуск',
            'after_channels':'Итого уникальных телеканалов в основном M3U',
            'stable_movie_channels':'Киноканалов с действующим полным подтверждением STABLE_CZ',
            'archived_stream_variants':'Всего вариантов потоков в резервной базе',
            'movie_variants_with_russian_metadata':'Вариантов киноканалов с русским языком в метаданных'}
    rows += [f'| {label} | {summary[key]} |' for key,label in labels.items() if key in summary]
    rows += ['',f"Источников-проектов доступно: {db['scan']['providers_available']}/5. Проверено адресов каталогов: {db['scan']['sources_processed']}.",
             f"Полностью просмотрено записей каталогов: {db['scan']['source_entries']}.",
             f'Статусы вариантов фильмовых каналов: `{dict(checks)}`.', '',
             'Существующие каналы сохранены без заявления об их текущей работоспособности. Новые киноканалы/улучшения требуют трёх разнесённых CZ-проверок, подтверждения русской дорожки и права бесплатного доступа к точному URL.',
             'Дубликаты определяются по EPG/tvg-id, уникальным метаданным названия и ручным алиасам. Совпадение логотипа само по себе ничего не объединяет. Все резервные URL и исходные блоки находятся в data/main-catalogue.json.', '',
             '| Канал | Вариантов | Измеренные характеристики | Русская дорожка | CZ | Последний медиазамер UTC | Публикуется |',
             '|---|---:|---|---|---|---|---|']
    for ident,ch in sorted(db['channels'].items(),key=lambda kv:kv[1]['name'].casefold()):
        if not ch['movie_candidate'] or (ch['catalogue_languages'] and 'rus' not in ch['catalogue_languages']):continue
        v=ch['variants'].get(db.get('selected',{}).get(ident),next(iter(ch['variants'].values())))
        h=next((sample for sample in reversed(v['history']) if sample.get('media')),latest(v)); m=h.get('media',{})
        media=(f"{m.get('width','?')}×{m.get('height','?')} / {m.get('video_codec','?')} + {m.get('audio_codec','?')} / {m.get('fps','?')} fps / "
               +(str(m['bitrate'])+' бит/с' if m.get('bitrate') else 'битрейт не определён')) if m else 'Не измерены'
        safe=ch['name'].replace('|','/')
        published=('да — подтверждён' if eligible_movie(ch,v) else 'да — существовавший ранее') if ident in db.get('selected',{}) else 'нет — кандидат'
        rows.append(f"| {safe} | {len(ch['variants'])} | {media} | {'подтверждена по дорожке' if h.get('russian_audio_confirmed') else 'не подтверждена'} | {v['status']} | {h.get('at','—')} | {published} |")
    return '\n'.join(rows)+'\n'


def run(refresh=False,publish=True):
    db,metadata=collect(refresh)
    files={}
    summary={'published':False,**db['scan']}
    personal_policy=DATA/'personal-policy.json'
    if personal_policy.exists() and json.loads(personal_policy.read_text(encoding='utf-8')).get('preserve_full_playlist'):
        publish=False
        summary['full_playlist_preserved']=True
        summary.update(before_channels=db['scan']['before_entries'],after_channels=db['scan']['before_entries'],
                       archived_stream_variants=sum(len(ch['variants']) for ch in db['channels'].values()),
                       movie_channels_found=sum(ch['movie_confirmed'] and 'rus' in ch['catalogue_languages'] for ch in db['channels'].values()))
    if publish:
        text,details=choose(db,metadata)
        summary.update(details,published=True)
        if text!=PLAYLIST.read_text(encoding='utf-8-sig'):
            files[DATA/'playlist-last-good.m3u']=PLAYLIST.read_text(encoding='utf-8-sig')
            files[PLAYLIST]=text
    files[STATE]=common.json_text(db)
    files[DATA/'main-update-report.json']=common.json_text(summary)
    files[REPORT]=report(db,summary)
    common.transaction(files)
    print(json.dumps(summary,ensure_ascii=True,indent=2))
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh',action='store_true')
    parser.add_argument('--collect-only',action='store_true')
    args=parser.parse_args()
    with common.lock():run(args.refresh,not args.collect_only)
