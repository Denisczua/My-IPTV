import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import update_movies as movies


class PublicationTests(unittest.TestCase):
    def test_requires_two_separated_checks_and_falls_back_to_hd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            versions = [
                {'name': 'Кино', 'language': 'ru', 'size': list(size),
                 'source': 'Broadcaster', 'review': 'Legal free source and Russian audio',
                 'url': f'https://example.com/{size[1]}.m3u8'}
                for size in ((1280, 720), (1920, 1080))
            ]
            (root / 'playlist.m3u').write_text(
                '#EXTM3U\n#EXTINF:-1,Other\nhttps://example.com/other.m3u8\n',
                encoding='utf-8')
            (root / 'movies-sources.json').write_text(
                json.dumps({'initial_review': 'Test', 'channels': versions}), encoding='utf-8')
            report, playlist, state = (root / name for name in ('report.md', 'movies.m3u', 'state.json'))
            now = datetime.now(timezone.utc)

            def save_checks(hd_minutes, full_minutes, full_ok=True):
                history = {}
                for item, minutes, ok in zip(versions, (hd_minutes, full_minutes), (True, full_ok)):
                    key = json.dumps([item['url'], item['url'], 'ru', item['size'],
                                      item['source'], item['review']], ensure_ascii=False)
                    history[key] = [
                        {'at': (now - timedelta(minutes=age)).isoformat(),
                         'ok': ok, 'detail': '' if ok else 'Playback failed'}
                        for age in minutes
                    ]
                state.write_text(json.dumps(history), encoding='utf-8')

            with patch.multiple(movies, ROOT=root, DATA=root, REPORT=report,
                                PLAYLIST=playlist, STATE=state):
                save_checks([12], [12, 4])
                movies.update(False)
                self.assertFalse(playlist.exists())
                save_checks([30, 12], [30, 12])
                movies.update(False)
                self.assertEqual(playlist.read_text(encoding='utf-8').splitlines(), [
                    '#EXTM3U', '#EXTINF:-1 group-title="Фильмы — русский",Кино Full HD',
                    'https://example.com/1080.m3u8',
                ])
                save_checks([30, 12], [30, 12], full_ok=False)
                movies.update(False)
                self.assertIn('Кино HD\nhttps://example.com/720.m3u8', playlist.read_text(encoding='utf-8'))
                save_checks([], [])
                movies.update(False)
                self.assertFalse(playlist.exists())


if __name__ == '__main__':
    unittest.main()
