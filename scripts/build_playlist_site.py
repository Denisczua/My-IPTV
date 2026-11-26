"""Publish byte-identical mirrors of the existing main playlist, never a new selection."""
import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import subprocess

from filter_ru_ua import ROOT, parse


def build(source, destination, commit=''):
    raw=source.read_bytes()
    text=raw.decode('utf-8-sig')
    channels=parse(text)
    if b'\x00' in raw:
        raise ValueError('NUL bytes in M3U')
    if not channels:
        raise ValueError('Empty playlists cannot replace a deployed site')
    digest=hashlib.sha256(raw).hexdigest()
    destination.mkdir(parents=True,exist_ok=True)
    # Both extensions refer to exactly the same bytes/channels/HTTP directives.
    for name in ('playlist.m3u','playlist.m3u8'):
        (destination/name).write_bytes(raw)
    # Controlled diagnostics: preserve the body exactly; change only the global
    # header, or take complete channel blocks. Never rewrite the source file.
    lines=raw.splitlines(keepends=True)
    simple=b'#EXTM3U\n'+b''.join(lines[1:])
    small=('#EXTM3U\n'+'\n'.join('\n'.join(b) for b in channels[:100])+'\n').encode('utf-8')
    variants={'playlist-simple.m3u':simple,'diagnostic-100.m3u':small,
              'diagnostic-100-epg.m3u':lines[0].rstrip(b'\r\n')+b'\n'+small.split(b'\n',1)[1],
              'playlist-simple-crlf.m3u':simple.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')}
    for count in (1000,2000,5000):
        variants[f'diagnostic-{count}.m3u']=('#EXTM3U\n'+'\n'.join('\n'.join(b) for b in channels[:count])+'\n').encode('utf-8')
    optimized_report=None
    catalogue_path=source.parent/'data/main-catalogue.json'
    if catalogue_path.exists():
        from personal_playlist import export
        catalogue=json.loads(catalogue_path.read_text(encoding='utf-8'))
        def data(name):return json.loads((source.parent/'data'/name).read_text(encoding='utf-8'))
        optimized,optimized_report=export(catalogue,data('personal-policy.json'),data('personal-names.json')['names'],data('personal-discoveries.json'),data('oled-channels.json'))
        variants['playlist-ottplayer.m3u']=optimized
        (destination/'personal-report.json').write_text(json.dumps(optimized_report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    diagnostics={}
    for name,body in variants.items():
        count=len(parse(body.decode('utf-8-sig')))
        (destination/name).write_bytes(body)
        diagnostics[name]={'channels':count,'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()}
    manifest={'generated_at':datetime.now(timezone.utc).isoformat(),
              'source':'playlist.m3u','source_commit':commit,'sha256':digest,
              'bytes':len(raw),'channels':len(channels),'ottplayer_connection_confirmed':bool(optimized_report),
              'diagnostics':diagnostics,'optimized':optimized_report}
    (destination/'playlist-info.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (destination/'.nojekyll').write_text('',encoding='utf-8')
    (destination/'index.html').write_text('''<!doctype html>
<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>My-IPTV — постоянный адрес плейлиста</title>
<style>body{font:17px/1.6 system-ui;max-width:760px;margin:60px auto;padding:0 24px}a{color:#145ac1}code{overflow-wrap:anywhere}small{color:#555}</style>
<h1>My-IPTV</h1><p>Зеркало существующего основного плейлиста. Состав не сокращается и не меняется при публикации.</p>
<p><a href="playlist.m3u" download>playlist.m3u</a> · <a href="playlist.m3u8" download>тот же файл с расширением .m3u8</a></p>
<p><a href="playlist-ottplayer.m3u">Персональный OTTPlayer</a> — кино, рыбалка и охота, путешествия и природа, новости Украины и России. Только потоки с повторными проверками декодирования из Чехии и подтверждённым происхождением.</p>
<p>Проверка объёма: <a href="diagnostic-1000.m3u">1000 каналов</a> · <a href="diagnostic-2000.m3u">2000 каналов</a> · <a href="diagnostic-5000.m3u">5000 каналов</a>.</p>
<p>Диагностика OTTPlayer: <a href="playlist-simple.m3u">все каналы без глобального EPG</a> · <a href="diagnostic-100.m3u">100 каналов</a> · <a href="diagnostic-100-epg.m3u">100 каналов с исходным EPG</a> · <a href="playlist-simple-crlf.m3u">все каналы без EPG, CRLF</a>. Эти варианты автоматически строятся из основного файла.</p>
<p>Для OTTPlayer используйте прямой адрес файла в поле ссылки автообновления. Включите автообновление в настройках уже загруженного плейлиста.</p>
<p>''' + str(len(channels))+''' записей · '''+str(len(raw))+''' байт</p>
<small>SHA-256: <code>'''+digest+'''</code><br>Снимок репозитория: <code>'''+html.escape(commit)+'''</code></small>
<p><a href="playlist-info.json">Данные публикации</a></p></html>
''',encoding='utf-8')
    return manifest


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=ROOT/'_site')
    a=p.parse_args()
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    print(json.dumps(build(ROOT/'playlist.m3u',a.output,commit),ensure_ascii=True,indent=2))
