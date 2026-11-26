import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from build_playlist_site import build
import filter_ru_ua as common


class PlaylistMirrorTests(unittest.TestCase):
    def test_both_urls_preserve_every_byte_and_stream_option(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);source=root/'main.m3u';site=root/'site'
            raw=('#EXTM3U\n#EXTINF:-1 tvg-id="Example.ru" group-title="OLED",Кино\n'
                 '#EXTVLCOPT:http-referrer=https://example.org/\nhttps://example.org/live.m3u8\n').encode('utf-8')
            source.write_bytes(raw)
            result=build(source,site,'abc123')
            self.assertEqual((site/'playlist.m3u').read_bytes(),raw)
            self.assertEqual((site/'playlist.m3u8').read_bytes(),raw)
            self.assertEqual(source.read_bytes(),raw)
            self.assertEqual(result['sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(result['channels'],1)
            self.assertFalse(result['ottplayer_connection_confirmed'])

    def test_invalid_input_cannot_replace_old_mirror(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);source=root/'main.m3u';site=root/'site';site.mkdir()
            (site/'playlist.m3u').write_bytes(b'old working version')
            source.write_text('#EXTM3U\n',encoding='utf-8')
            with self.assertRaises(ValueError):build(source,site)
            self.assertEqual((site/'playlist.m3u').read_bytes(),b'old working version')

    def test_diagnostics_isolate_header_size_and_newlines(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root=Path(d);source=root/'main.m3u';site=root/'site'
            blocks=[f'#EXTINF:-1 group-title="OLED",Channel {i}\n#EXTVLCOPT:http-referrer=https://example.org/\nhttps://example.org/{i}\n' for i in range(105)]
            header='#EXTM3U x-tvg-url="https://example.org/epg.xml"\n'
            raw=(header+''.join(blocks)).encode();source.write_bytes(raw)
            result=build(source,site)
            simple=(site/'playlist-simple.m3u').read_bytes()
            self.assertEqual(simple,b'#EXTM3U\n'+''.join(blocks).encode())
            self.assertEqual(result['diagnostics']['diagnostic-100.m3u']['channels'],100)
            self.assertEqual((site/'diagnostic-100.m3u').read_bytes(),b'#EXTM3U\n'+''.join(blocks[:100]).encode())
            self.assertEqual((site/'diagnostic-100-epg.m3u').read_bytes(),(header+''.join(blocks[:100])).encode())
            self.assertEqual((site/'playlist-simple-crlf.m3u').read_bytes().replace(b'\r\n',b'\n'),simple)
            self.assertEqual(source.read_bytes(),raw)

    def test_post_replace_error_rolls_back_the_changed_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            p=Path(d)/'one';p.write_text('original',encoding='utf-8')
            atomic=common.atomic;injected=[]
            def fail_after_replace(path,text):
                atomic(path,text)
                if not injected:
                    injected.append(True)
                    raise OSError('Failure after replacement')
            with patch.object(common,'atomic',side_effect=fail_after_replace),self.assertRaises(OSError):
                common.transaction({p:'new'})
            self.assertEqual(p.read_text(encoding='utf-8'),'original')


if __name__=='__main__':unittest.main()
