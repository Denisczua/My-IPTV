"""Read-only RU/UA view of the main playlist and approved reserve streams."""
from collections import Counter, defaultdict
import re

from filter_ru_ua import attrs, parse
from main_catalogue import eligible_movie, quality, stream_key


def export(blocks, catalogue, feeds):
    by_stream={stream_key(v['block']):ident for ident,ch in catalogue['channels'].items()
               for v in ch['variants'].values()}
    by_channel=defaultdict(set)
    for ident,langs in feeds.items():
        by_channel[ident.split('@')[0]].update(langs)
    aliases={'ru':'rus','rus':'rus','russian':'rus','русский':'rus',
             'uk':'ukr','ukr':'ukr','ukrainian':'ukr','українська':'ukr'}
    def languages(block):
        a=attrs(block[0]); explicit={aliases[t.strip()] for t in re.split('[;,/]',a.get('tvg-language','').casefold()) if t.strip() in aliases}
        tvg=a.get('tvg-id','')
        # A named feed with another language must not inherit Russian from a
        # different regional feed of the same channel.
        if explicit:return explicit
        if tvg in feeds:return set(feeds[tvg])
        union=by_channel.get(tvg.split('@')[0],set())
        return union if union and union <= {'rus','ukr'} else set()
    rows={}; languages_by_id={}; ignored=Counter(); improved=0
    for block in blocks:
        langs=languages(block)
        if not langs & {'rus','ukr'}:
            ignored['other_or_unconfirmed_language']+=1;continue
        ident=by_stream.get(stream_key(block),attrs(block[0]).get('tvg-id') or stream_key(block))
        ch=catalogue['channels'].get(ident,{})
        # Preserve current main selection and all its groups/options by default.
        chosen=block
        candidates=[v for v in ch.get('variants',{}).values()
                    if languages(v['block']) & {'rus','ukr'} and v.get('status')=='STABLE_CZ'
                    and (not ch.get('movie_candidate') or eligible_movie(ch,v))]
        if candidates:
            best=max(candidates,key=quality)
            if stream_key(best['block'])!=stream_key(block):
                chosen=best['block'][:]
                group=attrs(block[0]).get('group-title')
                if group:
                    from ru_ua_categories import with_group
                    chosen=with_group(chosen,group)
                improved+=1
        if ident in rows:
            ignored['duplicates']+=1;continue
        rows[ident]=chosen;languages_by_id[ident]=langs
    added=0
    for ident,ch in catalogue['channels'].items():
        if ident in rows:continue
        candidates=[v for v in ch['variants'].values() if eligible_movie(ch,v)
                    and languages(v['block']) & {'rus','ukr'}]
        if candidates:
            v=max(candidates,key=quality);rows[ident]=v['block'];languages_by_id[ident]=languages(v['block']);added+=1
    if not rows:raise ValueError('No language-confirmed channels; retain deployed site')
    body=('#EXTM3U\n'+'\n'.join('\n'.join(b) for b in rows.values())+'\n').encode('utf-8')
    assert len(parse(body.decode()))==len(rows)
    counts=Counter()
    for ident,block in rows.items():
        ch=catalogue['channels'].get(ident,{})
        v=ch.get('variants',{}).get(stream_key(block),{})
        counts[v.get('status','UNKNOWN_CZ')]+=1
    return body,{'channels':len(rows),'bytes':len(body),'russian':sum('rus' in x for x in languages_by_id.values()),
                 'ukrainian':sum('ukr' in x for x in languages_by_id.values()),'status_counts':dict(counts),
                 'excluded':dict(ignored),'new_approved_movies':added,'improved_streams':improved,
                 'groups':dict(Counter(attrs(b[0]).get('group-title','') for b in rows.values())),
                 'language_basis':'explicit tvg-language or IPTV-org feed; country/name never proves language',
                 'playback_confirmed_for_all':False}
