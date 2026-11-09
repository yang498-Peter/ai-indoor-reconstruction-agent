"""Frozen-denominator batch fitting and coverage, without model mutation.

Coverage artifacts omit source paths. They are not substitutes for the complete
fit report and independently authored review/receipt accepted by verify_review.
"""
from pathlib import Path
import collections,ctypes,errno,html,json,os,shutil,sys,tempfile
from multiview_fit import (MultiviewError,_read_json,_fail,sha256,canonical_hash,
    load_bundle,fit_bundle,verify_bindings,verify_review,verify_producer_binding,
    producer_binding,protect_output,atomic_json)


def _id(value):
    _fail(isinstance(value,str) and bool(value.strip()) and len(value)<=240
          and not any(c in value for c in '\r\n\\:') and not value.startswith('/'), 'INVALID_BATCH_ID')
    return value


def _binding(value,base):
    _fail(isinstance(value,dict) and isinstance(value.get('path'),str)
          and isinstance(value.get('sha256'),str) and len(value['sha256'])==64,'INVALID_BATCH_BINDING')
    _fail(all(c in '0123456789abcdef' for c in value['sha256']),'INVALID_BATCH_BINDING')
    path=(base/value['path']).resolve()
    return {'path':path,'sha256':value['sha256']}


def _verify(binding):
    _fail(binding['path'].is_file() and sha256(binding['path'])==binding['sha256'],'BATCH_BINDING_CHANGED')


def _paths_in_json(value,base):
    """Protect referenced inputs even when a case fails later schema validation."""
    result=[]
    if isinstance(value,dict):
        if isinstance(value.get('path'),str):result.append((base/value['path']).resolve())
        for item in value.values():result.extend(_paths_in_json(item,base))
    elif isinstance(value,list):
        for item in value:result.extend(_paths_in_json(item,base))
    return result


def batch_producer_binding():
    result=producer_binding()
    root=Path(__file__).resolve().parents[1]
    result['batchProducerSha256']={name:sha256(root/name) for name in
        ('scene-core/multiview_batch.py','scripts/multiview_batch.py')}
    return result


def load_job(path):
    path=Path(path).resolve();data=_read_json(path);base=path.parent
    _fail(data.get('schema')=='multiview-batch-job-v1','INVALID_BATCH_SCHEMA')
    registry_binding=_binding(data.get('mappingRegistry'),base);_verify(registry_binding)
    registry=_read_json(registry_binding['path'])
    _fail(registry.get('schema')=='multiview-object-registry-v1','INVALID_BATCH_REGISTRY')
    model=registry.get('sourceModelSha256')
    _fail(isinstance(model,str) and len(model)==64,'MISSING_REGISTRY_MODEL_BINDING')
    rooms=registry.get('rooms');objects=registry.get('objects')
    _fail(isinstance(rooms,list) and isinstance(objects,list),'INVALID_BATCH_DENOMINATOR')
    room_ids=[_id(r.get('id')) for r in rooms]
    _fail(len(set(room_ids))==len(room_ids) and 'UNASSOCIATED' not in room_ids,'DUPLICATE_BATCH_ROOM')
    by_object={};target_owner={}
    for obj in objects:
        oid=_id(obj.get('id'));room=obj.get('room')
        _fail(oid not in by_object,'DUPLICATE_BATCH_OBJECT')
        _fail('room' in obj and (room is None or room in room_ids),'UNKNOWN_BATCH_ROOM')
        required=obj.get('requiredTargets')
        _fail(isinstance(required,list) and bool(required),'MISSING_REQUIRED_TARGETS')
        for target in required:
            tid=_id(target.get('id'))
            _fail(tid not in target_owner,'DUPLICATE_BATCH_TARGET')
            _fail(target.get('scope') in ('component','whole_object'),'INVALID_BATCH_TARGET_SCOPE')
            _fail(target['scope']!='whole_object' or (len(required)==1 and tid==oid),'INVALID_WHOLE_OBJECT_SCOPE')
            target_owner[tid]=oid
        by_object[oid]=obj
    raw_cases=data.get('cases');_fail(isinstance(raw_cases,list),'INVALID_BATCH_CASES')
    cases=[];case_ids=set();protected=[path,registry_binding['path']]
    source=[_binding(v,base) for v in data.get('sourceBindings',[])]
    _fail(source and any(v['sha256']==model for v in source),'REGISTRY_MODEL_BYTES_UNBOUND')
    protected.extend(v['path'] for v in source)
    for raw in raw_cases:
        cid=_id(raw.get('id'));_fail(cid not in case_ids,'DUPLICATE_BATCH_CASE');case_ids.add(cid)
        inp=_binding(raw.get('input'),base);mapping=raw.get('targets')
        _fail(isinstance(mapping,list) and bool(mapping),'MISSING_CASE_TARGET_MAPPING')
        mapped={}
        for item in mapping:
            tid=_id(item.get('targetId'));oid=_id(item.get('logicalObjectId'))
            _fail(tid not in mapped and target_owner.get(tid)==oid,'INVALID_CASE_TARGET_MAPPING')
            mapped[tid]=oid
        review=raw.get('review');review_bindings=None
        if review is not None:
            _fail(isinstance(review,dict),'INVALID_BATCH_REVIEW')
            review_bindings={key:_binding(review.get(key),base) for key in ('fitReport','review','receipt')}
            protected.extend(v['path'] for v in review_bindings.values())
        protected.append(inp['path'])
        for p in [inp['path']]+([v['path'] for v in review_bindings.values()] if review_bindings else []):
            if p.is_file():
                try:protected.extend(_paths_in_json(_read_json(p),p.parent))
                except (MultiviewError,ValueError,TypeError,OSError):pass
        cases.append({'id':cid,'input':inp,'mapping':mapped,'review':review_bindings})
    # Freeze existing referenced bytes, including bad inputs, to detect races.
    frozen={p:sha256(p) if p.is_file() else None for p in set(protected)}
    return {'path':path,'data':data,'registry':registry,'registryBinding':registry_binding,
            'objects':by_object,'rooms':room_ids,'cases':cases,'sourceBindings':source,
            'protected':protected,'frozen':frozen,'jobSha256':sha256(path)}


