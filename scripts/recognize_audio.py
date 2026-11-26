"""Offline speech language evidence; no transcript or audio is published."""
import json
import sys
import wave
import numpy as np
from faster_whisper import WhisperModel

model=WhisperModel(sys.argv[2],device='cpu',compute_type='int8',cpu_threads=2,local_files_only=True)
with wave.open(sys.argv[1],'rb') as stream:
    if (stream.getnchannels(),stream.getsampwidth(),stream.getframerate())!=(1,2,16000):
        raise ValueError('Expected mono 16-bit PCM at 16 kHz')
    audio=np.frombuffer(stream.readframes(stream.getnframes()),dtype=np.int16).astype(np.float32)/32768
segments,info=model.transcribe(audio,beam_size=1,vad_filter=True)
segments=list(segments)
speech=sum(s.end-s.start for s in segments)
characters=sum(len(s.text.strip()) for s in segments)
print(json.dumps({'language':info.language,'probability':info.language_probability,
                  'speech_seconds':round(speech,3),'characters':characters,
                  'method':'offline faster-whisper/base, decoded audio sample; not a guarantee for all programming'}))
