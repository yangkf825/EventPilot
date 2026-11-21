#!/usr/bin/env python3
"""Package code, reviewed data and the unchanged required local HTML archive."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def selected_files():
    names=['README.md','activate.sh','requirements.txt','requirements.lock.txt','models.json','.gitignore',
           'corpus/data/normalized/tasks.jsonl','corpus/snapshots.zip',
           'corpus/ATTRIBUTION.md','corpus/upstream/Mind2Web_README.md']
    for directory in ('eventpilot','scripts','tests','docs','data','review'):
        for path in sorted((ROOT/directory).rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or path.name.startswith(('~$','.')):continue
            if path.suffix=='.py' and not path.stem.isidentifier():continue
            if path.suffix not in ('.py','.zsh','.sh','.ps1','.json','.jsonl','.md','.xlsx'):continue
            names.append(str(path.relative_to(ROOT)))
    for name in ('validation/release_validation.json','validation/v12_validation.json'):
        if (ROOT/name).is_file():names.append(name)
    return sorted(set(names))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    checksums={}
    with zipfile.ZipFile(args.output,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for name in selected_files():
            raw=(ROOT/name).read_bytes()
            checksums[name]=hashlib.sha256(raw).hexdigest()
            archive.writestr('EventPilot_offline_v12/'+name,raw)
        archive.writestr('EventPilot_offline_v12/RELEASE_SHA256.json',json.dumps(checksums,indent=2)+'\n')
    with zipfile.ZipFile(args.output) as archive:
        bad=archive.testzip()
        if bad:raise ValueError('ZIP checksum failure: '+bad)
    sha=hashlib.sha256(args.output.read_bytes()).hexdigest()
    args.output.with_suffix(args.output.suffix+'.sha256').write_text(sha+'  '+args.output.name+'\n')
    print(json.dumps({'zip':str(args.output.resolve()),'files':len(checksums)+1,
                      'bytes':args.output.stat().st_size,'sha256':sha},indent=2))


if __name__=='__main__':main()
