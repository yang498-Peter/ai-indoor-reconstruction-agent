"""Batch evidence retention, independent receipts, and safe output tests."""
from pathlib import Path
import copy,json,subprocess,sys
import pytest
from test_multiview_refinement import scene
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scene-core'))
from multiview_fit import (MultiviewError,sha256,load_bundle,fit_bundle,verify_review,REVIEW_POLICY_PATH)
from multiview_batch import load_job,run_batch,write_batch


def write(path,value):
    path.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8');return {'path':str(path),'sha256':sha256(path)}


def prepare(scene,*,scope='whole_object',extra=False):
    data,_,folder=scene
    case=write(folder/'case.json',data)
    logical='cuboid' if scope=='whole_object' else 'cabinet'
    required=[{'id':'cuboid','scope':scope}]
    if extra:required.append({'id':'unseen-rail','scope':'component'})
    registry={'schema':'multiview-object-registry-v1','sourceModelSha256':data['source']['modelSha256'],
              'rooms':[{'id':'room-a'},{'id':'empty-required-room'}],
              'objects':[{'id':logical,'room':'room-a','requiredTargets':required},
                         {'id':'unobserved-chair','room':None,'requiredTargets':[{'id':'unobserved-chair','scope':'whole_object'}]}]}
    job={'schema':'multiview-batch-job-v1','mappingRegistry':write(folder/'registry.json',registry),
         'sourceBindings':[{'path':str(folder/'baseline-model.json'),'sha256':data['source']['modelSha256']}],
         'cases':[{'id':'fit-one','input':case,'targets':[{'targetId':'cuboid','logicalObjectId':logical}]}]}
    write(folder/'job.json',job)
    return job,folder


def independent_review(job,folder,verdict='ACCEPT'):
    report=fit_bundle(load_bundle(Path(job['cases'][0]['input']['path'])))
    binding=write(folder/'fit-report.json',report)
    policy=sha256(REVIEW_POLICY_PATH)
    review={'schema':'multiview-refinement-review-v1','fitReportSha256':binding['sha256'],'inputSha256':report['inputSha256'],
            'reviewer':{'role':'reviewer','toolPolicy':'readonly','actorId':'independent-observer','runId':'independent-run',
                        'toolPolicyId':'reviewer-readonly-v1','toolPolicySha256':policy},
            'objects':[{'objectId':r['objectId'],'verdict':verdict,'reasons':['Independent synthetic view check'],
                        'inspectedViews':r['annotatedViews']} for r in report['objects']]}
    review_binding=write(folder/'review.json',review)
    receipt=verify_review(report,binding['sha256'],review)
    job['cases'][0]['review']={'fitReport':binding,'review':review_binding,'receipt':write(folder/'receipt.json',receipt)}
    write(folder/'job.json',job)
    return report,review,receipt


def test_real_fitting_preserves_full_denominator_and_empty_room(scene):
    _,folder=prepare(scene);result=run_batch(load_job(folder/'job.json'))
    assert result['logicalObjectDenominator']==2 and len(result['objects'])==2
    assert result['objects'][0]['status']=='REVIEW_REQUIRED'
    assert result['objects'][1]['status']=='UNOBSERVED' and result['objects'][1]['room']=='UNASSOCIATED'
    assert next(r for r in result['rooms'] if r['id']=='empty-required-room')['status']=='NO_OBJECTS'
    row=result['cases'][0]['targets'][0]
    assert row['metrics']['before']['fit']['rmsPx']>10 and row['metrics']['after']['holdout']['rmsPx']<1e-6
    assert result['wholeObjectAcceptedCount']==0 and not result['modelApplied']


def test_only_verified_independent_receipt_accepts_whole_target(scene):
    job,folder=prepare(scene);independent_review(job,folder)
    result=run_batch(load_job(folder/'job.json'))
    assert result['objects'][0]['status']=='ACCEPTED' and result['objects'][0]['wholeObjectAccepted']
    assert result['cases'][0]['reviewStatus']=='INDEPENDENT_RECEIPT_VERIFIED'
    assert result['wholeObjectAcceptedCount']==1 and result['logicalObjectDenominator']==2


