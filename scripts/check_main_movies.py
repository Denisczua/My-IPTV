"""Bounded direct-CZ checks of decoded video AND audio; no proxy or region bypass."""
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit

import check_ru_ua as transport
import filter_ru_ua as common
import main_catalogue as main

ENV={k:v for k,v in os.environ.items() if 'proxy' not in k.lower()}
PROTOCOLS='http,https,tcp,tls,crypto'


def public_url(url):
    parts=urlsplit(url)
    if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password:
        raise transport.Inconclusive('Only public HTTP(S) streams without embedded credentials are checked')
    addresses=socket.getaddrinfo(parts.hostname,parts.port or (443 if parts.scheme=='https' else 80),type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise transport.Inconclusive('Private/local network endpoint is not a public broadcast')


def public_fetch(url,headers=None,**kwargs):
    public_url(url)
    result=transport.fetch(url,headers,**kwargs)
    public_url(result[1])
    return result


def direct_cz():
    curl=shutil.which('curl.exe') or shutil.which('curl')
    if not curl:raise RuntimeError('curl required for direct IPv4 geolocation')
    identities=[]
    for url,field in [('https://ipwho.is/','country_code'),('https://ipinfo.io/json','country')]:
        r=subprocess.run([curl,'-4','--noproxy','*','-fsS','--max-time','15',url],env=ENV,capture_output=True,check=True,timeout=20)
        d=json.loads(r.stdout)
        if d.get(field)!='CZ' or not d.get('ip'):raise RuntimeError('Direct CZ location not confirmed')
        identities.append(d['ip'])
    if len(set(identities))!=1:raise RuntimeError('Geolocation providers disagree')
    return hashlib.sha256(identities[0].encode()).hexdigest()


def rate(value):
    try:
        a,_,b=value.partition('/')
        return round(float(a)/float(b or 1),3)
    except (ValueError,ZeroDivisionError,AttributeError):return 0


def inspect_stream(url,headers,ffprobe):
    started=time.monotonic()
    command=[ffprobe,'-v','error','-protocol_whitelist',PROTOCOLS,'-rw_timeout','10000000',
             '-headers',''.join(f'{k}: {v}\r\n' for k,v in headers.items()),
             '-read_intervals','%+3','-show_packets',
             '-show_entries','stream=index,codec_type,codec_name,width,height,pix_fmt,field_order,avg_frame_rate,bit_rate,channels,sample_rate:stream_tags=language,title:stream_disposition=default:format=bit_rate:packet=stream_index,size,duration_time,pts_time',
             '-of','json',url]
    result=subprocess.run(command,capture_output=True,text=True,errors='replace',timeout=25,env=ENV)
    if result.returncode:raise transport.StreamFailure('FFprobe failed to read media streams')
    info=json.loads(result.stdout)
    videos=[s for s in info.get('streams',[]) if s.get('codec_type')=='video']
    audios=[s for s in info.get('streams',[]) if s.get('codec_type')=='audio']
    if not videos or not audios:raise transport.StreamFailure('Video or audio stream missing')
    video=max(videos,key=lambda s:s.get('width',0)*s.get('height',0))
    russian=[s for s in audios if s.get('tags',{}).get('language','').lower() in ('rus','ru','russian')]
    default_audio=next((a for a in audios if a.get('disposition',{}).get('default')),audios[0])
    audio=next((a for a in russian if a.get('disposition',{}).get('default')), russian[0] if russian else default_audio)
    def packet_bitrate(index,fallback):
        packets=[p for p in info.get('packets',[]) if p.get('stream_index')==index]
        try:
            duration=sum(float(p.get('duration_time',0)) for p in packets)
            if duration>0:return round(sum(int(p.get('size',0)) for p in packets)*8/duration)
        except (ValueError,TypeError):pass
        return int(fallback or 0)
    media={'width':video.get('width',0),'height':video.get('height',0),
           'video_codec':video.get('codec_name'),'audio_codec':audio.get('codec_name'),
           'audio_language':audio.get('tags',{}).get('language','und'),
           'audio_language_tag':audio.get('tags',{}).get('language','und'),
           'russian_default':bool(russian) and default_audio['index']==audio['index'],
           'audio_tracks':len(audios),'field_order':video.get('field_order','unknown'),
           'selected_audio_index':audio['index'],'default_audio_index':default_audio['index'],
           'fps':rate(video.get('avg_frame_rate','0')),'channels':audio.get('channels'),
           'bitrate':packet_bitrate(video['index'],video.get('bit_rate') or info.get('format',{}).get('bit_rate')),
           'audio_bitrate':packet_bitrate(audio['index'],audio.get('bit_rate')),'sample_rate':audio.get('sample_rate'),
           'probe_startup_seconds':round(time.monotonic()-started,3)}
    return video['index'],audio['index'],bool(russian),media


def decoded_timestamps(text):
    # FFmpeg writes showinfo/ashowinfo from different threads. Their records may
    # share a line, and even lose a prefix. Match the contiguous frame fields,
    # never an arbitrary earlier prefix ("ashowinfo" also contains "showinfo").
    frame=r'\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:(-?\d+(?:\.\d+)?)\s+'
    video=frame+r'(?:duration:\s*-?\d+\s+duration_time:-?[\d.]+\s+)?fmt:\S+\s+(?:cl:\S+\s+)?sar:\S+\s+s:\d+x\d+'
    audio=frame+r'fmt:\S+\s+channels:\d+\s+(?:chlayout:\S+\s+)?rate:\d+\s+nb_samples:\d+'
    return [float(x) for x in re.findall(video,text)],[float(x) for x in re.findall(audio,text)]


def decode(url,headers,video,audio,ffmpeg):
    command=[ffmpeg,'-nostdin','-hide_banner','-nostats','-v','info','-xerror','-err_detect','explode',
             '-threads','2','-filter_threads','1',
             '-protocol_whitelist',PROTOCOLS,'-rw_timeout','10000000',
             '-headers',''.join(f'{k}: {v}\r\n' for k,v in headers.items()),
             '-re','-i',url,'-t','25','-map',f'0:{video}','-map',f'0:{audio}',
             '-vf','setpts=PTS-STARTPTS,showinfo,freezedetect=n=-60dB:d=15',
             '-af','asetpts=PTS-STARTPTS,ashowinfo,silencedetect=n=-55dB:d=15',
             '-progress','pipe:1','-f','null','-']
    started=time.monotonic()
    first_frame=[]
    common.ROOT.joinpath('.tools').mkdir(exist_ok=True)
    with tempfile.TemporaryFile(dir=common.ROOT/'.tools') as log:
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=log,text=True,env=ENV)
        def read():
            for line in process.stdout:
                if line.startswith('frame=') and int(line.partition('=')[2])>0 and not first_frame:
                    first_frame.append(time.monotonic()-started)
        reader=threading.Thread(target=read,daemon=True);reader.start()
        try:process.wait(timeout=55)
        except subprocess.TimeoutExpired:
            process.kill();process.wait();raise
        finally:
            reader.join(timeout=2)
            process.stdout.close()
        log.seek(0);text=log.read().decode('utf-8',errors='replace')
    if process.returncode:raise transport.StreamFailure('FFmpeg video/audio decode error')
    video_pts,audio_pts=decoded_timestamps(text)
    def continuous(pts):
        return len(pts)>10 and pts[-1]-pts[0]>=19 and all(0<b-a<=1 for a,b in zip(pts,pts[1:]))
    silence='silence_start:' in text
    freeze='freeze_start:' in text
    return {'startup_seconds':round(first_frame[0],3) if first_frame else None,
            'timestamp_origin_normalized':True,'timestamp_parser_version':2,
            'audio_decoded':bool(audio_pts),'decoded_video_seconds':round(video_pts[-1]-video_pts[0],3) if video_pts else 0,
            'decoded_audio_seconds':round(audio_pts[-1]-audio_pts[0],3) if audio_pts else 0,
            'video_continuous':continuous(video_pts),'audio_continuous':continuous(audio_pts),
            'video_frames':len(video_pts),'audio_frames':len(audio_pts),
            'max_video_gap':max((b-a for a,b in zip(video_pts,video_pts[1:])),default=0),
            'max_audio_gap':max((b-a for a,b in zip(audio_pts,audio_pts[1:])),default=0),
            'long_silence':silence,'long_freeze':freeze}


