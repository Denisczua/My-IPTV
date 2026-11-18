"""Publish only reviewed movie streams with recent direct-CZ playback checks."""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urljoin, urlsplit

from check_ru_ua import fetch
from filter_ru_ua import atomic, parse


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
REPORT = ROOT / 'playlist-movies-ru-cz-report.md'
PLAYLIST = ROOT / 'playlist-movies-ru-cz.m3u'
STATE = DATA / 'movies-cz-checks.json'
QUALITY = {(1280, 720): 'HD', (1920, 1080): 'Full HD'}
GROUP = {'ru': 'Фильмы — русский', 'uk': 'Фильмы — украинский'}
PROTOCOLS = 'http,https,tcp,tls,crypto'
DIRECT_ENV = {key: value for key, value in os.environ.items()
              if 'proxy' not in key.lower()}


def cz_identity():
    curl = shutil.which('curl.exe') or shutil.which('curl')
    if not curl:
        raise SystemExit('curl нужен для проверки страны прямого IPv4-подключения')
    observations = []
    for url, field in (('https://ipwho.is/', 'country_code'),
                       ('https://ipinfo.io/json', 'country')):
        response = subprocess.run(
            [curl, '-4', '--noproxy', '*', '-fsS', '--max-time', '10', url],
            check=True, capture_output=True, env=DIRECT_ENV)
        data = json.loads(response.stdout)
        if data.get(field) != 'CZ' or not data.get('ip'):
            raise SystemExit('Два сервиса должны подтвердить страну CZ')
        observations.append(data['ip'])
    if observations[0] != observations[1]:
        raise SystemExit('Сервисы геолокации видят разные внешние IP')
    return observations[0]


def direct_url(url):
    parts = urlsplit(url)
    return (parts.scheme in {'http', 'https'} and parts.hostname
            and parts.username is None and '?' not in url and '#' not in url)


def hls_options(url):
    body, effective, _ = fetch(url, limit=262144)
    if not direct_url(effective):
        raise ValueError('Перенаправление на URL с параметрами')
    lines = body.decode('utf-8-sig').splitlines()
    for info, target in zip(lines, lines[1:]):
        if (info.startswith('#EXT-X-STREAM-INF:') and not target.startswith('#')
                and re.search(r'\bRESOLUTION=(1920x1080|1280x720)(?:,|$)', info)):
            option = urljoin(effective, target.strip())
            if direct_url(option):
                yield option
    yield url


def probe(url):
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-protocol_whitelist', PROTOCOLS,
         '-rw_timeout', '10000000', '-show_entries',
         'stream=index,width,height,codec_type:stream_disposition=default',
         '-read_intervals', '%+3', '-of', 'json', url],
        capture_output=True, text=True, errors='replace', timeout=25,
        env=DIRECT_ENV, check=True)
    streams = json.loads(result.stdout)['streams']
    video = {(s.get('width'), s.get('height')) for s in streams
             if s['codec_type'] == 'video'}
    audio = [s for s in streams if s['codec_type'] == 'audio']
    # A master with mixed video dimensions cannot guarantee HD on an adaptive player.
    if len(video) == 1 and video <= QUALITY.keys() and audio:
        track = next((s for s in audio if s.get('disposition', {}).get('default')), audio[0])
        return video.pop(), track['index']
    return None


