"""Install a wheel outside the checkout and exercise it with checkout reads denied."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import venv
import zipfile
from pathlib import Path

PROBE = r'''
import asyncio, hashlib, json, os, sys, uuid
from datetime import datetime, timezone
from pathlib import Path
import laspy
import numpy as np
from mcp import ClientSession, StdioServerParameters, stdio_client
from indoor_reconstruction.repository import repository_root
root=repository_root()
assert root.name=='_runtime' and 'site-packages' in str(root)
work=Path(sys.argv[1]); capture=work/'capture'; capture.mkdir()
cloud=laspy.LasData(laspy.LasHeader(point_format=3,version='1.2'))
cloud.x=np.tile(np.linspace(0,4,21),21); cloud.y=np.repeat(np.linspace(0,3,21),21)
cloud.z=np.zeros(441); cloud.write(capture/'synthetic.las')
policies=json.loads((root/'schemas/tool-policies-v1.json').read_text(encoding='utf-8'))['policies']
def identity(role, policy):
    value={'schemaVersion':'1.0','actorId':'standalone-'+role,'runId':str(uuid.uuid4()),'role':role,
      'provider':'standalone-install-test','model':'synthetic','policyId':policy,
      'toolPolicyHash':hashlib.sha256(json.dumps(policies[policy],sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),
      'startedAt':datetime.now(timezone.utc).isoformat(),
      'attestation':{'issuer':'standalone-install-test','enforcementMode':'application-enforced'}}
    if role=='reviewer': value['reviewerClass']='regional'
    return value
scene=work/'mcp-scene.json'
async def exercise(role, policy):
    actor=identity(role,policy); path=work/(role+'.json');path.write_text(json.dumps(actor),encoding='utf-8')
    params=StdioServerParameters(command=sys.executable,args=['-m','indoor_reconstruction.cli','mcp',
        '--scene',str(scene),'--identity',str(path)],cwd=work)
    async with stdio_client(params) as (read,write):
      async with ClientSession(read,write) as session:
        initialized=await session.initialize();assert initialized.server_info.name=='semantic-scene-v2'
        tools=await session.list_tools();assert tools.tools
        if role=='author':
          result=await session.call_tool('init_scene',{'dataset':'synthetic-standalone'})
          assert not result.is_error,result
          result=await session.call_tool('create_wall',{'id':'wall_standalone','start':[0,0],'end':[4,0],'height':2.7,'thickness':0.12})
          assert not result.is_error,result
        else:
          before=scene.read_bytes()
          result=await session.call_tool('create_wall',{'id':'forbidden','start':[0,1],'end':[4,1]})
          assert result.is_error and scene.read_bytes()==before
asyncio.run(exercise('author','author-v1'))
asyncio.run(exercise('reviewer','reviewer-readonly-v1'))
print(json.dumps({'mcpStdio':True,'reviewerMutationRejectedWithoutDiskChange':True}))
'''

GUARD = r'''
import os, sys
from pathlib import Path
blocked=Path(os.environ['INDOOR_RECON_FORBID_SOURCE']).resolve()
allowed=[Path(p).resolve() for p in os.environ.get('INDOOR_RECON_DEPENDENCY_SITES','').split(os.pathsep) if p]
def guard(event,args):
    if event=='open' and args and isinstance(args[0],(str,bytes)):
        p=Path(os.fsdecode(args[0])).resolve()
        if p.is_relative_to(blocked) and not any(p.is_relative_to(a) for a in allowed):
            raise PermissionError('CHECKOUT_ACCESS_FORBIDDEN')
sys.addaudithook(guard)
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', required=True, type=Path)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--dependency-site', type=Path,
                        help='Local offline check only: reuse installed third-party dependencies, without .pth execution')
    parser.add_argument('--constraints', type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    work = args.work.resolve()
    if work.exists() or work.is_relative_to(source):
        parser.error('Use a new directory outside the source checkout')
    work.mkdir(parents=True)
    wheel = work / args.wheel.name
    wheel.write_bytes(args.wheel.read_bytes())
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert any(n.endswith('_runtime/runtime-manifest.json') for n in names)
        assert not any(n.endswith(('.glb', '.ifc', '.npz', '.las', '.laz', '.webp')) for n in names)
    environment = work / 'environment'
    venv.EnvBuilder(with_pip=True).create(environment)
    executable = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    command = environment / ('Scripts/indoor-recon.exe' if os.name == 'nt' else 'bin/indoor-recon')
    install = [str(executable), '-m', 'pip', 'install', str(wheel) + '[presentation]']
    constraints = args.constraints or source / 'constraints-runtime.txt'
    copied_constraints = work / 'constraints-runtime.txt'
    copied_constraints.write_bytes(constraints.read_bytes())
    install.extend(['--constraint', str(copied_constraints)])
    if args.dependency_site:
        install.append('--no-deps')
    subprocess.run(install, check=True, cwd=work)
    site = Path(subprocess.run([str(executable), '-c', 'import sysconfig;print(sysconfig.get_path("purelib"))'],
                              capture_output=True, text=True, check=True).stdout.strip())
    if args.dependency_site:
        dependencies = args.dependency_site.resolve()
        entries = [str(dependencies)]
        if os.name == 'nt':
            entries += [str(dependencies / part) for part in ('win32', 'win32/lib', 'Pythonwin')]
            entries.append('import pywin32_bootstrap')
        (site / 'third-party-dependencies.pth').write_text('\n'.join(entries) + '\n', encoding='utf-8')
    (site / 'sitecustomize.py').write_text(GUARD, encoding='utf-8')
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env['INDOOR_RECON_FORBID_SOURCE'] = str(source)
    env['INDOOR_RECON_DEPENDENCY_SITES'] = str(args.dependency_site.resolve()) if args.dependency_site else ''
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PATH'] = str(executable.parent)  # No Git or Node on the runtime PATH.
    results = []

    def run(label, arguments, *, ok=True, launcher=False):
        cmd = ([str(command)] if launcher else [str(executable), '-m', 'indoor_reconstruction.cli']) + arguments
        result = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, encoding='utf-8', timeout=120)
        (work / (label + '.log')).write_text(result.stdout + result.stderr, encoding='utf-8')
        assert (result.returncode == 0) == ok, (label, result.returncode, result.stdout, result.stderr)
        results.append({'name': label, 'exitCode': result.returncode})
        return result.stdout

    doctor = json.loads(run('doctor', ['doctor', '--json'], launcher=True))
    assert doctor['runtimeKind'] == 'installed' and doctor['resourceCount'] > 50
    assert not doctor['nodeAvailable']
    forbidden = run('reject-resource-overwrite', ['benchmark','--output',str(site/'indoor_reconstruction/_runtime/forbidden.json')], ok=False)
    assert json.loads(forbidden)['error']['code'] == 'OUTPUT_IN_RUNTIME_FORBIDDEN'
    # A fake checkout in CWD must never override the installed implementation.
    (work / 'schemas').mkdir()
    (work / 'scene-core').mkdir()
    (work / 'schemas/pipeline-contract-v2.json').write_text('{}', encoding='utf-8')
    (work / 'scene-core/scene_api.py').write_text('raise RuntimeError("WRONG_CHECKOUT")', encoding='utf-8')
    assert json.loads(run('poisoned-cwd-doctor', ['doctor', '--json']))['runtimeDigest'] == doctor['runtimeDigest']
    probe = work / 'probe.py'
    probe.write_text(PROBE, encoding='utf-8')
    result = subprocess.run([str(executable), str(probe), str(work)], cwd=work, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=120)
    (work / 'mcp.log').write_text(result.stdout + result.stderr, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    # Reuse only the checked-in synthetic fixture producer, not customer data
    # or source modules. The generated probe imports the installed runtime.
    fixture_code = '''from pathlib import Path
import json, copy, sys
import numpy as np
from PIL import Image, ImageDraw
from indoor_reconstruction.repository import repository_root
sys.path.insert(0,str(repository_root()/'scene-core'))
from photo_projection import CameraModel,Frame,project_points
from multiview_fit import EDGES,corners,sha256
'''
    for filename, functions in [('test_multiview_refinement.py', {'c2w', 'scene'}),
                                ('test_multiview_batch.py', {'write', 'prepare'})]:
        tree = ast.parse((source / 'tests' / filename).read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in functions:
                node.decorator_list = []
                fixture_code += ast.unparse(node) + '\n'
    fixture_code += f"folder=Path({str(work / 'multiview-case')!r});folder.mkdir();prepare(scene(folder))\n"
    generator = work / 'generate-multiview.py'
    generator.write_text(fixture_code, encoding='utf-8')
    generated = subprocess.run([str(executable), str(generator)], cwd=work, env=env,
                               capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert generated.returncode == 0, generated.stderr
    run('multiview-fit', ['multiview','fit','--input',str(work/'multiview-case/case.json'),
                          '--output',str(work/'multiview-fit.json')])
    fitted = json.loads((work/'multiview-fit.json').read_text(encoding='utf-8'))['objects'][0]
    assert fitted['reviewStatus'] == 'NOT_RUN'
    assert fitted['after']['holdout']['summary']['rmsPx'] < 1e-6
    run('multiview-batch-retains-unobserved', ['multiview-batch','--job',str(work/'multiview-case/job.json'),
                                            '--output-dir',str(work/'coverage')], ok=False)
    coverage = json.loads((work/'coverage/coverage.json').read_text(encoding='utf-8'))
    assert coverage['logicalObjectDenominator'] == 2 and not coverage['modelApplied']
    run('init', ['init', '--capture', str(work/'capture'), '--work', str(work/'job'),
                 '--length-unit', 'metre', '--up-axis', 'Z', '--coordinate-reference', 'local-synthetic'])
    run('status', ['status', '--work', str(work/'job'), '--json'])
    assert json.loads(run('next', ['next', '--work', str(work/'job'), '--json']))['executed'] is False
    assert json.loads(run('run', ['run', '--work', str(work/'job'), '--json']))['executed'] is False
    run('evaluate-draft', ['evaluate', '--work', str(work/'job')], ok=False)
    before = (work/'mcp-scene.json').read_bytes()
    patch = work/'forbidden.json'
    patch.write_text(json.dumps({'ops':[{'op':'accept','id':'wall_standalone'}]}), encoding='utf-8')
    run('patch-reject-review', ['patch','--work',str(work/'job'),'--scene',str(work/'mcp-scene.json'),
                              '--identity',str(work/'author.json'),'--actor','standalone-author',
                              '--patch',str(patch)], ok=False)
    assert (work/'mcp-scene.json').read_bytes() == before
    run('reject-self-review', ['review','--work',str(work/'job'),'--identity',str(work/'author.json'),
                             '--actor','standalone-author','--issue','not-reviewed','--verdict','PASS',
                             '--score','100','--note','Synthetic rejection test'], ok=False)
    run('reject-draft-publish', ['publish','--work',str(work/'job'),'--identity',str(work/'author.json'),
                                '--actor','standalone-author','--output',str(work/'published')], ok=False)
    assert not (work/'published').exists()
    for name in ['multiview','camera-control','multiview-batch','evidence','alignment','ifc-check']:
        run(name+'-help', [name,'--help'])
    run('benchmark', ['benchmark','--output',str(work/'benchmark.json')])
    benchmark = json.loads((work/'benchmark.json').read_text(encoding='utf-8'))
    assert benchmark['status'] == 'REVIEW' and benchmark['gates']['realCaptureRegression'] == 'NOT_RUN'
    run('smoke', ['smoke','--work',str(work/'presentation-smoke')])
    assert json.loads((work/'presentation-smoke/smoke.json').read_text(encoding='utf-8'))['syntheticSmokeSucceeded']
    payload = {'wheelSha256':hashlib.sha256(wheel.read_bytes()).hexdigest(), 'doctor':doctor,
               'checks':results, 'mcpStdioAndReadonlyReviewer':True, 'checkoutAccessDenied':True,
               'gitAndNodeRemovedFromPath':True,
               'dependencies':'reused-local-third-party-site' if args.dependency_site else 'fresh-pip-install',
               'scope':'Synthetic standalone installation; no customer data or external BIM application acceptance.'}
    (work/'standalone-install.json').write_text(json.dumps(payload, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(payload, indent=2))


if __name__ == '__main__':
    main()