@pytest.mark.parametrize('extra',[False,True])
def test_component_acceptance_never_accepts_whole_object(scene,extra):
    job,folder=prepare(scene,scope='component',extra=extra);independent_review(job,folder)
    obj=run_batch(load_job(folder/'job.json'))['objects'][0]
    assert obj['acceptedComponents']==1 and not obj['wholeObjectAccepted']
    assert obj['status']==('INSUFFICIENT' if extra else 'REVIEW_REQUIRED')
    assert ('REQUIRED_TARGETS_UNOBSERVED' if extra else 'PARTIAL_COMPONENT_SCOPE') in obj['reasons']


def test_no_annotations_is_insufficient_not_retrieval_fit(scene):
    data,_,folder=scene
    for v in data['views']:v['annotations']=None
    _,folder=prepare(scene)
    result=run_batch(load_job(folder/'job.json'))
    assert result['objects'][0]['status']=='INSUFFICIENT'
    assert result['cases'][0]['targets'][0]['fitStatus']=='INSUFFICIENT_EVIDENCE'


def test_stale_case_and_malformed_case_both_remain_rejected(scene):
    job,folder=prepare(scene)
    malformed=folder/'malformed.json';malformed.write_text('{broken',encoding='utf-8')
    job['cases'].append({'id':'malformed','input':{'path':str(malformed),'sha256':sha256(malformed)},'targets':copy.deepcopy(job['cases'][0]['targets'])})
    job['cases'][0]['input']['sha256']='0'*64;write(folder/'job.json',job)
    report=run_batch(load_job(folder/'job.json'))
    assert len(report['cases'])==2 and all(c['status']=='REJECTED' for c in report['cases'])
    assert report['objects'][0]['status']=='REJECTED' and report['wholeObjectAcceptedCount']==0
    assert all(c['error']['code'] for c in report['cases'])


def test_bad_attempt_is_not_washed_away_by_later_accepted_case(scene):
    job,folder=prepare(scene);independent_review(job,folder)
    bad=copy.deepcopy(job['cases'][0]);bad['id']='failed-prior-attempt';bad['input']['sha256']='0'*64
    job['cases'].insert(0,bad);write(folder/'job.json',job)
    result=run_batch(load_job(folder/'job.json'))
    assert result['objects'][0]['status']=='REJECTED'
    assert len(result['objects'][0]['targets'][0]['caseIds'])==2


@pytest.mark.parametrize('problem',['receipt','self-review','stale-producer','different-score'])
def test_forged_or_stale_review_cannot_green_batch(scene,problem):
    job,folder=prepare(scene);report,review,receipt=independent_review(job,folder)
    if problem=='receipt':
        receipt['acceptedObjectIds']=[];job['cases'][0]['review']['receipt']=write(folder/'receipt.json',receipt)
    elif problem=='self-review':
        review['reviewer']['actorId']=report['author']['actorId'];job['cases'][0]['review']['review']=write(folder/'review.json',review)
    else:
        if problem=='stale-producer':report['provenance']['producerSha256']['scene-core/multiview_fit.py']='0'*64
        else:report['objects'][0]['after']['fit']['summary']['rmsPx']=.123
        job['cases'][0]['review']['fitReport']=write(folder/'fit-report.json',report)
    write(folder/'job.json',job)
    result=run_batch(load_job(folder/'job.json'))
    assert result['objects'][0]['status']=='REJECTED' and result['wholeObjectAcceptedCount']==0


def test_registry_model_must_equal_case_model(scene):
    job,folder=prepare(scene)
    other=folder/'other-model.json';other.write_text('new model',encoding='utf-8')
    reg=json.loads((folder/'registry.json').read_text(encoding='utf-8'));reg['sourceModelSha256']=sha256(other)
    job['mappingRegistry']=write(folder/'registry.json',reg);job['sourceBindings']=[{'path':str(other),'sha256':sha256(other)}]
    write(folder/'job.json',job)
    report=run_batch(load_job(folder/'job.json'))
    assert report['cases'][0]['error']['code']=='CASE_REGISTRY_MODEL_MISMATCH'


def test_mapping_must_include_every_case_target(scene):
    data,_,folder=scene
    extra=copy.deepcopy(data['objects'][0]);extra['id']='other';data['objects'].append(extra)
    _,folder=prepare(scene)
    report=run_batch(load_job(folder/'job.json'))
    assert report['cases'][0]['error']['code']=='CASE_TARGET_MAPPING_INCOMPLETE'


def test_changed_registry_fails_before_trusting_denominator(scene):
    _,folder=prepare(scene);(folder/'registry.json').write_text('{}',encoding='utf-8')
    with pytest.raises(MultiviewError,match='BATCH_BINDING_CHANGED'):load_job(folder/'job.json')