def playback(url, size, audio_index):
    result = subprocess.run(
        ['ffmpeg', '-nostdin', '-hide_banner', '-nostats', '-v', 'info', '-xerror',
         '-err_detect', 'explode', '-protocol_whitelist', PROTOCOLS,
         '-rw_timeout', '10000000', '-re', '-i', url,
         '-t', '61', '-map', '0:v:0', '-map', f'0:{audio_index}',
         '-vf', f'showinfo,blackdetect=d=2,freezedetect=n=-60dB:d=2',
         '-af', 'ashowinfo,silencedetect=n=-55dB:d=5', '-f', 'null', '-'],
        capture_output=True, text=True, errors='replace', timeout=105,
        env=DIRECT_ENV)
    log = result.stderr
    if result.returncode:
        return 'FFmpeg прервал воспроизведение'
    for marker, reason in (
        ('black_start:', 'FFmpeg обнаружил чёрный экран от 2 с'),
        ('freeze_start:', 'FFmpeg обнаружил остановку изображения от 2 с'),
        ('silence_start:', 'FFmpeg обнаружил тишину от 5 с'),
    ):
        if marker in log:
            return reason
    video = re.findall(r'\bpts_time:([\d.]+).*?\bs:(\d+)x(\d+)', log)
    audio = re.findall(r'\bpts_time:([\d.]+).*?\bnb_samples:', log)
    if (video and audio and {(int(w), int(h)) for _, w, h in video} == {size}
            and continuous([float(pts) for pts, _, _ in video])
            and continuous([float(pts) for pts in audio])):
        return ''
    return 'FFmpeg не подтвердил 60 с непрерывного видео и звука заданного разрешения'


def continuous(timestamps):
    return (timestamps[-1] - timestamps[0] >= 60
            and all(0 < b - a <= 0.5 for a, b in zip(timestamps, timestamps[1:])))


def write(path, content):
    for attempt in range(4):
        try:
            atomic(path, content)
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.5)


def check(url, approved_url, expected_size):
    try:
        if approved_url not in set(hls_options(url)):
            return False, 'Утверждённый вариант исчез из источника'
        body, effective, _ = fetch(approved_url, limit=262144)
        if not body.startswith((b'#EXTM3U', b'\xef\xbb\xbf#EXTM3U')) or not direct_url(effective):
            return False, 'Прямой HLS-манифест недоступен'
        media = probe(approved_url)
        if media is None or media[0] != expected_size:
            return False, 'FFprobe не подтвердил заданные размеры кадра и аудиодорожку'
        reason = playback(approved_url, expected_size, media[1])
        if reason:
            return False, reason
    except Exception as error:
        return False, f'Проверка потока завершилась ошибкой ({type(error).__name__})'
    return True, ''


def source_entries(source_ref=None):
    entries = defaultdict(set)
    if source_ref:
        source = subprocess.check_output(
            ['git', '-C', str(ROOT), 'show', f'{source_ref}:playlist.m3u'])
    else:
        source = (ROOT / 'playlist.m3u').read_bytes()
    blocks = parse(source.decode('utf-8-sig'))
    for block in blocks:
        match = re.search(r'\btvg-id="([^"@]+)(?:@[^"]+)?"', block[0])
        if match:
            entries[match[1]].add(block[-1])
    return len(blocks), entries