def _states(rows):
    if not rows:return 'UNOBSERVED'
    states=[r['status'] for r in rows]
    for state in ('REJECTED','INSUFFICIENT','REVIEW_REQUIRED'):
        if state in states:return state
    return 'ACCEPTED' if all(s=='ACCEPTED' for s in states) else 'UNOBSERVED'


def verify_frozen_inputs(job):
    for p,digest in job['frozen'].items():
        _fail((sha256(p) if p.is_file() else None)==digest,'BATCH_INPUT_CHANGED_DURING_RUN')


def publish_directory(staging,output):
    """Atomic rename with no replacement, including an empty-directory race."""
    if os.name=='nt':
        os.rename(staging,output)  # Windows refuses any existing destination.
        return
    libc=ctypes.CDLL(None,use_errno=True)
    if sys.platform.startswith('linux') and hasattr(libc,'renameat2'):
        call=libc.renameat2
        call.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
        result=call(-100,os.fsencode(staging),-100,os.fsencode(output),1)  # RENAME_NOREPLACE
    elif sys.platform=='darwin' and hasattr(libc,'renamex_np'):
        call=libc.renamex_np;call.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_uint]
        result=call(os.fsencode(staging),os.fsencode(output),4)  # RENAME_EXCL
    else:
        _fail(False,'ATOMIC_DIRECTORY_NOREPLACE_UNSUPPORTED')
    if result:
        error=ctypes.get_errno()
        _fail(error not in (errno.EEXIST,errno.ENOTEMPTY),'OUTPUT_EXISTS')
        raise OSError(error,os.strerror(error))


