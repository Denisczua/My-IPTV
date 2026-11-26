"""Merge trusted CZ runner observations into the latest catalogue, never the M3U."""
import argparse
import json
from pathlib import Path
from datetime import datetime,timezone

import filter_ru_ua as common
import main_catalogue as main
from audit_personal_cz import discoveries,replay


def run(path):
    # A trusted artifact is still validated before mutating the catalogue.
    rows=path.read_text(encoding='utf-8').splitlines()
    for line in rows:
        r=json.loads(line);h=r['observation']
        age=datetime.now(timezone.utc).timestamp()-datetime.fromisoformat(h['at']).timestamp()
        if h.get('region')!='CZ' or h.get('measurement_version')!=3 or not h.get('egress_sha256') or age< -120:
            raise ValueError('Untrusted or future CZ observation')
        if len(h['egress_sha256'])!=64 or len(h.get('observation_id',''))!=64:raise ValueError('Invalid proof identifier')
        if h['result'] not in ('ok','fail','geo','quality_fail','unknown'):raise ValueError('Invalid measurement outcome')
    with common.lock():
        db=main.load();discoveries(db);accepted=replay(db,path);common.save_json(main.STATE,db)
        report={'at':common.now(),'imported_observations':accepted,'source_records':len(rows)}
        common.save_json(common.DATA/'personal-import-report.json',report)
    print(json.dumps(report))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('journal',type=Path);run(p.parse_args().journal)