def check(channel,variant,ffmpeg,ffprobe):
    h={'at':common.now(),'region':'CZ','result':'unknown','measurement_version':2}
    started=time.monotonic()
    try:
        url,headers=transport.connection(variant['block'])
        body,resolved,_=public_fetch(url,headers,limit=262144,deadline=started+90)
        if body.lstrip(b'\xef\xbb\xbf').startswith(b'#EXTM3U'):
            sample=transport.hls_sample(body,resolved,headers,started+90,fetcher=public_fetch)
            sample.pop('playback_url',None)
            h.update(sample)
        video,audio,russian,media=inspect_stream(url,headers,ffprobe)
        if not russian:
            for review in variant.get('audio_reviews',[]):
                if (review.get('url')==variant['block'][-1] and review.get('language')=='ru'
                        and review.get('method')=='listening' and review.get('evidence') and review.get('observed_at')
                        and 0<=time.time()-datetime.fromisoformat(review['observed_at']).timestamp()<=main.MAX_AGE):
                    russian=True;h['audio_language_evidence']='Dated manual listening of exact URL'
                    media['russian_default']=media['selected_audio_index']==media['default_audio_index']
        h.update(media=media,russian_audio_confirmed=russian)
        if russian:h.setdefault('audio_language_evidence','FFprobe audio stream language tag')
        h.update(decode(url,headers,video,audio,ffmpeg))
        if not h['video_continuous'] or not h['audio_continuous']:
            h.update(result='quality_fail',reason='Decoded sample too short or timestamp discontinuity; inspect measured spans and gaps')
        elif h['long_silence'] or h['long_freeze']:
            h.update(result='quality_fail',reason='Long silence/freeze in sample; repeated confirmation required')
        elif channel['movie_candidate'] and not russian:
            h.update(reason='Russian audio language tag absent: needs listening/manual evidence; decoded sample is not language proof')
        else:h['result']='ok'
    except transport.GeoBlocked as error:h.update(result='geo',reason=str(error))
    except transport.Inconclusive as error:h.update(reason=str(error))
    except transport.StreamFailure as error:
        h.update(result='fail',reason=str(error),http_status=error.http_status)
    except (TimeoutError,subprocess.TimeoutExpired):h.update(result='fail',reason='Media timeout')
    except Exception as error:h.update(result='fail',reason=type(error).__name__+' during media check')
    h['elapsed_seconds']=round(time.monotonic()-started,3)
    return h