def run_batch(job):
    provenance=batch_producer_binding();cases=[];attempts=collections.defaultdict(list)
    source_error=None
    try:
        for b in job['sourceBindings']:_verify(b)
    except (MultiviewError,OSError) as exc:source_error=getattr(exc,'code','BATCH_SOURCE_IO_ERROR')
    for spec in job['cases']:
        entry={'caseId':spec['id'],'inputSha256':spec['input']['sha256'],'targets':[],
               'status':'REJECTED','reviewStatus':'NOT_RUN'}
        try:
            _fail(source_error is None,source_error or 'BATCH_SOURCE_BINDING_ERROR')
            _verify(spec['input']);bundle=load_bundle(spec['input']['path'])
            _fail(bundle['data']['source']['modelSha256']==job['registry']['sourceModelSha256'],'CASE_REGISTRY_MODEL_MISMATCH')
            _fail(set(bundle['objects'])==set(spec['mapping']),'CASE_TARGET_MAPPING_INCOMPLETE')
            for tid,obj in bundle['objects'].items():
                declared_room=job['objects'][spec['mapping'][tid]]['room']
                _fail(obj['raw'].get('room')==declared_room,'CASE_REGISTRY_ROOM_MISMATCH')
            fresh=fit_bundle(bundle);entry['fitReportContentSha256']=canonical_hash(fresh)
            entry['provenance']=fresh['provenance']
            verdicts={};accepted=set()
            if spec['review']:
                for b in spec['review'].values():_verify(b)
                report=_read_json(spec['review']['fitReport']['path'])
                review=_read_json(spec['review']['review']['path'])
                receipt=_read_json(spec['review']['receipt']['path'])
                verify_producer_binding(report)
                _fail(canonical_hash(report)==canonical_hash(fresh),'BATCH_FRESH_FIT_REPORT_MISMATCH')
                actual=verify_review(report,spec['review']['fitReport']['sha256'],review)
                _fail(canonical_hash(receipt)==canonical_hash(actual),'BATCH_REVIEW_RECEIPT_MISMATCH')
                accepted=set(actual['acceptedObjectIds'])
                verdicts={v['objectId']:v['verdict'] for v in review['objects']}
                entry['reviewStatus']='INDEPENDENT_RECEIPT_VERIFIED'
                entry['reviewBindingSha256']={k:v['sha256'] for k,v in spec['review'].items()}
            for row in fresh['objects']:
                tid=row['objectId'];state={'INSUFFICIENT_EVIDENCE':'INSUFFICIENT',
                    'CANDIDATE_REVIEW_REQUIRED':'REVIEW_REQUIRED','UNCHANGED_REVIEW_REQUIRED':'REVIEW_REQUIRED'}.get(row['status'],'REJECTED')
                reasons=list(row.get('reasons',[]))
                if tid in accepted:state='ACCEPTED'
                elif verdicts.get(tid)=='REJECT':state='REJECTED';reasons.append('INDEPENDENT_REVIEW_REJECTED')
                elif verdicts.get(tid)=='INSUFFICIENT_EVIDENCE':state='INSUFFICIENT';reasons.append('INDEPENDENT_REVIEW_INSUFFICIENT')
                # Keep numerical evidence; source paths and free-form source text
                # never enter the coverage payload.
                target={'targetId':tid,'logicalObjectId':spec['mapping'][tid],'caseId':spec['id'],
                        'status':state,'fitStatus':row['status'],'reasons':reasons,
                        'fitViews':len(row.get('fitViewIds',[])),'holdoutViews':len(row.get('holdoutViewIds',[])),
                        'metrics':{k:{split:row[k][split]['summary'] for split in ('fit','holdout')}
                                   for k in ('before','after') if k in row},
                        'candidate':row.get('candidate'),'parameterChange':row.get('parameterChange'),
                        'solver':row.get('solver')}
                entry['targets'].append(target);attempts[tid].append(target)
            verify_bindings(bundle);_verify(spec['input'])
            if spec['review']:
                for b in spec['review'].values():_verify(b)
            entry['status']=_states(entry['targets'])
        except Exception as exc:
            # A broken case remains visible for every declared mapped target.
            # No exception message is exposed: messages may contain raw paths.
            code=getattr(exc,'code','BATCH_CASE_EXECUTION_ERROR')
            entry['error']={'code':code,'exceptionType':type(exc).__name__}
            for prior in entry['targets']:
                attempts[prior['targetId']].remove(prior)
            entry['targets']=[]
            for tid,oid in spec['mapping'].items():
                target={'caseId':spec['id'],'targetId':tid,'logicalObjectId':oid,'status':'REJECTED','reasons':[code]}
                entry['targets'].append(target);attempts[tid].append(target)
            entry['status']='REJECTED';entry['reviewStatus']='INVALID_OR_NOT_RUN'
        cases.append(entry)
    objects=[]
    for oid,raw in job['objects'].items():
        targets=[]
        for target in raw['requiredTargets']:
            rows=attempts[target['id']];state=_states(rows)
            targets.append({'id':target['id'],'scope':target['scope'],'status':state,
                            'caseIds':[r['caseId'] for r in rows],
                            'reasons':sorted({v for r in rows for v in r.get('reasons',[])}) if rows else ['NO_DECLARED_OBSERVATION_CASE']})
        states=[t['status'] for t in targets];state=_states(targets);reasons=[]
        if 'UNOBSERVED' in states and any(s!='UNOBSERVED' for s in states) and state!='REJECTED':state='INSUFFICIENT';reasons.append('REQUIRED_TARGETS_UNOBSERVED')
        whole=state=='ACCEPTED' and len(targets)==1 and targets[0]['scope']=='whole_object'
        if state=='ACCEPTED' and not whole:state='REVIEW_REQUIRED';reasons.append('PARTIAL_COMPONENT_SCOPE')
        objects.append({'id':oid,'room':raw['room'] if raw['room'] is not None else 'UNASSOCIATED',
                        'status':state,'wholeObjectAccepted':whole,'acceptedComponents':sum(t['status']=='ACCEPTED' for t in targets if t['scope']=='component'),
                        'requiredTargetCount':len(targets),'observedTargetCount':sum(t['status']!='UNOBSERVED' for t in targets),
                        'nextAction':{'UNOBSERVED':'COLLECT_INDEPENDENT_IMAGE_OBSERVATIONS','INSUFFICIENT':'FILL_MISSING_EVIDENCE',
                          'REJECTED':'RESOLVE_REJECTION_WITHOUT_HOLDOUT_TUNING','REVIEW_REQUIRED':'INDEPENDENT_REVIEW_OR_REMAINING_COMPONENTS',
                          'ACCEPTED':'DECLARED_SCOPE_REVIEW_RECORDED'}[state],
                        'targets':targets,'reasons':reasons})
    rooms=[]
    for rid in job['rooms']+(['UNASSOCIATED'] if any(o['room']=='UNASSOCIATED' for o in objects) else []):
        members=[o for o in objects if o['room']==rid]
        rooms.append({'id':rid,'objectCount':len(members),'status':_states(members) if members else 'NO_OBJECTS',
                      'counts':dict(collections.Counter(o['status'] for o in members))})
    _fail(batch_producer_binding()==provenance,'BATCH_PRODUCER_CHANGED_DURING_RUN')
    verify_frozen_inputs(job)
    return {'schema':'multiview-batch-coverage-v1','jobSha256':job['jobSha256'],
            'registrySha256':job['registryBinding']['sha256'],'sourceModelSha256':job['registry']['sourceModelSha256'],
            'provenance':provenance,'sourceBindingsVerified':source_error is None,'sourceError':source_error,
            'status':'REVIEW_REQUIRED','wholeObjectAcceptedCount':sum(o['wholeObjectAccepted'] for o in objects),
            'logicalObjectDenominator':len(objects),'requiredRoomDenominator':len(job['rooms']),
            'counts':dict(collections.Counter(o['status'] for o in objects)),
            'rooms':rooms,'objects':objects,'cases':cases,'modelApplied':False,
            'limitations':['Coverage reports omit source paths and cannot replace source-bound full fit reports.',
              'Retrieval candidates are not observations. Unmapped or unobserved objects remain in the denominator.',
              'ACCEPTED refers to declared numerical/review evidence, never authority or survey acceptance.',
              'Accepting a component does not accept its whole logical object. Rejected attempts remain blocking.']}


