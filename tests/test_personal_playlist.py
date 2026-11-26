from datetime import datetime,timedelta,timezone
import sys
from pathlib import Path
import unittest
import copy

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from personal_playlist import export
from main_catalogue import stream_key


def fixture(count=3,rights=True):
    block=['#EXTINF:-1 tvg-id="Cinema.kz" tvg-logo="https://example.org/logo.png",Cinema 4K',
           '#EXTVLCOPT:http-referrer=https://example.org/', 'https://example.org/live.m3u8']
    key=stream_key(block);now=datetime.now(timezone.utc)
    history=[{'at':(now-timedelta(seconds=1800-i*600)).isoformat(),'result':'ok','region':'CZ',
              'egress_sha256':'a'*64,'measurement_version':3,'audio_decoded':True,
              'video_continuous':True,'audio_continuous':True,'russian_audio_confirmed':True,
              'media':{'width':1280,'height':720,'audio_language':'rus','selected_audio_index':1,'default_audio_index':1}}
             for i in range(count)]
    variant={'block':block,'history':history,'status':'STABLE_CZ','rights_evidence':'https://example.org/live' if rights else ''}
    db={'channels':{'Cinema.kz':{'name':'Cinema 4K','movie_confirmed':True,'variants':{key:variant}}},'selected':{'Cinema.kz':key}}
    return db,{'groups':{},'names':{'Cinema.kz':'Кино'}},variant


class PersonalTests(unittest.TestCase):
    def test_confirmed_movie_preserves_options_and_only_measured_quality(self):
        db,policy,v=fixture();before=copy.deepcopy(db)
        body,report=export(db,policy,{}, {},{})
        text=body.decode()
        self.assertIn('Кино HD',text);self.assertNotIn('4K',text)
        self.assertIn('group-title="Кино"',text);self.assertIn('#EXTVLCOPT:',text)
        self.assertEqual(report['channels'],1);self.assertEqual(report['oled'],0)
        self.assertEqual(db,before)

    def test_one_success_or_missing_rights_or_foreign_language_cannot_publish(self):
        for count,rights,language in [(1,True,'rus'),(3,False,'rus'),(3,True,'eng')]:
            db,policy,v=fixture(count,rights)
            for h in v['history']:
                h['media']['audio_language']=language;h['russian_audio_confirmed']=language=='rus'
            with self.assertRaises(ValueError):export(db,policy,{}, {},{})

    def test_group_limits_and_one_oled_record(self):
        db,policy,v=fixture();policy['groups']={'Новости России':{'limit':1,'ids':['A.ru','B.ru']}}
        db['channels']={i:{'name':i,'movie_confirmed':False,'variants':{stream_key(v['block']):copy.deepcopy(v)}} for i in ('A.ru','B.ru')}
        oled={'A.ru':{'approved':True,'method':'visual','evidence':'dated observation','reviewed_at':'2026-11-26'}}
        body,report=export(db,policy,{}, {},oled)
        self.assertEqual(report['channels'],1);self.assertEqual(report['oled'],1)
        self.assertEqual(body.count(b'#EXTINF:'),1)

    def test_old_checks_and_wrong_default_audio_cannot_publish(self):
        db,policy,v=fixture()
        for h in v['history']:h['measurement_version']=2
        with self.assertRaises(ValueError):export(db,policy,{}, {},{})
        db,policy,v=fixture()
        for h in v['history']:h['media']['default_audio_index']=2
        with self.assertRaises(ValueError):export(db,policy,{}, {},{})
