"""Known public broadcaster player adapters; source retrieval is never CZ proof."""
import json
import urllib.request
from urllib.parse import urlsplit
from filter_ru_ua import DATA,now,save_json

PROFILES={3:('Russia24.ru','Россия 24'),374:('360News.ru','360.ru Новости'),396:('Smotrim100Klassika.ru','Смотрим 100% Классика'),
          987:('Smotrim100Gorkogo.ru','Смотрим 100% Горького'),
          394:('Smotrim100Lyubov.ru','Смотрим 100% Любовь'),
          398:('Smotrim100Muzhskoe.ru','Смотрим 100% Мужское')}

# Exact public broadcasts from the rights holders, not arbitrary user uploads.
RUTUBE_PROFILES={
    '1da5d92af8c55b16241f1eb12a27f00c':('Ohotnikirybolov.ru',23802552),
    '3b7d1499da9396462bfd17282d758d30':('Kinolaffka.ru',141565),
    '4965b7b928c4a143d708ab424be01d37':('Kinozhelezo.ru',141565),
    'e05401be60a9cc719fa7075b6e77b86d':('StarMediaDetectives.ru',141565),
    '0ed0696149c131d3a7349372d730d4e6':('EpicMedia.ru',23480202),
    '21f3993ed8291b70208aa06c982c731f':('EpicMediaLove.ru',23480202),
    '3c47861a8b4b51639e41be092053b966':('Izvestia.ru',23872322),
}

# Discovery only. These public distributor leases are not CZ playback proof
# and must not enter the user playlist merely because the API returns a URL.
VINTERA_PROSPECTS=(30,46,851,852,3316,3317,836,9,306,3299,3321)


def public_live_url(metadata,player,publisher):
    countries=metadata.get('restrictions',{}).get('country',{})
    if (metadata.get('author',{}).get('id')!=publisher or metadata.get('is_official') is not True
        or metadata.get('is_paid') is not False or metadata.get('is_on_air') is not True
        or metadata.get('is_livestream') is not True or 'CZ' in countries.get('restricted',[])
        or (countries.get('allowed') and 'CZ' not in countries['allowed'])
        or player.get('acl_access',{}).get('allowed') is not True):
        raise ValueError('Public rights-holder live stream is not explicitly available for CZ')
    streams=player.get('live_streams',{}).get('hls',[])
    stream=next((s for s in streams if s.get('is_video') and s.get('is_audio') and not s.get('is_dvr')),None)
    url=stream['url'] if stream else ''
    parts=urlsplit(url)
    if parts.scheme!='https' or parts.hostname!='bl.rutube.ru' or not parts.path.startswith('/livestream/'):
        raise ValueError('Unexpected public live transport; keep previous sources for review')
    return url


def refresh():
    path=DATA/'personal-discoveries.json'
    records=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    state=DATA/'main-catalogue.json'
    catalogue=json.loads(state.read_text(encoding='utf-8')) if state.exists() else {}
    offline={v['block'][-1] for c in catalogue.get('channels',{}).values() for v in c['variants'].values()
             if v.get('status')=='OFFLINE'}
    audit=[]
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for ident,(channel,title) in PROFILES.items():
        api=f'https://player-api.smotrim.ru/api/v1/channel/{ident}'
        row={'channel':channel,'source':api,'at':now()}
        try:
            with opener.open(api,timeout=12) as f:response=json.load(f)
            d=response.get('data',{});url=d.get('streams',{}).get('m3u8','')
            parts=urlsplit(url)
            if response.get('status')!='OK' or d.get('title')!=title or d.get('isEnabledToEmbed') is not True:
                raise ValueError('Official player is not explicitly available for embedding')
            if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password:
                raise ValueError('Unsupported broadcaster media URL')
            record={'url':url,'evidence':api,'page':d.get('shareLink'),'language':'rus',
                    'reviewed_at':now(),'method':'Anonymous official player API, exact title and isEnabledToEmbed=true'}
            existing=records.setdefault(channel,[])
            # Keep every old/manual/working URL, even when the public player changes.
            old=next((r for r in existing if r['url']==url),None)
            if old is None:existing.append(record)
            row['ok']=True
        except Exception as error:row.update(ok=False,error=type(error).__name__)
        audit.append(row)
    for video,(channel,publisher) in RUTUBE_PROFILES.items():
        api=f'https://rutube.ru/api/play/options/{video}/?format=json'
        row={'channel':channel,'source':api,'at':now()}
        try:
            with opener.open(f'https://rutube.ru/api/video/{video}/?format=json',timeout=12) as f:metadata=json.load(f)
            with opener.open(api,timeout=12) as f:player=json.load(f)
            url=public_live_url(metadata,player,publisher)
            # Signed public live addresses can have a multi-year lease. Keep a
            # still-valid reviewed address, so refreshing metadata does not reset
            # its three independent CZ observations every six hours.
            from urllib.parse import parse_qs
            import time
            existing=records.setdefault(channel,[])
            reusable=any(r['url'] not in offline and r.get('evidence')==api and urlsplit(r['url']).path==urlsplit(url).path
                         and int(parse_qs(urlsplit(r['url']).query).get('e',['0'])[0])>time.time()+7*86400
                         for r in existing)
            if not reusable and not any(r['url']==url for r in existing):
                existing.append({'url':url,'evidence':api,'page':metadata['video_url'],'language':'rus',
                                 'reviewed_at':now(),'method':'Anonymous official rights-holder live API; free, on-air, CZ allowed; exact HLS URL'})
            row['ok']=True
        except Exception as error:row.update(ok=False,error=type(error).__name__)
        audit.append(row)
    import os
    prospects={'at':now(),'issuer':'github_hosted' if os.environ.get('GITHUB_ACTIONS')=='true' else 'local',
               'region_proof':'UNKNOWN_CZ; metadata retrieval is not playback verification','channels':[],'errors':[]}
    for ident in VINTERA_PROSPECTS:
        api=f'https://cms.vinteratv.com/api/channels/{ident}'
        try:
            with opener.open(api,timeout=8) as f:channel=json.load(f)['data']
            prospects['channels'].append({'provider_id':ident,'name':channel['name'],'url':channel['stream_url'],
                                         'evidence':api,'page':f'https://vinteratv.com/tv/{ident}',
                                         'publication_hold':'Short-lived distributor lease; needs CZ decoding, language and sustainable renewal proof'})
        except Exception as error:prospects['errors'].append({'provider_id':ident,'error':type(error).__name__})
    if prospects['channels']:save_json(DATA/'personal-vintera-prospects.json',prospects)
    save_json(path,records);save_json(DATA/'personal-source-report.json',{'at':now(),'sources':audit})
    return audit


if __name__=='__main__':print(json.dumps(refresh(),ensure_ascii=True))
