import copy
from datetime import datetime, timezone
import json
import io
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import filter_ru_ua as f
import check_ru_ua as c
import ru_ua_categories as categories
import ru_ua_content as content


BLOCK = ['#EXTINF:-1 tvg-id="Example.us@HD" tvg-logo="logo" group-title="News",Example',
         '#EXTVLCOPT:http-referrer=https://example.org/',
         'https://example.org/live.m3u8|User-Agent=Player']


def channel(results, old='UNKNOWN_CZ'):
    return {'status': old, 'history': [
        {'at': datetime.fromtimestamp(i*21600, timezone.utc).isoformat(),
         'result': result, 'region': 'CZ', 'egress_sha256': 'test'}
        for i, result in enumerate(results)]}


class PlaylistTests(unittest.TestCase):
    def test_verbatim_roundtrip(self):
        content = '#EXTM3U\n' + '\n'.join(BLOCK) + '\n'
        self.assertEqual(f.parse(content), [BLOCK])
        url, headers = c.connection(BLOCK)
        self.assertEqual(headers['Referer'], 'https://example.org/')
        self.assertEqual(headers['User-Agent'], 'Player')
        self.assertEqual(url, 'https://example.org/live.m3u8')

    def test_reject_corruption(self):
        for content in ['#EXTM3U\n', '#EXTM3U\n#EXTINF:-1,X\n',
                        '#EXTM3U\nhttps://example.org\n',
                        '#EXTM3U\n#EXTINF:-1,A\n#EXTINF:-1,B\nhttps://example.org\n']:
            with self.subTest(content=content), self.assertRaises(ValueError):
                f.parse(content)

    def test_language_never_uses_country_or_cyrillic_name(self):
        block = ['#EXTINF:-1 tvg-id="Unknown.ru@HD",Русское название', 'https://x/live']
        self.assertEqual(f.languages(block, {}, {}, {})[0], [])
        self.assertEqual(f.languages(BLOCK, {'Example.us': {'rus'}}, {}, {})[0], ['rus'])

    def test_feed_language_overrides_channel_level_inference(self):
        self.assertEqual(f.languages(BLOCK, {'Example.us': {'rus'}}, {}, {},
                                     {'Example.us@HD': ['eng']})[0], [])
        langs, why = f.languages(BLOCK, {}, {}, {}, {'Example.us@HD': ['rus', 'eng']})
        self.assertEqual(langs, [])
        self.assertIn('review', why)
        self.assertEqual(f.languages(BLOCK, {}, {}, {}, {'Example.us@HD': ['rus', 'ukr']})[0], ['rus', 'ukr'])

    def test_same_name_different_stream_and_headers_have_distinct_keys(self):
        another = BLOCK[:-1] + ['https://example.org/other.m3u8']
        self.assertNotEqual(f.key(BLOCK), f.key(another))
        self.assertNotEqual(f.key(BLOCK), f.key([BLOCK[0], BLOCK[-1]]))

    def test_empty_main_never_replaces_working_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            source = root / 'playlist.m3u'
            content = '#EXTM3U\n' + '\n'.join(BLOCK) + '\n'
            source.write_text(content, encoding='utf-8')
            main = root / 'playlist-ru-ua.m3u'
            main.write_text(content, encoding='utf-8')
            db = {'channels': {'a': {'block': BLOCK, 'languages': ['rus'], 'status': 'UNKNOWN_CZ', 'history': []}}}
            with patch.object(f, 'ROOT', root), patch.object(f, 'SOURCE', source):
                report = f.publish(db)
            self.assertFalse(report['main_updated'])
            self.assertEqual(main.read_text(encoding='utf-8'), content)

    def test_transaction_rolls_back_on_write_error(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            first, second = Path(folder)/'one', Path(folder)/'two'
            first.write_text('original', encoding='utf-8')
            atomic = f.atomic
            def fail_second(path, content):
                if path == second:
                    raise OSError('disk failure')
                atomic(path, content)
            with patch.object(f, 'atomic', side_effect=fail_second), self.assertRaises(OSError):
                f.transaction({first: 'new', second: 'bad'})
            self.assertEqual(first.read_text(encoding='utf-8'), 'original')
            self.assertFalse(second.exists())


class StabilityTests(unittest.TestCase):
    def classify(self, results, old='UNKNOWN_CZ'):
        return c.classify(channel(results, old), clock=max(0, len(results)-1)*21600)

    def test_one_success_is_unknown(self):
        self.assertEqual(self.classify(['ok']), 'UNKNOWN_CZ')

    def test_three_spaced_successes_and_recovery(self):
        self.assertEqual(self.classify(['ok']*3), 'STABLE_CZ')
        self.assertEqual(self.classify(['fail']*3+['ok']*2, 'OFFLINE'), 'UNSTABLE_CZ')
        self.assertEqual(self.classify(['fail']*3+['ok']*3, 'OFFLINE'), 'STABLE_CZ')

    def test_single_failure_grace_and_repeated_failures(self):
        self.assertEqual(self.classify(['ok']*3+['fail'], 'STABLE_CZ'), 'STABLE_CZ')
        self.assertEqual(self.classify(['ok']*3+['fail']*2, 'STABLE_CZ'), 'UNSTABLE_CZ')
        self.assertEqual(self.classify(['ok']*3+['fail']*3, 'UNSTABLE_CZ'), 'OFFLINE')
        self.assertEqual(self.classify(['geo']*2), 'GEO_BLOCKED_CZ')
        self.assertEqual(self.classify(['ok']*3+['unknown']*2, 'STABLE_CZ'), 'STABLE_CZ')

    def test_burst_non_cz_and_stale_do_not_qualify(self):
        value = channel(['ok']*3)
        for h in value['history']:
            h['at'] = datetime.fromtimestamp(0, timezone.utc).isoformat()
        self.assertEqual(c.classify(value, 0), 'UNKNOWN_CZ')
        value = channel(['ok']*3)
        for h in value['history']:
            h['region'] = 'US'
        self.assertEqual(c.classify(value, 43200), 'UNKNOWN_CZ')
        self.assertEqual(c.classify(channel(['ok']*3), c.MAX_AGE+43201), 'UNKNOWN_CZ')

    def test_http_200_html_and_bare_403_are_not_geo_proof(self):
        self.assertFalse(c.geo_message(b'<h1>403 Forbidden</h1>'))
        self.assertTrue(c.geo_message(b'Not available in your country'))

    def test_http_200_requires_media_and_geo_reason(self):
        for body, expected in [(b'<html>ordinary error</html>', ValueError),
                               (b'<html>not available in your country</html>', c.GeoBlocked)]:
            response = MagicMock()
            response.headers = {'Content-Type': 'text/html'}
            response.read.side_effect = [body, b'']
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value = response
            with patch.object(c.urllib.request, 'build_opener', return_value=opener), self.assertRaises(expected):
                c.fetch('https://example.org/video')
        for code, body, expected in [(403, b'Forbidden', ValueError),
                                     (403, b'geo blocked', c.GeoBlocked), (451, b'Unavailable', c.GeoBlocked)]:
            opener = MagicMock()
            opener.open.side_effect = c.urllib.error.HTTPError('https://example.org', code, 'blocked', {}, io.BytesIO(body))
            with patch.object(c.urllib.request, 'build_opener', return_value=opener), self.assertRaises(expected):
                c.fetch('https://example.org/video')

    def test_http_success_without_decoded_video_does_not_qualify(self):
        for stdout, stderr, status in [('frame=0\nout_time_us=0\n', '', 'fail'),
                                       ('frame=300\nout_time_us=12000000\n', 'decode error', 'fail'),
                                       ('frame=300\nout_time_us=12000000\n', '', 'ok')]:
            run = SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)
            with patch.object(c, 'fetch', return_value=(b'x'*1000, BLOCK[-1], 1)), \
                 patch.object(c.subprocess, 'run', return_value=run):
                result = c.check({'block': BLOCK}, 'ffmpeg')
            self.assertEqual(result['result'], status)

    def test_hls_checks_nested_segments_and_key(self):
        manifest = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nlow/index.m3u8\n'
        media = (b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n'
                 b'#EXT-X-MAP:URI="init"\n#EXTINF:6,\na.ts\n#EXTINF:6,\nb.ts\n#EXTINF:6,\nc.ts\n')
        seen = []
        def fetch(url, headers, **kwargs):
            seen.append(url)
            data = media if url.endswith('.m3u8') else b'k'*16 if url.endswith('/key') else b'x'*1000
            return data, url, 1
        with patch.object(c, 'fetch', side_effect=fetch):
            result = c.hls_sample(manifest, 'https://x/master.m3u8', {}, 999999999)
        self.assertEqual(result['segments'], 3)
        self.assertEqual(len(seen), 6)
        self.assertIn('https://x/low/key', seen)

    def test_missing_cz_or_ffmpeg_leaves_state_untouched(self):
        args = type('Args', (), {'confirm_direct_cz': False})()
        with patch.object(f, 'load') as load, self.assertRaises(c.Inconclusive):
            c.run(args)
        load.assert_not_called()

    def test_partial_cycle_preserves_outputs_then_resumes(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            data = root/'data'
            data.mkdir()
            for name in ('category-overrides.json', 'oled-channels.json'):
                (data/name).write_text('{}', encoding='utf-8')
            source = root/'playlist.m3u'
            source.write_text('#EXTM3U\n'+'\n'.join(BLOCK)+'\n', encoding='utf-8')
            main = root/'playlist-ru-ua.m3u'
            main.write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
            original = main.read_bytes()
            db_path = data/'ru-ua-state.json'
            old = datetime.now(timezone.utc).timestamp()-43200
            history = [{'at': datetime.fromtimestamp(old+i*21600, timezone.utc).isoformat(),
                        'result': 'ok', 'region': 'CZ', 'egress_sha256': 'test'} for i in range(2)]
            db = {'version': 1, 'cycle': None, 'counts': {}, 'review_needed': {}, 'channels': {
                'one': {'block': BLOCK, 'history': copy.deepcopy(history), 'languages': ['rus'], 'status': 'UNKNOWN_CZ'},
                'two': {'block': BLOCK[:-1]+['https://other.example/live'], 'history': copy.deepcopy(history),
                        'languages': ['ukr'], 'status': 'UNKNOWN_CZ'}}}
            f.save_json(db_path, db)
            args = SimpleNamespace(confirm_direct_cz=True, ffmpeg='ffmpeg', ffprobe='ffprobe', refresh_metadata=False,
                                   workers=1, max_channels=1, budget_seconds=600, pause_seconds=0)
            observation = {'at': f.now(), 'result': 'ok', 'region': 'CZ'}
            with patch.object(f, 'ROOT', root), patch.object(f, 'SOURCE', source), patch.object(f, 'DATA', data), \
                 patch.object(f, 'DB', db_path), patch.object(f, 'build', side_effect=lambda value, refresh: value), \
                 patch.object(f, 'load', side_effect=lambda: json.loads(db_path.read_text(encoding='utf-8'))), \
                 patch.object(c, 'cz_identity', return_value='test'), patch.object(c.shutil, 'which', return_value='tool'), \
                 patch.object(c, 'check', side_effect=lambda *args: copy.deepcopy(observation)), patch('builtins.print'):
                c.run(args)
                partial = json.loads(db_path.read_text(encoding='utf-8'))
                self.assertEqual(len(partial['cycle']['remaining']), 1)
                self.assertEqual(main.read_bytes(), original)
                self.assertEqual(partial['channels']['one']['status'], 'UNKNOWN_CZ')
                c.run(args)
                final = json.loads(db_path.read_text(encoding='utf-8'))
                self.assertIsNone(final['cycle'])
                self.assertTrue(all(ch['status'] == 'STABLE_CZ' for ch in final['channels'].values()))
                self.assertEqual(len(f.parse(main.read_text(encoding='utf-8'))), 2)


class CategoryTests(unittest.TestCase):
    def sample(self, video):
        return {'block': BLOCK, 'languages': ['rus'], 'status': 'STABLE_CZ', 'history': [
            {'at': f.now(), 'region': 'CZ', 'egress_sha256': 'test', 'result': 'ok', 'video': video}]}

    def test_hdr_detection_distinguishes_sdr_pq_hdr10_plus_hlg(self):
        stream = {'width': 3840, 'height': 2160, 'pix_fmt': 'yuv420p10le',
                  'color_transfer': 'bt709', 'color_primaries': 'bt2020'}
        self.assertEqual(c.video_properties({'streams': [stream]})['hdr_types'], [])
        stream['color_transfer'] = 'smpte2084'
        self.assertEqual(c.video_properties({'streams': [stream]})['hdr_types'], [])
        stream['side_data_list'] = [{'side_data_type': 'Mastering display metadata'},
                                    {'side_data_type': 'Content light level metadata'}]
        self.assertEqual(c.video_properties({'streams': [stream]})['hdr_types'], ['HDR10'])
        info = {'streams': [stream], 'frames': [{'side_data_list': [
            {'side_data_type': 'HDR Dynamic Metadata SMPTE2094-40 (HDR10+)'}]}]}
        self.assertEqual(c.video_properties(info)['hdr_types'], ['HDR10+'])
        stream['color_transfer'] = 'arib-std-b67'
        self.assertEqual(c.video_properties({'streams': [stream]})['hdr_types'], ['HLG'])
        stream['pix_fmt'] = 'yuv420p'
        self.assertEqual(c.video_properties({'streams': [stream]})['hdr_types'], [])

    def test_name_and_logo_do_not_prove_oled_4k_or_hdr(self):
        value = self.sample({})
        value['block'] = ['#EXTINF:-1 tvg-id="Example.us@HD",4K HDR OLED', BLOCK[-1]]
        flags = categories.flags(value, {})
        self.assertFalse(any(flags.values()))
        self.assertEqual(categories.groups(value, {}, {}), ['Другие'])

    def test_multiple_categories_keep_stream_options_and_identity(self):
        value = self.sample({'width': 3840, 'height': 2160, 'hdr_types': ['HDR10+']})
        oled = {'Example.us': {'approved': True, 'evidence': 'Manual visual review: minimal static graphics',
                              'reviewed_at': f.now()}}
        groups = categories.groups(value, {}, oled)
        self.assertEqual(groups, ['Новости', 'OLED', '4K Ultra HD', 'HDR'])
        for group in groups:
            rendered = categories.with_group(BLOCK, group)
            self.assertEqual(f.key(rendered), f.key(BLOCK))
            self.assertEqual(rendered[1:], BLOCK[1:])
            self.assertEqual(f.attrs(rendered[0])['tvg-id'], 'Example.us@HD')

    def test_technical_categories_cannot_be_forged_by_override(self):
        value = self.sample({})
        override = {'Example.us': {'groups': ['Спорт', 'OLED', '4K Ultra HD', 'HDR'], 'evidence': 'genre review'}}
        self.assertEqual(categories.groups(value, override, {}), ['Спорт'])

    def test_oled_requires_dated_evidence_and_can_expire(self):
        value = self.sample({})
        with self.assertRaises(ValueError):
            categories.flags(value, {'Example.us': {'approved': True}})
        oled = {'Example.us': {'approved': True, 'reviewed_at': '2020-01-01T00:00:00+00:00',
                              'expires_at': '2020-02-01T00:00:00+00:00', 'evidence': 'review'}}
        self.assertFalse(categories.flags(value, oled)['OLED'])

    def test_old_properties_do_not_qualify(self):
        value = self.sample({'width': 3840, 'height': 2160, 'hdr_types': ['HLG']})
        value['history'][0]['at'] = '2020-01-01T00:00:00+00:00'
        self.assertFalse(categories.flags(value, {})['4K Ultra HD'])

    def test_only_specialized_adult_is_excluded(self):
        ordinary = ['#EXTINF:-1 tvg-id="Cinema.us@HD" group-title="Movies",Horror 18+', BLOCK[-1]]
        self.assertEqual(content.decision(ordinary, {}, {})[0], 'allow')
        self.assertEqual(content.decision(ordinary, {'Cinema.us': {'categories': ['movies']}}, {})[0], 'allow')
        self.assertEqual(content.decision(ordinary, {'Cinema.us': {'categories': ['xxx']}}, {})[0], 'exclude')
        self.assertEqual(content.decision(ordinary, {'Cinema.us': {'is_nsfw': True}}, {})[0], 'review')
        allow = {'Cinema.us': {'decision': 'allow', 'evidence': 'official ordinary cinema specialization'}}
        self.assertEqual(content.decision(ordinary, {'Cinema.us': {'categories': ['xxx']}}, allow)[0], 'allow')

    def test_exclusion_applies_to_all_groups(self):
        value = self.sample({'width': 3840, 'height': 2160, 'hdr_types': ['HLG']})
        value['content_decision'] = 'exclude'
        self.assertFalse(content.eligible(value))
        value['content_decision'] = 'review'
        self.assertFalse(content.eligible(value))


if __name__ == '__main__':
    unittest.main()
