"""Fail-closed personal selection: decoded CZ evidence, rights, scope, one channel."""
from collections import Counter
from datetime import datetime,timezone
import re

import filter_ru_ua as common
import main_catalogue as main
from ru_ua_categories import with_group


def category(ident,channel,policy):
    if channel.get('movie_confirmed') and not channel.get('adult_specialization'):
        return 'Кино'
    return next((group for group,rules in policy['groups'].items() if ident in rules['ids']),None)


def confirmed(ident,channel,variant,group,policy,discoveries):
    # Cached status strings and old measurements are not publication evidence.
    history=[h for h in variant['history'] if h.get('measurement_version')==3]
    if main.status({**variant,'history':history})!='STABLE_CZ':return False,'not_stable_cz'
    h=next((h for h in reversed(history) if h.get('result')=='ok'),{})
    if not h.get('audio_decoded') or not h.get('video_continuous') or not h.get('audio_continuous'):
        return False,'decode_not_confirmed'
    media=h.get('media',{})
    if not media.get('width') or not media.get('height'):return False,'resolution_unknown'
    language=str(media.get('audio_language','')).lower()
    allowed={'uk','ukr','ukrainian'} if group=='Новости Украины' else {'ru','rus','russian'}
    if language not in allowed and not (group!='Новости Украины' and h.get('russian_audio_confirmed')):
        return False,'audio_language_unconfirmed'
    # OTTPlayer on Tizen follows default tracks; an alternate Russian track alone
    # is insufficient until a dedicated rendition is checked.
    if media.get('selected_audio_index')!=media.get('default_audio_index'):
        return False,'required_audio_not_default'
    official=any(r.get('url')==variant['block'][-1] and r.get('evidence') and r.get('reviewed_at') for r in discoveries.get(ident,[]))
    if not variant.get('rights_evidence') and not official:return False,'rights_unconfirmed'
    if channel.get('adult_specialization'):return False,'out_of_scope'
    return True,''


def display_name(ident,channel,policy,names,media):
    name=policy.get('names',{}).get(ident) or names.get(ident) or channel['name']
    name=re.sub(r'\s*[\[(](?:not 24/7|geo.?blocked|\d{3,4}p|hd|sd|4k)[\])]','',name,flags=re.I)
    name=re.sub(r'\s+(?:FULL\s*HD|FHD|UHD|HD|SD|4K|\d{3,4}p)\s*$','',name,flags=re.I).strip()
    w,h=media['width'],media['height']
    label='4K' if w>=3840 and h>=2160 else 'Full HD' if w>=1920 and h>=1080 else 'HD' if w>=1280 and h>=720 else ''
    return name+(' '+label if label else '')


def export(catalogue,policy,names,discoveries,oled):
    groups={};excluded=Counter();rows=[];checked=set();working=set();replacements=0;movies=[]
    for ident,ch in catalogue['channels'].items():
        group=category(ident,ch,policy)
        if not group:continue
        candidates=[]
        for key,v in ch['variants'].items():
            history=[h for h in v['history'] if h.get('measurement_version')==3 and h.get('region')=='CZ' and h.get('egress_sha256')]
            if history:checked.add(ident)
            if any(h.get('result')=='ok' for h in history):working.add(ident)
            ok,reason=confirmed(ident,ch,v,group,policy,discoveries)
            if ok:candidates.append((key,v))
            else:excluded[reason]+=1
        if candidates:
            key,v=max(candidates,key=lambda pair:main.quality(pair[1]))
            groups.setdefault(group,[]).append((ident,key,v))
    for group,entries in groups.items():
        entries.sort(key=lambda row:main.quality(row[2]),reverse=True)
        limit=policy['groups'][group]['limit'] if group in policy['groups'] else len(entries)
        for ident,key,v in entries[:limit]:
            ch=catalogue['channels'][ident];h=main.latest(v);media=h['media']
            name=display_name(ident,ch,policy,names,media)
            block=v['block'][:]
            # Find the actual name separator, allowing commas inside attributes.
            old=main.name_of(block);block[0]=block[0][:-len(old)]+name
            original_group=group
            review=oled.get(ident,{})
            if review.get('approved') is True and review.get('evidence') and review.get('reviewed_at') and review.get('method')=='visual':
                group_for_row='OLED'
            else:group_for_row=group
            block=with_group(block,group_for_row)
            old_key=catalogue.get('selected',{}).get(ident)
            if old_key and old_key!=key:replacements+=1
            row={'id':ident,'name':name,'group':group_for_row,'theme':original_group,
                 'resolution':f"{media['width']}×{media['height']}",'status':'STABLE_CZ',
                 'last_check':h['at'],'audio_language':media.get('audio_language','und'),
                 'rights_evidence':v.get('rights_evidence') or next(r['evidence'] for r in discoveries[ident] if r['url']==v['block'][-1])}
            rows.append((row,block))
            if original_group=='Кино':movies.append(row)
    if not rows:raise ValueError('No eligible personal channels: retain previous deployment; never republish unknown channels')
    order={'Кино':0,'Рыбалка и охота':1,'Путешествия и природа':2,'Новости Украины':3,'Новости России':4,'OLED':5}
    rows.sort(key=lambda item:(order[item[0]['group']],item[0]['name'].casefold().replace('ё','е')))
    body=('#EXTM3U\n'+'\n'.join('\n'.join(block) for _,block in rows)+'\n').encode('utf-8')
    if len(common.parse(body.decode()))!=len({r['id'] for r,_ in rows}):raise ValueError('Duplicate personal channels')
    report={'at':common.now(),'channels':len(rows),'bytes':len(body),'unique_channels_checked':len(checked),
            'channels_with_decoded_sample':len(working),'working_replacements':replacements,
            'excluded_stream_reasons':dict(excluded),'groups':dict(Counter(r['group'] for r,_ in rows)),
            'oled':sum(r['group']=='OLED' for r,_ in rows),'movies':movies,'channel_list':[r for r,_ in rows],
            'publication_policy':'Three spaced decoded CZ observations, default RU/UA audio, explicit rights evidence; no unknown entries',
            'ottplayer_connection_confirmed':True}
    return body,report
