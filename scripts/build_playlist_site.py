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
    manifest={'generated_at':datetime.now(timezone.utc).isoformat(),
              'source':'playlist.m3u','source_commit':commit,'sha256':digest,
              'bytes':len(raw),'channels':len(channels),'ottplayer_connection_confirmed':False}
    (destination/'playlist-info.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (destination/'.nojekyll').write_text('',encoding='utf-8')
    (destination/'index.html').write_text('''<!doctype html>
<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>My-IPTV — постоянный адрес плейлиста</title>
<style>body{font:17px/1.6 system-ui;max-width:760px;margin:60px auto;padding:0 24px}a{color:#145ac1}code{overflow-wrap:anywhere}small{color:#555}</style>
<h1>My-IPTV</h1><p>Зеркало существующего основного плейлиста. Состав не сокращается и не меняется при публикации.</p>
<p><a href="playlist.m3u" download>playlist.m3u</a> · <a href="playlist.m3u8" download>тот же файл с расширением .m3u8</a></p>
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