def shared_ru_evidence(db,checked):
    """Reuse exact-stream CZ observations; the RU/UA classifier keeps its own spacing."""
    if not common.DB.exists():return None
    ru=common.load()
    for row in checked:
        variant=db['channels'][row['channel']]['variants'][row['variant']]
        key=common.key(variant['block'])
        if key not in ru['channels']:continue
        h=variant['history'][-1]
        if h.get('region')!='CZ' or not h.get('egress_sha256'):continue
        media=h.get('media',{})
        evidence={k:h[k] for k in ('at','region','egress_sha256','elapsed_seconds') if k in h}
        evidence.update(result=h['result'] if h['result'] in ('ok','fail','geo') else 'unknown',
                        shared_main_check=True,
                        video={'width':media.get('width',0),'height':media.get('height',0),'hdr_types':[]})
        channel=ru['channels'][key]
        if not any(old['at']==evidence['at'] and old.get('egress_sha256')==evidence['egress_sha256'] for old in channel['history']):
            channel['history'].append(evidence)
        if not ru.get('cycle'):channel['status']=transport.classify(channel)
    return ru


def run(args):
    # Keep the old CLI usable, but route it through hard worker deadlines,
    # per-result checkpoints and a lock shared by all checkouts.
    if not args.confirm_direct_cz:
        raise RuntimeError('--confirm-direct-cz required')
    from audit_personal_cz import run as durable_run
    from pathlib import Path
    args.passes=3
    args.official_only=False
    args.progress_dir=os.environ.get('MYIPTV_PROGRESS_DIR',str(Path(os.environ.get('LOCALAPPDATA',common.ROOT/'.tools'))/'MyIPTV-CZ-runner'/'progress'))
    durable_run(args)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--confirm-direct-cz',action='store_true')
    p.add_argument('--ffmpeg',default='ffmpeg');p.add_argument('--ffprobe',default='ffprobe')
    p.add_argument('--max-streams',type=int,default=40);p.add_argument('--workers',type=int,choices=range(1,3),default=2)
    p.add_argument('--movies-only',action='store_true')
    p.add_argument('--channel')
    p.add_argument('--budget-seconds',type=int,default=1500)
    a=p.parse_args()
    if a.max_streams<1 or a.budget_seconds<201:p.error('Invalid limits')
    try:
        run(a)
    except Exception as error:
        common.record_failure('main-cz-checks',error)
        raise
