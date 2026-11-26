"""Export only complete accepted observations while a local probe may append."""
import argparse
import json
import os
from pathlib import Path


def snapshot(source,target):
    if not source.exists():return 0
    lines=source.read_text(encoding='utf-8').splitlines();accepted=[]
    for index,line in enumerate(lines):
        try:json.loads(line)
        except json.JSONDecodeError:
            if index==len(lines)-1:break
            raise ValueError('Corrupt journal; no export')
        accepted.append(line)
    if accepted:target.write_text('\n'.join(accepted)+'\n',encoding='utf-8')
    return len(accepted)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=Path(os.environ.get('LOCALAPPDATA','.tools'))/'MyIPTV-CZ-runner/progress/observations.jsonl')
    p.add_argument('--output',type=Path,default=Path('personal-observations.jsonl'))
    a=p.parse_args();print(json.dumps({'complete_observations_exported':snapshot(a.source,a.output)}))