def update(run_check, source_ref=None):
    now = datetime.now(timezone.utc)
    ip = None
    if run_check:
        if not all(shutil.which(tool) for tool in ('ffmpeg', 'ffprobe')):
            raise SystemExit('FFmpeg и FFprobe должны быть в PATH')
        ip = cz_identity()
    count, entries = source_entries(source_ref)
    config = json.loads((DATA / 'movies-sources.json').read_text(encoding='utf-8'))
    previous = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    history, selected, rows = {}, {}, []
    for item in config['channels']:
        name, lang, original = (item[k] for k in ('name', 'language', 'url'))
        source = item['source']
        size = tuple(item.get('size', []))
        url = item.get('playback_url', original)
        status, reason, date = 'UNKNOWN_CZ', item.get('pending', ''), config['initial_review']
        if item.get('excluded'):
            status, reason = 'EXCLUDED', item['excluded']
        elif item.get('id') and original not in entries[item['id']]:
            status, reason = 'EXCLUDED', 'Точный URL исчез из исходного playlist.m3u'
        elif item.get('review') and lang in GROUP and size in QUALITY and all(map(direct_url, (original, url))):
            key = json.dumps([original, url, lang, size, source, item['review']], ensure_ascii=False)
            samples = [s for s in previous.get(key, [])
                       if now - datetime.fromisoformat(s['at']) <= timedelta(days=3)]
            started = datetime.now(timezone.utc)
            if ip and (not samples or started - datetime.fromisoformat(samples[-1]['at']) >= timedelta(minutes=10)):
                ok, detail = check(original, url, size)
                if cz_identity() != ip:
                    raise SystemExit('Внешнее подключение изменилось во время проверки')
                samples.append({'at': started.isoformat(), 'ok': ok, 'detail': detail})
            history[key] = samples[-2:]
            if samples:
                date = 'Чехия (CZ), ' + ', '.join(s['at'][:16].replace('T', ' ') + ' UTC' for s in samples[-2:])
                status = 'PENDING_CZ' if samples[-1]['ok'] else 'FAILED_CZ'
                reason = samples[-1]['detail'] or 'Нужен второй успешный запуск'
            if (len(samples) >= 2 and all(s['ok'] for s in samples[-2:])
                    and datetime.fromisoformat(samples[-1]['at'])
                    - datetime.fromisoformat(samples[-2]['at']) >= timedelta(minutes=10)):
                status, reason = 'PASSED_CZ', '—'
                if name not in selected or size > selected[name][0]:
                    selected[name] = (size, lang, url)
        label = {'ru': 'русский', 'uk': 'украинский'}.get(lang, lang)
        measured = tuple(item.get('measured_size', size))
        resolution = '×'.join(map(str, measured)) if measured else 'Не измерено'
        if item.get('probe_date') and status in {'EXCLUDED', 'UNKNOWN_CZ'}:
            date = item['probe_date']
        rows.append((name, label, resolution, source, date, status, reason, url))

    rows = [(*row[:5], 'DUPLICATE', 'Выбран другой прошедший вариант канала')
            if row[5] == 'PASSED_CZ' and selected[row[0]][2] != row[-1] else row[:-1]
            for row in rows]
    rows.extend(config.get('additional_review', []))
    waiting = len({row[0] for row in rows if row[5] in {'UNKNOWN_CZ', 'PENDING_CZ'}} - selected.keys())
    report = [
        '# Фильмовые телеканалы для OTTPlayer в Чехии', '',
        f'В исходном `playlist.m3u` прочитано {count} записей. Ниже перечислены рассмотренные каналы и варианты.',
        f'Прошли проверку из Чехии — **{len(selected)}**. Ожидают проверки — **{waiting}**.', '',
        'Разрешение из подписей каталога не использовалось как результат измерения.',
        'Совместимость с OTTPlayer на Samsung TV требует импорта и проверки на телевизоре.',
        'На чешском runner используются FFprobe и FFmpeg. Проверки включают декодирование обоих потоков',
        'в реальном времени и поиск чёрного экрана, остановок изображения и длительной тишины.', '',
        '| Канал | Язык | Разрешение | Источник | Дата и регион проверки | Статус | Причина исключения |',
        '|---|---|---|---|---|---|---|',
    ]
    report.extend('| ' + ' | '.join(str(value).replace('|', '\\|') for value in row) + ' |'
                  for row in rows)
    if config.get('notes'):
        report.extend(['', *config['notes']])
    if run_check:
        write(STATE, json.dumps(history, ensure_ascii=False, indent=2) + '\n')
    write(REPORT, '\n'.join(report) + '\n')
    if selected:
        lines = ['#EXTM3U']
        for name, (size, lang, url) in sorted(selected.items(), key=lambda entry: (entry[1][1], entry[0])):
            lines.extend([f'#EXTINF:-1 group-title="{GROUP[lang]}",{name} {QUALITY[size]}', url])
        write(PLAYLIST, '\n'.join(lines) + '\n')
    else:
        PLAYLIST.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-direct-cz', action='store_true',
                        help='Run media checks from a direct connection in Czechia')
    parser.add_argument('--source-ref', help='Read playlist.m3u from this Git ref')
    args = parser.parse_args()
    update(args.confirm_direct_cz, args.source_ref)
