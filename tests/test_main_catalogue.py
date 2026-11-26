import copy
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from collections import defaultdict

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import main_catalogue as m
import movie_sources as sources
import check_main_movies as probe
import filter_ru_ua as common


def block(ident,name='Кино',url='https://example.org/live',group='Movies',extra=''):
    return [f'#EXTINF:-1 tvg-id="{ident}" tvg-logo="https://example.org/logo" group-title="{group}" {extra},{name}',url]


def history(results,clock=100000):
    return [{'at':datetime.fromtimestamp(clock-(len(results)-1-i)*m.MIN_INTERVAL,timezone.utc).isoformat(),
             'region':'CZ','egress_sha256':'test','result':r} for i,r in enumerate(results)]


class IdentityTests(unittest.TestCase):
    def metadata(self):
        meta=m.Metadata.__new__(m.Metadata)
        meta.channels={'Cinema.us':{'id':'Cinema.us','name':'Cinema','categories':['movies']}}
        meta.ids={'cinema.us':'Cinema.us'}
        meta.names=defaultdict(set,{'cinema':{'Cinema.us'},'кино':{'Cinema.us'}})
        meta.reviews={'channels':{},'aliases':{}}
        meta.languages=defaultdict(set,{'Cinema.us':{'rus'}});meta.feeds={}
        return meta

    def test_quality_suffixes_aliases_and_tvg_id(self):
        meta=self.metadata()
        self.assertEqual(meta.identity(block('Cinema.us@HD','Cinema Full HD')), 'Cinema.us')
        self.assertEqual(meta.identity(block('','Кино 4K UHD')),'Cinema.us')
        self.assertNotEqual(meta.identity(block('Cinema2.us','Cinema 2 HD')),meta.identity(block('Cinema.us','Cinema HD')))
        self.assertEqual(m.normalize('Cinema (1080p) HD'),'cinema')

    def test_same_logo_does_not_merge_independent_schedules(self):
        meta=self.metadata()
        self.assertNotEqual(meta.identity(block('BrandAction.ru','Brand Action')),meta.identity(block('BrandDrama.ru','Brand Drama')))

    def test_ambiguous_generic_name_needs_corroboration(self):
        meta=self.metadata()
        a=block('','TV8',extra='');b=block('','TV8')
        b[0]=b[0].replace('https://example.org/logo','https://different.org/logo')
        self.assertNotEqual(meta.identity(a),meta.identity(b))

    def test_different_headers_are_independent_reserves(self):
        a=block('Cinema.us');b=block('Cinema.us@HD',group='OLED')
        self.assertEqual(m.stream_key(a),m.stream_key(b))
        b.insert(1,'#EXTVLCOPT:http-referrer=https://broadcaster.example/')
        self.assertNotEqual(m.stream_key(a),m.stream_key(b))

    def test_18_plus_is_not_a_movie_specialization_test(self):
        meta=self.metadata()
        self.assertFalse(meta.describe(block('Cinema.us','Фильмы ужасов 18+'),'Cinema.us')['adult_specialization'])
        meta.channels['Cinema.us']['categories']=['xxx']
        self.assertTrue(meta.describe(block('Cinema.us'),'Cinema.us')['adult_specialization'])

    def test_explicit_programme_review_overrides_a_wrong_movie_category(self):
        meta=self.metadata();meta.reviews['channels']['Cinema.us']={'movie':False,'evidence':'Official backstage-only programming'}
        self.assertFalse(meta.describe(block('Cinema.us'),'Cinema.us')['movie_confirmed'])


