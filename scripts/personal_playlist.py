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


def audio_proof(variant,group):
    current=main.latest(variant).get('media',{})
    allowed={'uk','ukr','ukrainian','ru','rus','russian'} if group=='Новости Украины' else {'ru','rus','russian'}
    language=str(current.get('audio_language','und')).lower()
    if language not in allowed|{'und','none',''}:return None
    now=datetime.now(timezone.utc).timestamp()
    for h in reversed(variant['history']):
        media=h.get('media',{})
        if h.get('invalidated_reason') or h.get('measurement_version')!=3 or h.get('result')!='ok' or not h.get('egress_sha256') or h.get('region')!='CZ':continue
        if not 0<=now-datetime.fromisoformat(h['at']).timestamp()<=86400:continue
        if media.get('selected_audio_index')!=current.get('selected_audio_index') or media.get('default_audio_index')!=current.get('default_audio_index'):continue
        if group=='Кино':
            speech=h.get('speech_language',{})
            measured=(speech.get('language')=='ru' and speech.get('probability',0)>=.95 and speech.get('speech_seconds',0)>=5 and speech.get('characters',0)>=30)
            listened=h.get('russian_audio_confirmed') and h.get('audio_language_evidence','').startswith('Dated manual listening')
            if not (measured or listened):continue
        if str(media.get('audio_language','')).lower() in allowed or (h.get('russian_audio_confirmed') and 'rus' in allowed):return h
    return None


def confirmed(ident,channel,variant,group,policy,discoveries):
    # Cached status strings and old measurements are not publication evidence.
    history=[h for h in variant['history'] if h.get('measurement_version')==3 and not h.get('invalidated_reason')]
    spaced=[];now=datetime.now(timezone.utc).timestamp()
    for h in sorted(history,key=lambda h:h['at']):
        at=datetime.fromisoformat(h['at']).timestamp()
        if h.get('region')!='CZ' or not h.get('egress_sha256') or not 0<=now-at<=main.MAX_AGE:continue
        if not spaced or at-datetime.fromisoformat(spaced[-1]['at']).timestamp()>=main.MIN_INTERVAL:spaced.append(h)
    outcomes=[h['result'] for h in spaced if h['result']!='unknown']
    proven_before=any(outcomes[i:i+3]==['ok']*3 for i in range(max(0,len(outcomes)-2)))
    if main.status({**variant,'history':history,'was_stable':proven_before})!='STABLE_CZ':return False,'not_stable_cz'
    h=next((h for h in reversed(history) if h.get('result')=='ok'),{})
    if not h.get('audio_decoded') or not h.get('video_continuous') or not h.get('audio_continuous'):
        return False,'decode_not_confirmed'
    media=h.get('media',{})
    if not media.get('width') or not media.get('height'):return False,'resolution_unknown'
    if audio_proof(variant,group) is None:
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
    groups={};excluded=Counter();rows=[];checked=set();working=set();replacements=0;movies=[];observations=set();offline_streams=set()
    all_checked=set();all_observations=set()
    for ident,ch in catalogue['channels'].items():
        for v in ch['variants'].values():
            valid=[h for h in v['history'] if not h.get('invalidated_reason') and h.get('measurement_version')==3 and h.get('region')=='CZ' and h.get('egress_sha256')]
            if valid:all_checked.add(ident)
            all_observations.update(h.get('observation_id',ident+main.stream_key(v['block'])+h['at']) for h in valid)
        group=category(ident,ch,policy)
        if not group:continue
        candidates=[]
        for key,v in ch['variants'].items():
            history=[h for h in v['history'] if not h.get('invalidated_reason') and h.get('measurement_version')==3 and h.get('region')=='CZ' and h.get('egress_sha256')]
            if history:checked.add(ident)
            observations.update(h.get('observation_id',ident+key+h['at']) for h in history)
            if main.status({**v,'history':history,'was_stable':False})=='OFFLINE':offline_streams.add((ident,v['block'][-1]))
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
                 'last_check':h['at'],'audio_language':audio_proof(v,original_group)['media'].get('audio_language','rus'),
                 'audio_language_checked_at':audio_proof(v,original_group)['at'],
                 'audio_language_method':audio_proof(v,original_group).get('audio_language_evidence','FFprobe track language tag'),
                 'rights_evidence':v.get('rights_evidence') or next(r['evidence'] for r in discoveries[ident] if r['url']==v['block'][-1])}
            rows.append((row,block))
            if original_group=='Кино':movies.append(row)
    if not rows:raise ValueError('No eligible personal channels: retain previous deployment; never republish unknown channels')
    order={'Кино':0,'Рыбалка и охота':1,'Путешествия и природа':2,'Новости Украины':3,'Новости России':4,'OLED':5}
    rows.sort(key=lambda item:(order[item[0]['group']],item[0]['name'].casefold().replace('ё','е')))
    body=('#EXTM3U\n'+'\n'.join('\n'.join(block) for _,block in rows)+'\n').encode('utf-8')
    if len(common.parse(body.decode()))!=len({r['id'] for r,_ in rows}):raise ValueError('Duplicate personal channels')
    report={'at':common.now(),'channels':len(rows),'bytes':len(body),'unique_channels_checked':len(checked),
            'total_unique_channels_checked':len(all_checked),'total_independent_observations':len(all_observations),
            'independent_observations':len(observations),'confirmed_offline_streams':len(offline_streams),
            'channels_with_decoded_sample':len(working),'working_replacements':replacements,
            'excluded_stream_reasons':dict(excluded),'groups':dict(Counter(r['group'] for r,_ in rows)),
            'oled':sum(r['group']=='OLED' for r,_ in rows),'movies':movies,'channel_list':[r for r,_ in rows],
            'publication_policy':'Three spaced decoded CZ observations, default RU/UA audio, explicit rights evidence; no unknown entries',
            'ottplayer_connection_confirmed':True}
    return body,report