def test_source_changes_during_run_abort_publication(scene,monkeypatch):
    _,folder=prepare(scene);job=load_job(folder/'job.json')
    import multiview_batch as batch
    original=batch.fit_bundle
    def mutate(bundle):
        result=original(bundle);(folder/'baseline-model.json').write_text('changed',encoding='utf-8');return result
    monkeypatch.setattr(batch,'fit_bundle',mutate)
    with pytest.raises(MultiviewError,match='BATCH_INPUT_CHANGED_DURING_RUN'):write_batch(job,folder/'out')
    assert not (folder/'out').exists()


def test_reports_have_no_source_paths_and_fresh_directory_is_atomic(scene):
    _,folder=prepare(scene);job=load_job(folder/'job.json');out=folder/'coverage'
    write_batch(job,out)
    for name in ('coverage.json','coverage.md','index.html'):
        text=(out/name).read_text(encoding='utf-8')
        assert str(folder) not in text and str(folder).replace('\\','\\\\') not in text
    assert len(list(out.iterdir()))==3
    with pytest.raises(MultiviewError,match='OUTPUT_EXISTS'):write_batch(job,out)


@pytest.mark.parametrize('source',['job.json','view-0.png','registry.json'])
def test_cli_cannot_overwrite_any_input(scene,source):
    _,folder=prepare(scene);p=folder/source;before=p.read_bytes()
    result=subprocess.run([sys.executable,str(ROOT/'scripts/multiview_batch.py'),'--job',str(folder/'job.json'),'--output-dir',str(p)],capture_output=True,text=True)
    assert result.returncode==2 and 'OUTPUT_OVERWRITES_INPUT' in result.stderr
    assert p.read_bytes()==before


def test_output_directory_cannot_contain_bound_inputs(scene):
    _,folder=prepare(scene);job=load_job(folder/'job.json')
    with pytest.raises(MultiviewError,match='OUTPUT_OVERWRITES_INPUT'):write_batch(job,folder)


def test_sources_are_rechecked_at_atomic_publication(scene,monkeypatch):
    _,folder=prepare(scene);job=load_job(folder/'job.json')
    import multiview_batch as batch
    original=batch.atomic_json
    def mutate(path,value):
        original(path,value);(folder/'case.json').write_text('changed during write',encoding='utf-8')
    monkeypatch.setattr(batch,'atomic_json',mutate)
    with pytest.raises(MultiviewError,match='BATCH_INPUT_CHANGED_DURING_RUN'):write_batch(job,folder/'out')
    assert not (folder/'out').exists() and not list(folder.glob('.multiview-batch-*'))


def test_atomic_directory_publish_refuses_empty_destination_race(scene,monkeypatch):
    _,folder=prepare(scene);job=load_job(folder/'job.json')
    import multiview_batch as batch
    original=batch.publish_directory
    def race(staging,output):
        output.mkdir();original(staging,output)
    monkeypatch.setattr(batch,'publish_directory',race)
    with pytest.raises((MultiviewError,FileExistsError,PermissionError)):write_batch(job,folder/'out')
    assert (folder/'out').is_dir() and not list((folder/'out').iterdir())
    assert not list(folder.glob('.multiview-batch-*'))


def test_missing_source_and_unknown_mapping_are_not_green(scene):
    job,folder=prepare(scene);(folder/'baseline-model.json').unlink()
    report=run_batch(load_job(folder/'job.json'))
    assert not report['sourceBindingsVerified'] and report['sourceError']=='BATCH_BINDING_CHANGED'
    assert report['objects'][0]['status']=='REJECTED'
    job['cases'][0]['targets'][0]['logicalObjectId']='ghost';write(folder/'job.json',job)
    with pytest.raises(MultiviewError,match='INVALID_CASE_TARGET_MAPPING'):load_job(folder/'job.json')


def test_cli_keeps_unobserved_objects_and_returns_nonzero(scene):
    _,folder=prepare(scene)
    result=subprocess.run([sys.executable,str(ROOT/'scripts/multiview_batch.py'),'--job',str(folder/'job.json'),'--output-dir',str(folder/'out')],capture_output=True,text=True)
    assert result.returncode==1
    assert json.loads(result.stdout)['logicalObjectDenominator']==2
    assert 'unobserved-chair' in (folder/'out/coverage.md').read_text(encoding='utf-8')
