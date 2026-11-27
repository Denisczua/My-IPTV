"""Regression: real interleaved FFmpeg stderr must not fabricate video jumps."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from check_main_movies import decoded_timestamps


class TimestampTests(unittest.TestCase):
    def test_audio_prefix_cannot_supply_video_timestamp(self):
        log='''[Parsed_showinfo_1 @ v] n: 196 pts: 705600 pts_time:7.84 duration:3600 duration_time:0.04 fmt:yuv420p cl:left sar:1/1 s:640x360 i:P
[Parsed_ashowinfo_1 @ a] n:376 pts:385024 pts_time:8.021333 fmt:fltp channels:2 chlayout:stereo rate:48000 nb_samples:1024 checksum:9393E628 plane_checksums: [ BD5CD51E 2E5A110A n: 197 pts: 709200 pts_time:7.88 duration:3600 duration_time:0.04 fmt:yuv420p cl:left sar:1/1 s:640x360 i:P
[Parsed_showinfo_1 @ v] n: 198 pts: 712800 pts_time:7.92 duration:3600 duration_time:0.04 fmt:yuv420p cl:left sar:1/1 s:640x360 i:P'''
        video,audio=decoded_timestamps(log)
        self.assertEqual(video,[7.84,7.88,7.92])
        self.assertEqual(audio,[8.021333])

    def test_real_negative_and_backward_timestamps_are_preserved(self):
        log='\n'.join(f'[Parsed_showinfo_1 @ v] n:{n} pts:{n} pts_time:{at} fmt:yuv420p sar:1/1 s:1280x720'
                      for n,at in enumerate(['0','0.04','-0.08','0.12']))
        video,_=decoded_timestamps(log)
        self.assertEqual(video,[0,.04,-.08,.12])
        self.assertTrue(any(b<=a for a,b in zip(video,video[1:])))
