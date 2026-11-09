"""Create a whitelist-only runtime bundle, excluding tests, data and secrets."""
from pathlib import Path
import argparse
import hashlib
import io
import json
import os
import re
import tarfile

ROOT=Path(__file__).resolve().parent
FILES=[
    'server/__init__.py','server/app.py','server/store.py','server/locks.py','server/backup.py','server/admin_cli.py',
    'frontend/index.html','frontend/styles.css','frontend/app.js','frontend/public.html','frontend/public.js',
    'frontend/assets/tersus-logo.png','frontend/assets/auth-space-generic.svg',
    'admin/index.html','admin/admin.css','admin/admin.js',
    'viewer/index.html','viewer/viewer.css','viewer/viewer.js','viewer/vendor/three.module.js','viewer/vendor/three.core.js',
    'viewer/vendor/LICENSE','viewer/vendor/NOTICE','viewer/vendor/addons/controls/OrbitControls.js',
    'viewer/vendor/addons/loaders/GLTFLoader.js','viewer/vendor/addons/utils/BufferGeometryUtils.js',
    'deploy/nginx-limits.conf','deploy/nginx-workspace.conf','deploy/tersus-workspace.service',
    'deploy/tersus-workspace-backup.service','deploy/tersus-workspace-backup.timer','README.zh-CN.md',
]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True)
    parser.add_argument('--version',default='20261028-s1-workspace-v1');args=parser.parse_args()
    if not re.fullmatch(r'[0-9]{8}-s1-workspace-v[0-9]+',args.version):raise ValueError('invalid release version')
    output=Path(args.output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    payload={}
    for name in FILES:
        path=ROOT/name
        assert path.is_file() and not path.is_symlink() and '..' not in Path(name).parts,name
        payload[name]=path.read_bytes()
    manifest={'version':args.version,'files':{name:{'size':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in payload.items()}}
    payload['release-manifest.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8')
    temp=output.with_name(output.name+'.tmp')
    with tarfile.open(str(temp),'w:gz') as archive:
        for name,data in sorted(payload.items()):
            info=tarfile.TarInfo(name);info.mode=0o644;info.size=len(data);info.mtime=0
            archive.addfile(info,io.BytesIO(data))
    os.replace(str(temp),str(output))
    data=output.read_bytes()
    info={'version':manifest['version'],'files':len(manifest['files']),'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    output.with_suffix(output.suffix+'.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(info,ensure_ascii=False))


if __name__=='__main__':main()
