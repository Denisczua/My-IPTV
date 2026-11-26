"""Scoped sleep inhibition and isolated, time-bounded media workers."""
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


class ProbeBusy(RuntimeError):
    pass


@contextmanager
def awake():
    kernel=ctypes.windll.kernel32 if os.name=='nt' else None
    if kernel and not kernel.SetThreadExecutionState(0x80000001):
        raise OSError('Cannot inhibit automatic idle sleep during active checks')
    try:yield
    finally:
        if kernel:kernel.SetThreadExecutionState(0x80000000)


@contextmanager
def progress_lock(root):
    """One probe across desktop checkouts and the runner's separate checkout."""
    root.mkdir(parents=True,exist_ok=True)
    with (root/'.probe.lock').open('a+b') as f:
        if os.fstat(f.fileno()).st_size==0:f.write(b'0');f.flush()
        f.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (BlockingIOError,PermissionError):
            raise ProbeBusy('Another direct-CZ check owns the shared progress journal') from None
        try:yield
        finally:
            if os.name=='nt':
                f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(f.fileno(),fcntl.LOCK_UN)


@contextmanager
def owned_job(process):
    """Windows job kills only our worker and its FFmpeg descendants on close."""
    handle=None
    if os.name=='nt':
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),
                      ('flags',w.DWORD),('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),
                      ('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(n,ctypes.c_ulonglong) for n in ('read','write','other','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_=[('basic',Basic),('io',IO),('process_mem',ctypes.c_size_t),('job_mem',ctypes.c_size_t),
                      ('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.CreateJobObjectW.restype=w.HANDLE
        k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        k.CloseHandle.argtypes=[w.HANDLE]
        handle=k.CreateJobObjectW(None,None)
        info=Extended();info.basic.flags=0x2000
        if not handle or not k.SetInformationJobObject(handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not k.AssignProcessToJobObject(handle,w.HANDLE(int(process._handle))):
            if handle:k.CloseHandle(handle)
            process.kill();process.wait()
            raise OSError('Worker job isolation failed')
    try:yield
    finally:
        if handle:k.CloseHandle(handle)


def isolated_check(channel,variant,ffmpeg,ffprobe,timeout=150):
    from filter_ru_ua import now,ROOT
    folder=ROOT/'.tools'/'probe-workers';folder.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder) as temp:
        inp=Path(temp)/'input.json';out=Path(temp)/'output.json'
        inp.write_text(json.dumps({'channel':{'movie_candidate':False},'variant':variant,
                                   'ffmpeg':ffmpeg,'ffprobe':ffprobe}),encoding='utf-8')
        env={k:v for k,v in os.environ.items() if 'proxy' not in k.lower()}
        p=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),str(inp),str(out)],
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env,
                           start_new_session=os.name!='nt')
        with owned_job(p):
            try:p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if os.name=='nt':subprocess.run(['taskkill','/PID',str(p.pid),'/T','/F'],capture_output=True,timeout=10)
                else:os.killpg(p.pid,signal.SIGKILL)
                p.wait(timeout=10)
                return {'at':now(),'result':'unknown','region':'CZ','reason':'Hard worker deadline; network/DNS/CPU cause not distinguished',
                        'elapsed_seconds':timeout,'measurement_version':3}
            if p.returncode or not out.exists():
                return {'at':now(),'result':'unknown','region':'CZ','reason':'Isolated worker infrastructure error','measurement_version':3}
            return json.loads(out.read_text(encoding='utf-8'))


if __name__=='__main__':
    # Child never decides location or publication; parent attaches current CZ proof.
    from check_main_movies import check
    data=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    result=check(data['channel'],data['variant'],data['ffmpeg'],data['ffprobe'])
    # Only paid/public licensing decisions remain outside this technical worker.
    # When tags are missing, measure speech instead of guessing from the title.
    if result.get('audio_decoded') and result.get('media',{}).get('audio_language') in ('und',None,''):
        asr=os.environ.get('MYIPTV_ASR_PYTHON');model=os.environ.get('MYIPTV_ASR_MODEL')
        if asr and model:
            from check_ru_ua import connection
            from check_main_movies import ENV,PROTOCOLS
            url,headers=connection(data['variant']['block']);audio=result['media']['selected_audio_index']
            wav=Path(sys.argv[2]).with_suffix('.wav')
            try:
                command=[data['ffmpeg'],'-nostdin','-v','error','-protocol_whitelist',PROTOCOLS,'-rw_timeout','8000000',
                         '-headers',''.join(f'{k}: {v}\r\n' for k,v in headers.items()),'-i',url,'-t','20',
                         '-map',f'0:{audio}','-vn','-ac','1','-ar','16000','-y',str(wav)]
                subprocess.run(command,check=True,capture_output=True,timeout=28,env=ENV)
                probe=subprocess.run([asr,str(Path(__file__).with_name('recognize_audio.py')),str(wav),model],
                                     check=True,capture_output=True,text=True,timeout=30,env=ENV)
                speech=json.loads(probe.stdout);result['speech_language']=speech
                if speech['probability']>=.95 and speech['speech_seconds']>=5 and speech['characters']>=30 and speech['language'] in ('ru','uk'):
                    result['audio_language_evidence']=speech['method']
                    result['media']['audio_language']='rus' if speech['language']=='ru' else 'ukr'
                    result['russian_audio_confirmed']=speech['language']=='ru'
                    result['media']['russian_default']=speech['language']=='ru' and audio==result['media']['default_audio_index']
            except Exception as error:result['speech_language_error']=type(error).__name__
            finally:wav.unlink(missing_ok=True)
    result['measurement_version']=3
    Path(sys.argv[2]).write_text(json.dumps(result),encoding='utf-8')
