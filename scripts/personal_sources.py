"""Known public broadcaster player adapters; source retrieval is never CZ proof."""
import json
import urllib.request
from urllib.parse import urlsplit
from filter_ru_ua import DATA,now,save_json

PROFILES={3:('Russia24.ru','Россия 24'),374:('360News.ru','360.ru Новости'),396:('Smotrim100Klassika.ru','Смотрим 100% Классика')}


def refresh():
    path=DATA/'personal-discoveries.json'
    records=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
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
    save_json(path,records);save_json(DATA/'personal-source-report.json',{'at':now(),'sources':audit})
    return audit


if __name__=='__main__':print(json.dumps(refresh(),ensure_ascii=True))
