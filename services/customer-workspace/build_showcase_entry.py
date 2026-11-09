"""Produce a minimal workspace entry over the verified current demo release."""
from pathlib import Path
import copy
import hashlib
import json
import os

ROOT=Path(__file__).resolve().parent
REPO=ROOT.parent.parent
W=REPO/'work/customer-workspace-20261028/showcase-link'
previous=REPO/'work/product-polish-20261028'
base=json.loads((W/'baseline.json').read_text(encoding='utf-8'))
old=json.loads((previous/'plan.json').read_text(encoding='utf-8'))
assert base['files']==old['candidate'] and base['release'].endswith('/'+old['version'])
source=(previous/'candidate/index.html').read_bytes()
assert hashlib.sha256(source).hexdigest()==base['files']['index.html']['sha256']
html=source.decode('utf-8')
before='<a class="header-link" href="#projects">浏览项目 <span aria-hidden="true">↗</span></a>'
after=('<nav class="showcase-workspace-links" aria-label="展厅导航">'+before+
       '<a class="showcase-workspace-entry" href="https://mvpmodel.tersus-gnss.com/workspace/">进入工作台 <span aria-hidden="true">↗</span></a></nav>')
assert html.count(before)==1 and html.count('</head>')==1
html=html.replace(before,after,1).replace('</head>','<link rel="stylesheet" href="/BIM-DEMO/assets/workspace-entry-20261028.css">\n</head>',1)
version='20261028-workspace-entry-v1'
metadata=copy.deepcopy(base['metadata']);metadata['version']=version
metadata['presentationUi'].update({'version':version,'scope':'workspace entry','independentAcceptance':'NOT_ASSESSED'})
payload={'index.html':html.encode('utf-8'),
         'assets/workspace-entry-20261028.css':(ROOT/'deploy/showcase-entry.css').read_bytes(),
         'release.json':json.dumps(metadata,ensure_ascii=False,indent=2).encode('utf-8')}
for name,data in payload.items():
    target=W/'candidate'/name;target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_name(target.name+'.tmp');temp.write_bytes(data);os.replace(str(temp),str(target))
rows={name:{'size':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in payload.items()}
candidate=dict(base['files']);candidate.update(rows)
changed=[name for name in base['files'] if candidate[name]!=base['files'][name]]
assert set(changed)=={'index.html','release.json'}
plan={'version':version,'baseline':base['release'],'payload':rows,'candidate':candidate,
      'changedBaselineFiles':changed,'preservedFiles':len(base['files'])-len(changed),'pages':old['pages'],
      'missingPages':[],'routeFix':old['routeFix'],'producerSha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
target=W/'plan.json';tmp=target.with_name(target.name+'.tmp')
tmp.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(str(tmp),str(target))
print(json.dumps({'version':version,'changedFiles':len(rows),'preservedFiles':plan['preservedFiles']}))
