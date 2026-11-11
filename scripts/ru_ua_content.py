"""Exclude channel specialization, never the age rating of individual programmes."""
import json
import urllib.request
import re

URL = 'https://iptv-org.github.io/api/channels.json'


def metadata(data, save, now, refresh=False):
    path = data/'iptv-org-content.json'
    if refresh:
        with urllib.request.urlopen(URL, timeout=30) as response:
            channels = json.load(response)
        if not isinstance(channels, list) or len(channels) < 1000:
            raise ValueError('Incomplete channel specialization metadata')
        save(path, project(channels, now()))
    return json.loads(path.read_text(encoding='utf-8'))['flagged_channels']


def project(channels, retrieved_at):
    return {'source': URL, 'retrieved_at': retrieved_at, 'total_channels': len(channels),
            'flagged_channels': {c['id']: {'categories': c.get('categories', []),
                                         'is_nsfw': c.get('is_nsfw', False)}
                                 for c in channels if 'xxx' in c.get('categories', []) or c.get('is_nsfw')}}


def decision(block, flagged, overrides):
    attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', block[0]))
    ident = attrs.get('tvg-id', '').split('@')[0]
    override = overrides.get(block[-1], overrides.get(ident))
    if override is not None:
        if not override.get('evidence') or override.get('decision') not in ('allow', 'exclude', 'review'):
            raise ValueError('Specialization override needs decision and evidence')
        return override['decision'], override['evidence']
    info = flagged.get(ident, {})
    if 'xxx' in info.get('categories', []):
        return 'exclude', 'IPTV-org channel category xxx: specialized adult channel'
    groups = {s.strip().lower() for s in attrs.get('group-title', '').split(';')}
    if 'xxx' in groups:
        return 'exclude', 'Source playlist category XXX: specialized adult channel'
    if info.get('is_nsfw'):
        return 'review', 'is_nsfw without xxx: specialization requires manual review'
    # No title/18+/horror/thriller/genre heuristics.
    return 'allow', 'No evidence of specialized erotic or pornographic channel'


def eligible(channel):
    return bool(channel.get('languages')) and channel.get('content_decision', 'allow') == 'allow'
