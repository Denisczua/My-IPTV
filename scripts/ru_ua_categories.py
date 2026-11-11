"""Categories derived from measured video metadata and documented manual review."""
from datetime import datetime, timezone
import json
import re
from ru_ua_content import eligible

THEMES = {
    'Movies': 'Кино и сериалы', 'Series': 'Кино и сериалы', 'Sports': 'Спорт',
    'News': 'Новости', 'Music': 'Музыка', 'Documentary': 'Познавательные',
    'Science': 'Познавательные', 'Education': 'Познавательные', 'Kids': 'Детские',
    'Animation': 'Детские', 'General': 'Общие', 'Entertainment': 'Развлекательные',
    'Family': 'Семейные', 'Culture': 'Культура', 'Lifestyle': 'Образ жизни',
    'Travel': 'Путешествия', 'Outdoor': 'Природа и отдых', 'Religious': 'Религиозные',
    'Business': 'Бизнес', 'Shop': 'Магазины', 'Cooking': 'Кулинария',
    'Comedy': 'Юмор', 'Auto': 'Авто', 'Classic': 'Классика', 'Relax': 'Релакс',
    'Weather': 'Погода', 'Legislative': 'Парламентские', 'XXX': 'Для взрослых',
}
SPECIAL = {'OLED', '4K Ultra HD', 'HDR'}


def settings(data):
    return (json.loads((data/'category-overrides.json').read_text(encoding='utf-8')),
            json.loads((data/'oled-channels.json').read_text(encoding='utf-8')))


def attributes(line):
    return dict(re.findall(r'([\w-]+)="([^"]*)"', line))


def selection(channel, mapping):
    a = attributes(channel['block'][0])
    for selector in (channel['block'][-1], a.get('tvg-id', ''), a.get('tvg-id', '').split('@')[0]):
        if selector and selector in mapping:
            return mapping[selector]
    return None


def video(channel, clock=None):
    clock = datetime.now(timezone.utc).timestamp() if clock is None else clock
    for h in reversed(channel['history']):
        if h.get('region') == 'CZ' and h.get('result') == 'ok' and h.get('egress_sha256'):
            if clock-datetime.fromisoformat(h['at']).timestamp() > 7*86400:
                return {}
            return h.get('video', {})
    return {}


def flags(channel, oled, clock=None):
    measured = video(channel, clock)
    approved = selection(channel, oled)
    oled_ok = False
    if approved:
        if not isinstance(approved, dict) or not approved.get('evidence') or not approved.get('reviewed_at'):
            raise ValueError('OLED approval requires evidence and reviewed_at')
        datetime.fromisoformat(approved['reviewed_at'])
        oled_ok = approved.get('approved') is True
        if approved.get('expires_at'):
            current = datetime.now(timezone.utc).timestamp() if clock is None else clock
            oled_ok = oled_ok and datetime.fromisoformat(approved['expires_at']).timestamp() > current
    hdr = measured.get('hdr_types', [])
    return {'4K Ultra HD': measured.get('width', 0) >= 3840 and measured.get('height', 0) >= 2160,
            'HDR10': 'HDR10' in hdr, 'HDR10+': 'HDR10+' in hdr, 'HLG': 'HLG' in hdr,
            'HDR': bool(set(hdr) & {'HDR10', 'HDR10+', 'HLG'}),
            'OLED': oled_ok}


def groups(channel, overrides, oled):
    a = attributes(channel['block'][0])
    original = a.get('group-title', '')
    if not original:
        original = next((s.partition(':')[2] for s in channel['block'] if s.startswith('#EXTGRP:')), '')
    theme = [THEMES.get(s.strip(), s.strip()) for s in original.split(';') if s.strip()]
    override = selection(channel, overrides)
    if override is not None:
        if not override.get('evidence') or not isinstance(override.get('groups'), list):
            raise ValueError('Category override requires groups and evidence')
        theme = override['groups']
    # Special categories cannot be claimed by source names or a thematic override.
    theme = [s for s in theme if s not in SPECIAL]
    if not theme:
        theme = ['Другие']
    evidence = flags(channel, oled)
    if evidence['OLED']:
        theme.append('OLED')
    if evidence['4K Ultra HD']:
        theme.append('4K Ultra HD')
    if any(evidence[h] for h in ('HDR10', 'HDR10+', 'HLG')):
        theme.append('HDR')
    if any(not isinstance(s, str) or any(x in s for x in ('\n', '\r', '"')) for s in theme):
        raise ValueError('Invalid category name')
    return list(dict.fromkeys(theme))


def with_group(block, group):
    line = block[0]
    if re.search(r'group-title="[^"]*"', line):
        line = re.sub(r'group-title="[^"]*"', lambda _: f'group-title="{group}"', line, count=1)
    else:
        quoted = False
        for i, character in enumerate(line):
            if character == '"':
                quoted = not quoted
            if character == ',' and not quoted:
                line = line[:i] + f' group-title="{group}"' + line[i:]
                break
        else:
            raise ValueError('EXTINF channel name separator missing')
    return [line] + [f'#EXTGRP:{group}' if s.startswith('#EXTGRP:') else s for s in block[1:]]


def statistics(db, data):
    _, oled = settings(data)
    all_counts = dict.fromkeys(('4K Ultra HD', 'HDR', 'HDR10', 'HDR10+', 'HLG', 'OLED'), 0)
    stable_counts = all_counts.copy()
    for channel in db['channels'].values():
        if not eligible(channel):
            continue
        for category, present in flags(channel, oled).items():
            all_counts[category] += bool(present)
            if channel['status'] == 'STABLE_CZ':
                stable_counts[category] += bool(present)
    return {'categories_confirmed_candidates': all_counts, 'categories_stable_cz': stable_counts}
