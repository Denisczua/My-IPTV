"""Durable, resumable media audit; diagnostic admission is separate from publishing."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from urllib.parse import urlsplit

import filter_ru_ua as common
import main_catalogue as main
from check_main_movies import direct_cz
from probe_runtime import awake,isolated_check,progress_lock,ProbeBusy
from check_ru_ua import connection


def transport_key(variant):
    try:
        url,headers=connection(variant['block'])
        return (url,json.dumps(headers,sort_keys=True))
    except Exception:return (main.stream_key(variant['block']), '')


def candidates(db,policy):
    allowed={i for g in policy['groups'].values() for i in g['ids']}
    rows=[]
    for ident,ch in db['channels'].items():
        if ch.get('adult_specialization'):continue
        if ident not in allowed and not (ch.get('movie_candidate') and ('rus' in ch.get('catalogue_languages',[]) or any(v.get('rights_evidence') for v in ch['variants'].values()))):continue
        preferred=db.get('selected',{}).get(ident) or db.get('current',{}).get(ident)
        for key,v in ch['variants'].items():
            rows.append((-1 if v.get('rights_evidence') else 0 if key==preferred else 1,0 if ident in allowed else 1,ident,key))
    unique={}
    for row in sorted(rows):
        ident,key=row[-2:]
        unique.setdefault((ident,transport_key(db['channels'][ident]['variants'][key])),row)
    return list(unique.values())


def discoveries(db):
    path=common.DATA/'personal-discoveries.json'
    if not path.exists():return
    for ident,records in json.loads(path.read_text(encoding='utf-8')).items():
        if ident not in db['channels']:continue
        ch=db['channels'][ident]
        for r in records:
            if not r.get('evidence') or not r.get('reviewed_at'):continue
            logo=next((common.attrs(v['block'][0]).get('tvg-logo') for v in ch['variants'].values() if common.attrs(v['block'][0]).get('tvg-logo')),'')
            block=[f'#EXTINF:-1 tvg-id="{ident}" tvg-logo="{logo}" tvg-language="{r["language"]}",{ch["name"]}',r['url']]
            v=ch['variants'].setdefault(main.stream_key(block),{'block':block,'history':[],'existing':False,'status':'UNKNOWN_CZ','sources':[]})
            v['rights_evidence']=r['evidence'];v['audio_reviews']=v.get('audio_reviews',[])
            source={'provider':'official broadcaster','url':r['evidence'],'header':'#EXTM3U'}
            if source not in v['sources']:v['sources'].append(source)


def replay(db,journal):
    accepted=0
    if not journal.exists():return accepted
    for line in journal.read_text(encoding='utf-8').splitlines():
        try:r=json.loads(line)
        except json.JSONDecodeError:continue # a power loss may truncate the last append
        variants=db['channels'].get(r['channel'],{}).get('variants',{})
        v=variants.get(r['variant'])
        h=r['observation']
        if not v or main.stream_key(v['block'])!=r['variant'] or not h.get('egress_sha256'):continue
        for target in variants.values():
            if transport_key(target)!=transport_key(v):continue
            if not any(old.get('observation_id')==h['observation_id'] for old in target['history']):
                target['history'].append(h);target['history'].sort(key=lambda x:x['at']);accepted+=1
            target['status']=main.status(target)
            if target['status']=='STABLE_CZ':target['was_stable']=True
    return accepted


def repair_journal_tail(path):
    if not path.exists():return
    lines=path.read_text(encoding='utf-8').splitlines();valid=[]
    for index,line in enumerate(lines):
        try:json.loads(line)
        except json.JSONDecodeError:
            if index!=len(lines)-1:raise ValueError('Corrupt journal before last record; retaining all files')
            path.with_suffix('.interrupted-tail').write_text(line,encoding='utf-8')
            break
        valid.append(line)
    # Ensure an interrupted final append cannot swallow the next observation.
    common.atomic(path,'\n'.join(valid)+'\n' if valid else '')


def spaced_checks(variant,identity):
    recent=[h for h in variant['history'] if h.get('measurement_version')==3 and h.get('egress_sha256')==identity
            and 0<=time.time()-datetime.fromisoformat(h['at']).timestamp()<=main.MAX_AGE]
    spaced=[]
    for h in sorted(recent,key=lambda h:h['at']):
        if not spaced or datetime.fromisoformat(h['at']).timestamp()-datetime.fromisoformat(spaced[-1]['at']).timestamp()>=main.MIN_INTERVAL:
            spaced.append(h)
    return spaced


def run(a):
    ffmpeg=shutil.which(a.ffmpeg);ffprobe=shutil.which(a.ffprobe)
    if not ffmpeg or not ffprobe:raise RuntimeError('FFmpeg and FFprobe required')
    policy=json.loads((common.DATA/'personal-policy.json').read_text(encoding='utf-8'))
    root=Path(a.progress_dir);root.mkdir(parents=True,exist_ok=True)
    journal=root/'observations.jsonl'
    with progress_lock(root),awake(),common.lock():
        identity=direct_cz();geo_at=time.monotonic();db=main.load();repair_journal_tail(journal);discoveries(db);replay(db,journal)
        rows=candidates(db,policy)
        if getattr(a,'channel',None):rows=[r for r in rows if r[-2]==a.channel]
        if getattr(a,'movies_only',False):rows=[r for r in rows if db['channels'][r[-2]].get('movie_candidate')]
        if a.official_only:rows=[r for r in rows if db['channels'][r[-2]]['variants'][r[-1]].get('rights_evidence')]
        started=time.monotonic();done=0
        with ThreadPoolExecutor(max_workers=a.workers) as pool:
            while done<a.max_streams and time.monotonic()-started<a.budget_seconds-165:
                if (root/'stop-request').exists():
                    (root/'stop-request').unlink();break
                due=[]
                for priority,kind,ident,key in rows:
                    v=db['channels'][ident]['variants'][key]
                    checks=spaced_checks(v,identity)
                    age=time.time()-datetime.fromisoformat(checks[-1]['at']).timestamp() if checks else float('inf')
                    if len(checks)>=a.passes and age<21600:continue
                    if age<main.MIN_INTERVAL:continue
                    due.append((len(checks),priority,kind,ident,key))
                if not due:
                    pending=any(len(spaced_checks(db['channels'][i[-2]]['variants'][i[-1]],identity))<a.passes for i in rows)
                    if pending:
                        time.sleep(10);continue
                    break
                # Complete proof for official free candidates first; speculative
                # reserves must not postpone their second/third spaced samples.
                due.sort(key=lambda r:(0 if r[1]<0 else 1,*r));batch=[];hosts=set()
                for item in due:
                    ident,key=item[-2:];v=db['channels'][ident]['variants'][key]
                    host=urlsplit(v['block'][-1].split('|')[0]).hostname
                    if host in hosts:continue
                    hosts.add(host);batch.append(item)
                    if len(batch)>=min(a.workers,a.max_streams-done):break
                if time.monotonic()-geo_at>=60:
                    if direct_cz()!=identity:raise RuntimeError('CZ egress changed; previous accepted checkpoints retained')
                    geo_at=time.monotonic()
                futures={pool.submit(isolated_check,db['channels'][i[-2]],db['channels'][i[-2]]['variants'][i[-1]],ffmpeg,ffprobe):i for i in batch}
                for f in as_completed(futures):
                    item=futures[f];ident,key=item[-2:];h=f.result()
                    if time.monotonic()-geo_at>=60:
                        if direct_cz()!=identity:raise RuntimeError('CZ egress changed; incomplete batch not accepted')
                        geo_at=time.monotonic()
                    h['egress_sha256']=identity
                    h['observation_id']=hashlib.sha256((ident+key+h['at']+identity).encode()).hexdigest()
                    record={'channel':ident,'variant':key,'observation':h}
                    with journal.open('a',encoding='utf-8') as out:
                        out.write(json.dumps(record,ensure_ascii=True)+'\n');out.flush();os.fsync(out.fileno())
                    variants=db['channels'][ident]['variants'];v=variants[key]
                    for target in variants.values():
                        if transport_key(target)!=transport_key(v):continue
                        target['history'].append(h);target['status']=main.status(target)
                        if target['status']=='STABLE_CZ':target['was_stable']=True
                    done+=1
                    report={'at':common.now(),'checks_this_run':done,'candidate_streams':len(rows),
                            'unique_candidates':len({i[-2] for i in rows}),
                            'streams_with_v3_check':sum(any(h.get('measurement_version')==3 for h in db['channels'][i[-2]]['variants'][i[-1]]['history']) for i in rows),
                            'results':dict(Counter(json.loads(line)['observation']['result'] for line in journal.read_text(encoding='utf-8').splitlines() if line.strip())),
                            'last_channel':ident,'last_result':h['result'],'elapsed_seconds':round(time.monotonic()-started)}
                    common.save_json(root/'progress.json',report)
                    print(json.dumps({'checked':done,'channel':ident,'result':h['result'],'reason':h.get('reason','')},ensure_ascii=True),flush=True)
                # Merge durable journal every batch; termination can lose at most a DB
                # rewrite, never observations or resume position.
                common.save_json(main.STATE,db)
                common.save_json(common.DATA/'personal-probe-report.json',report)
                time.sleep(1)
        common.save_json(main.STATE,db)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ffmpeg',default='ffmpeg');p.add_argument('--ffprobe',default='ffprobe')
    p.add_argument('--workers',type=int,choices=range(1,7),default=4)
    p.add_argument('--max-streams',type=int,default=2000);p.add_argument('--passes',type=int,choices=range(1,10),default=3)
    p.add_argument('--budget-seconds',type=int,default=1500)
    p.add_argument('--official-only',action='store_true',help='Finish known official free candidates first')
    p.add_argument('--channel',help='Check one canonical channel and its archived variants')
    p.add_argument('--movies-only',action='store_true')
    p.add_argument('--skip-if-busy',action='store_true',help='Scheduled job reuses accepted checkpoints if a manual audit is active')
    p.add_argument('--progress-dir',default=os.environ.get('MYIPTV_PROGRESS_DIR',str(Path(os.environ.get('LOCALAPPDATA',common.ROOT/'.tools'))/'MyIPTV-CZ-runner'/'progress')))
    a=p.parse_args()
    if a.budget_seconds<201 or a.max_streams<1:p.error('Invalid limits')
    try:run(a)
    except ProbeBusy:
        if not a.skip_if_busy:raise
        print(json.dumps({'probe_active_elsewhere':True,'new_checks_this_job':0,'action':'export previously accepted checkpoints only'}))