class EvidenceTests(unittest.TestCase):
    def test_three_successes_and_failure_hysteresis(self):
        self.assertEqual(m.status({'history':history(['ok'])},100000),'UNKNOWN_CZ')
        self.assertEqual(m.status({'history':history(['ok']*3)},100000),'STABLE_CZ')
        self.assertEqual(m.status({'history':history(['ok']*3+['fail']),'was_stable':True},100000),'STABLE_CZ')
        self.assertEqual(m.status({'history':history(['ok']*3+['fail']*2),'was_stable':True},100000),'UNSTABLE_CZ')
        self.assertEqual(m.status({'history':history(['fail']*3)},100000),'OFFLINE')
        self.assertEqual(m.status({'history':history(['geo']*2)},100000),'GEO_BLOCKED_CZ')

    def test_burst_or_non_cz_or_old_success_not_proof(self):
        v={'history':history(['ok']*3)}
        for h in v['history']:h['at']=v['history'][-1]['at']
        self.assertEqual(m.status(v,100000),'UNKNOWN_CZ')
        v={'history':history(['ok']*3)}
        for h in v['history']:h['region']='US'
        self.assertEqual(m.status(v,100000),'UNKNOWN_CZ')
        self.assertEqual(m.status({'history':history(['ok']*3)},100000+m.MAX_AGE+1),'UNKNOWN_CZ')

    def test_catalogue_language_is_not_confirmed_audio(self):
        ch={'movie_confirmed':True,'adult_specialization':False}
        v={'rights_evidence':'official broadcaster','status':'STABLE_CZ','history':[
            {'result':'ok','media':{'width':1920},'audio_decoded':True,'russian_audio_confirmed':False}]}
        self.assertFalse(m.eligible_movie(ch,v))
        v['history'][0]['russian_audio_confirmed']=True
        self.assertTrue(m.eligible_movie(ch,v))
        v['history'][0]['media']['russian_default']=False
        self.assertFalse(m.eligible_movie(ch,v))
        v['history'][0]['media']['russian_default']=True
        v['rights_evidence']=''
        self.assertFalse(m.eligible_movie(ch,v))

    def test_every_resolution_including_1080i_and_sd(self):
        for size,group in [((3840,2160),0),((1920,1080),1),((1280,720),2),((768,432),3)]:
            self.assertEqual(m.resolution(dict(zip(('width','height'),size))),m.GROUPS[group])
        with self.assertRaises(ValueError):m.resolution({})

    def test_stability_precedes_resolution(self):
        def variant(size,results):
            hs=history(results)
            for h in hs:h['media']={'width':size[0],'height':size[1],'video_codec':'h264','audio_codec':'aac'}
            return {'history':hs}
        hd=variant((1280,720),['ok']*3)
        uhd=variant((3840,2160),['fail','ok','ok'])
        self.assertGreater(m.quality(hd),m.quality(uhd))
        uhd=variant((3840,2160),['ok']*3)
        self.assertGreater(m.quality(uhd),m.quality(hd))

    def test_sources_account_for_placeholders_and_do_not_truncate(self):
        text='#EXTM3U\n'+ '\n'.join(block('A'))+'\n#EXTINF:-1,No stream\n[NO PUBLIC STREAM]\n'
        usable,omitted=sources.parse_source(text)
        self.assertEqual(len(usable),1);self.assertEqual(len(omitted),1)
        with self.assertRaises(ValueError):sources.parse_source('#EXTM3U\n#EXTINF:-1,Broken\n')


class SelectionTests(unittest.TestCase):
    def fixture(self):
        def var(ident,url,stable=False,size=(1280,720)):
            h={'result':'ok','media':{'width':size[0],'height':size[1],'video_codec':'h264','audio_codec':'aac'},
               'russian_audio_confirmed':True,'audio_decoded':True,'startup_seconds':1}
            return {'block':block(ident,url=url),'existing':True,'history':[h] if stable else [],
                    'status':'STABLE_CZ' if stable else 'UNKNOWN_CZ','rights_evidence':'official' if stable else '',
                    'sources':[]}
        c={'name':'Кино','movie_candidate':True,'movie_confirmed':True,'adult_specialization':False,
           'catalogue_languages':['rus'],
           'baseline':True,'groups':['Movies'],'variants':{'old':var('A','https://old/live'),'hd':var('A','https://hd/live',True)}}
        return {'channels':{'A':c},'selected':{},'current':{'A':'old'},
                'scan':{'before_entries':2,'before_identities':1,'primary_available':True,'providers_available':5}}

    def choose(self,db,root):
        meta=type('Metadata',(),{'identity':lambda _,b:common.attrs(b[0])['tvg-id'].split('@')[0],
                                 'reviews':{'channels':{}}})()
        source=root/'playlist.m3u'
        source.write_text('#EXTM3U x-tvg-url="https://epg.example/xml"\n'+'\n'.join(block('A'))+'\n',encoding='utf-8')
        with patch.object(m,'PLAYLIST',source):return m.choose(db,meta)

    def test_deduplication_and_backup_selection_are_one_entry(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            db=self.fixture();text,summary=self.choose(db,Path(folder))
        self.assertEqual(len(common.parse(text)),1)
        self.assertIn('https://hd/live',text)
        self.assertEqual(summary['duplicates_removed'],1)
        self.assertEqual(summary['existing_streams_improved'],1)
        self.assertIn('https://epg.example/xml',text)

    def test_oled_never_duplicates_channel(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            db=self.fixture();db['channels']['A']['groups']=['OLED','Movies']
            text,_=self.choose(db,Path(folder))
        self.assertEqual(len(common.parse(text)),1)
        self.assertEqual(common.attrs(common.parse(text)[0][0])['group-title'],'OLED')

    def test_unconfirmed_new_channel_not_published(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            db=self.fixture();new=copy.deepcopy(db['channels']['A']);new['baseline']=False
            new['variants']={'bad':{'block':block('B'),'history':[],'status':'UNKNOWN_CZ','rights_evidence':'','sources':[]}}
            db['channels']['B']=new
            text,summary=self.choose(db,Path(folder))
        self.assertEqual(len(common.parse(text)),1)
        self.assertEqual(summary['unique_movies_added'],0)

    def test_source_outage_rejects_publication(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            db=self.fixture();db['scan']['providers_available']=1
            with self.assertRaises(RuntimeError):self.choose(db,Path(folder))

    def test_single_failure_retains_working_channel(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            db=self.fixture();db['channels']['A']['variants'].pop('hd')
            db['channels']['A']['variants']['old']['status']='UNKNOWN_CZ'
            text,_=self.choose(db,Path(folder))
        self.assertIn('https://old/live',text)


if __name__=='__main__':unittest.main()
