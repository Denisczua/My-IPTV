from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from audit_personal_cz import replay,repair_journal_tail,spaced_checks
from main_catalogue import stream_key
from probe_runtime import progress_lock,ProbeBusy,verify_tools
from unittest.mock import patch
from types import SimpleNamespace
from export_cz_journal import snapshot


class JournalTests(unittest.TestCase):
    def test_replay_survives_truncated_tail_and_deduplicates_same_transport(self):
        block=['#EXTINF:-1 tvg-id="Example.kz",Example','https://example.org/live.m3u8']
        key=stream_key(block)
        now=datetime.now(timezone.utc)
        variant={'block':block,'history':[]}
        db={'channels':{'Example.kz':{'variants':{key:variant}}}}
        h={'at':(now-timedelta(seconds=10)).isoformat(),'region':'CZ','egress_sha256':'a'*64,
           'result':'ok','observation_id':'b'*64,'measurement_version':3}
        row={'channel':'Example.kz','variant':key,'observation':h}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            path=Path(temp)/'journal.jsonl';path.write_text(json.dumps(row)+'\n{"interrupted":',encoding='utf-8')
            repair_journal_tail(path)
            self.assertEqual(replay(db,path),1);self.assertEqual(replay(db,path),0)
            self.assertEqual(len(variant['history']),1)
            self.assertTrue(path.with_suffix('.interrupted-tail').exists())

    def test_reused_samples_cannot_count_as_independent_passes(self):
        now=datetime.now(timezone.utc)
        history=[{'at':(now-timedelta(seconds=offset)).isoformat(),'measurement_version':3,'egress_sha256':'a'} for offset in (1800,1700,1200)]
        self.assertEqual(len(spaced_checks({'history':history},'a')),2)

    def test_two_checkouts_cannot_probe_simultaneously(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root=Path(temp)
            with progress_lock(root):
                with self.assertRaises(ProbeBusy):
                    with progress_lock(root):pass
            with progress_lock(root):pass

    def test_artifact_snapshot_does_not_export_partial_last_record(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root=Path(temp);source=root/'journal';target=root/'artifact'
            source.write_text('{"accepted":true}\n{"writing":',encoding='utf-8')
            self.assertEqual(snapshot(source,target),1)
            self.assertEqual(json.loads(target.read_text()),{'accepted':True})

    def test_ffprobe_cannot_be_mistaken_for_ffmpeg(self):
        with patch('probe_runtime.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=b'ffprobe version 9.1')):
            with self.assertRaises(RuntimeError):verify_tools('wrong','ffprobe')
