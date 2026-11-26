import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from ottplayer_export import export
from main_catalogue import stream_key
from filter_ru_ua import parse


class ExportTests(unittest.TestCase):
    def test_feed_language_overrides_country_and_ambiguous_channel_union(self):
        def b(ident,group='Movies'):
            return [f'#EXTINF:-1 tvg-id="{ident}" group-title="{group}",Example',
                    '#EXTVLCOPT:http-referrer=https://example.org/',f'https://example.org/{ident}']
        ru=b('Cinema.kz@RU','OLED');en=b('Cinema.kz@EN');unknown=b('Cinema.kz');ua=b('Other.us@UA')
        channels={}
        for ident,block in [('Cinema.kz',ru),('Other.us',ua)]:
            channels[ident]={'variants':{stream_key(block):{'block':block,'status':'UNKNOWN_CZ','history':[]}},'movie_confirmed':False}
        cat={'channels':channels}
        feeds={'Cinema.kz@RU':['rus'],'Cinema.kz@EN':['eng'],'Other.us@UA':['ukr']}
        before=repr(cat)
        raw,report=export([ru,en,unknown,ua,ru],cat,feeds)
        self.assertEqual(len(parse(raw.decode())),2)
        self.assertIn(b'group-title="OLED"',raw)
        self.assertIn(b'#EXTVLCOPT:',raw)
        self.assertEqual(report['excluded']['duplicates'],1)
        self.assertEqual(report['excluded']['other_or_unconfirmed_language'],2)
        self.assertEqual(repr(cat),before)

    def test_empty_language_selection_fails_closed(self):
        with self.assertRaises(ValueError):export([],{'channels':{}},{})