def write_batch(job,output):
    output=Path(output).resolve();protect_output(output,job['protected'],directory=True)
    _fail(not output.exists(),'OUTPUT_EXISTS');output.parent.mkdir(parents=True,exist_ok=True)
    report=run_batch(job);staging=Path(tempfile.mkdtemp(prefix='.multiview-batch-',dir=output.parent))
    try:
        atomic_json(staging/'coverage.json',report)
        lines=['# Multi-view coverage','',f"Logical objects: {report['logicalObjectDenominator']}; whole objects accepted: {report['wholeObjectAcceptedCount']}.",'',
               'No model was applied. Source paths are omitted. Component acceptance does not accept a whole object.','',
               '| Room | Objects | Status |','| --- | ---: | --- |']
        escape=lambda s:str(s).replace('|','\\|').replace('<','&lt;').replace('>','&gt;')
        lines.extend(f"| {escape(r['id'])} | {r['objectCount']} | {r['status']} |" for r in report['rooms'])
        lines+=['','| Object | Room | Status | Accepted components |','| --- | --- | --- | ---: |']
        lines.extend(f"| {escape(o['id'])} | {escape(o['room'])} | {o['status']} | {o['acceptedComponents']} |" for o in report['objects'])
        (staging/'coverage.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
        rows=''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in (o['id'],o['room'],o['status'],o['acceptedComponents']))+'</tr>' for o in report['objects'])
        room_rows=''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in (r['id'],r['objectCount'],r['status']))+'</tr>' for r in report['rooms'])
        page='<!doctype html><html lang="en"><meta charset="utf-8"><title>Multi-view coverage</title><style>body{font:15px system-ui;margin:32px;max-width:1400px;color:#25332d}table{border-collapse:collapse;width:100%}td,th{padding:9px;border-bottom:1px solid #ccc;text-align:left}th{background:#edf2ef}</style><h1>Multi-view coverage</h1><p>'+html.escape(lines[2])+'</p><p>No model was applied. All required objects remain listed. Source paths are omitted; component acceptance does not accept a whole object.</p><table><tr><th>Room</th><th>Objects</th><th>Status</th></tr>'+room_rows+'</table><h2>Objects</h2><table><tr><th>Object</th><th>Room</th><th>Status</th><th>Accepted components</th></tr>'+rows+'</table></html>'
        (staging/'index.html').write_text(page,encoding='utf-8')
        verify_frozen_inputs(job)
        _fail(batch_producer_binding()==report['provenance'],'BATCH_PRODUCER_CHANGED_DURING_RUN')
        _fail(not output.exists(),'OUTPUT_EXISTS');publish_directory(staging,output)
    finally:
        if staging.exists():
            _fail(staging.resolve().parent==output.parent and staging.name.startswith('.multiview-batch-'),'INVALID_STAGING_CLEANUP')
            shutil.rmtree(staging)
    return report
