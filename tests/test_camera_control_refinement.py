"""Known rigid-pose recovery and independent fixed-control rejection tests."""
from pathlib import Path
import copy,json,subprocess,sys
import numpy as np
from PIL import Image
import pytest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scene-core'))
from photo_projection import Frame,CameraModel,project_points
from multiview_fit import MultiviewError,sha256,REVIEW_POLICY_PATH
from camera_control_fit import (updated_pose,validate_camera_case,check_or_fit_camera,
                                load_camera_case,verify_camera_review,_geometry)


def look_at(center,target=(0,0,1)):
    center=np.array(center,dtype=float);z=center-np.array(target);z/=np.linalg.norm(z)
    x=np.cross([0,0,1],z);x/=np.linalg.norm(x);y=np.cross(z,x)
    m=np.eye(4);m[:3,:3]=np.column_stack([x,y,z]);m[:3,3]=center;return m


@pytest.fixture
def camera_case(tmp_path):
    truth=look_at([4,-6,3]);original=updated_pose(truth,np.array([.1,-.06,.04,.025,-.02,.015]))
    camera={'width':800,'height':600,'fx':600.,'fy':600.,'cx':400.,'cy':300.,'model':'pinhole'}
    path=tmp_path/'source-image.png';Image.new('RGB',(800,600),(50,65,80)).save(path)
    pose=tmp_path/'pose.json';pose.write_text(json.dumps({'c2w':original.tolist(),'camera':camera}),encoding='utf-8')
    xyzs=[[-1,-1,.2],[1,-1,.2],[-1,1,.2],[1,1,1.8],[-1,-.5,1.8],[.5,0,1.2],[.4,.8,.4],[-.5,.3,1.5]]
    features=[{'id':f'junction-{i}','xyz':xyz,'uncertaintyM':.017+i*.0008} for i,xyz in enumerate(xyzs)]
    artifact=tmp_path/'source-controls.json';artifact.write_text(json.dumps({'sourceCloudSha256':'b'*64,'controls':features}),encoding='utf-8')
    projection,_,_=project_points(Frame('frame-1','',truth,CameraModel(**camera)),np.array(xyzs))
    marks=[{'id':f['id'],'split':'fit' if i<6 else 'holdout','kind':'static_architecture_junction','xyz':f['xyz'],
            'uncertaintyM':f['uncertaintyM'],'pixels':projection[i].tolist(),'uncertaintyPx':.5,
            'provenance':{'annotator':'fit-reader' if i<6 else 'holdout-reader','sourceImageSha256':sha256(path),
                          'sourceCloudSha256':'b'*64,'featureDescription':'Synthetic fixed static architecture junction','derivedFromTargetObject':False}}
           for i,f in enumerate(features)]
    data={'schema':'camera-control-refinement-input-v1','units':'metre','upAxis':'Z',
          'source':{'coordinateFrame':'synthetic metre Z-up','sourceCloudSha256':'b'*64,'poseSourceSha256':sha256(pose),'calibrationSourceSha256':sha256(pose)},
          'sourceBindings':[{'path':pose.name,'sha256':sha256(pose)},{'path':artifact.name,'sha256':sha256(artifact)}],
          'sourceEvidenceBinding':{'path':artifact.name,'sha256':sha256(artifact)},
          'author':{'actorId':'camera-author','runId':'camera-author-run'},
          'frame':{'id':'frame-1','image':{'path':path.name,'sha256':sha256(path)},'camera':camera,'c2w':original.tolist()},
          'controls':marks,'bounds':{'translationM':[.4,.4,.4],'rotationRad':[.12,.12,.12]},
          'policy':{'minFitControls':4,'minHoldoutControls':2,'minHoldoutSeparationM':.1,'maxNormalizedRms':2.,
                    'maxPerControlNormalizedError':3.,'maxHoldoutWorseningNormalized':.1,'minImprovementNormalized':.01,
                    'lossScale':1.,'maxNfev':300,'conditionLimit':1e7,'minSpatialSingularRatio':.02}}
    return data,truth,tmp_path


def test_known_se3_recovery_and_uncertainty_scope(camera_case):
    data,truth,path=camera_case;original=copy.deepcopy(data);case=validate_camera_case(data,path)
    report=check_or_fit_camera(case)
    assert report['status']=='LOCAL_POSE_CANDIDATE_REVIEW_REQUIRED',report['reasons']
    np.testing.assert_allclose(report['candidateC2w'],truth,atol=1e-7)
    assert report['after']['fit']['rmsPx']<1e-6 and report['after']['holdout']['rmsPx']<1e-6
    assert report['before']['fit']['rmsPx']>10
    assert report['cameraVerified'] is False and report['independentReviewStatus']=='NOT_RUN'
    assert report['sourceLineage']['rawCloudBytesRehashed'] is False
    assert report['intrinsics']==original['frame']['camera'] and data==original
    np.testing.assert_allclose([r['sourceUncertaintyM'][0] for r in report['before']['fit']['controls']], [.017+i*.0008 for i in range(6)])


def test_holdout_contradiction_does_not_change_camera_solution(camera_case):
    data,_,path=camera_case;first=check_or_fit_camera(validate_camera_case(data,path))
    for mark in data['controls']:
        if mark['split']=='holdout':mark['pixels'][0]+=45
    second=check_or_fit_camera(validate_camera_case(data,path))
    np.testing.assert_allclose(first['candidateC2w'],second['candidateC2w'],atol=1e-12)
    assert second['status']=='REJECTED' and 'HOLDOUT_ERROR_EXCEEDS_STATED_UNCERTAINTY' in second['reasons']


def test_correct_original_camera_is_retained_without_forced_optimization(camera_case):
    data,truth,path=camera_case;data['frame']['c2w']=truth.tolist()
    report=check_or_fit_camera(validate_camera_case(data,path))
    assert report['status']=='UNCHANGED_WITHIN_STATED_UNCERTAINTY_REVIEW_REQUIRED'
    assert report['solver']['optimized'] is False and report['translationMagnitudeM']==0
    np.testing.assert_array_equal(report['originalC2w'],report['candidateC2w'])


def test_check_only_reports_error_without_adjusting_camera(camera_case):
    data,_,path=camera_case;report=check_or_fit_camera(validate_camera_case(data,path),optimize=False)
    assert report['status']=='REJECTED' and not report['solver']['optimized']
    np.testing.assert_array_equal(report['originalC2w'],report['candidateC2w'])


def test_source_features_field_mapping_preserves_fixed_measurements(camera_case):
    data,_,path=camera_case;p=path/'source-controls.json';artifact=json.loads(p.read_text(encoding='utf-8'))
    artifact['features']=[{'id':c['id'],'sourceXYZ':c['xyz'],'sourceLocalizationUncertaintyM':c['uncertaintyM']} for c in artifact.pop('controls')]
    p.write_text(json.dumps(artifact),encoding='utf-8')
    data['sourceBindings'][1]['sha256']=sha256(p);data['sourceEvidenceBinding']['sha256']=sha256(p)
    data['sourceEvidenceMapping']={'items':'features','position':'sourceXYZ','uncertainty':'sourceLocalizationUncertaintyM'}
    report=check_or_fit_camera(validate_camera_case(data,path));assert report['status']=='LOCAL_POSE_CANDIDATE_REVIEW_REQUIRED'
    assert report['sourceLineage']['mapping']==data['sourceEvidenceMapping']
    data['controls'][0]['xyz'][0]+=.01
    with pytest.raises(MultiviewError,match='SOURCE_CONTROL_WAS_CHANGED'):validate_camera_case(data,path)


@pytest.mark.parametrize('damage',['duplicate_id','duplicate_position','bad_determinant','no_controls','collinear','coplanar','target_object','stale_image'])
def test_invalid_and_ambiguous_control_sets_fail_closed(camera_case,damage):
    data,_,path=camera_case
    if damage=='duplicate_id':data['controls'][1]['id']=data['controls'][0]['id']
    elif damage=='duplicate_position':
        data['controls'][1]['xyz']=copy.deepcopy(data['controls'][0]['xyz'])
        p=path/'source-controls.json';artifact=json.loads(p.read_text(encoding='utf-8'));artifact['controls'][1]['xyz']=copy.deepcopy(data['controls'][0]['xyz']);p.write_text(json.dumps(artifact),encoding='utf-8')
        data['sourceBindings'][1]['sha256']=sha256(p);data['sourceEvidenceBinding']['sha256']=sha256(p)
    elif damage=='bad_determinant':data['frame']['c2w'][0][0]*=-1
    elif damage=='target_object':data['controls'][0]['provenance']['derivedFromTargetObject']=True
    elif damage=='stale_image':(path/'source-image.png').write_bytes(b'changed')
    elif damage=='no_controls':data['controls']=[]
    else:
        p=path/'source-controls.json';artifact=json.loads(p.read_text(encoding='utf-8'))
        for i in range(6):
            xyz=[-.8+i*.2,0,1] if damage=='collinear' else [(-1 if i%2 else 1),-.8+(i//2)*.5,1]
            artifact['controls'][i]['xyz']=xyz;data['controls'][i]['xyz']=xyz
        p.write_text(json.dumps(artifact),encoding='utf-8');data['sourceBindings'][1]['sha256']=sha256(p);data['sourceEvidenceBinding']['sha256']=sha256(p)
    if damage in ('no_controls','collinear','coplanar'):
        report=check_or_fit_camera(validate_camera_case(data,path));assert report['status']=='REJECTED'
        assert 'COLLINEAR_OR_COPLANAR_CONTROL_AMBIGUITY' in report['reasons']
    else:
        with pytest.raises(MultiviewError):validate_camera_case(data,path)


def test_fisheye_known_pose_recovery(camera_case):
    data,truth,path=camera_case;data['frame']['camera'].update({'model':'fisheye-equidistant','k':[.1,-.02,.003,0]})
    frame=Frame('truth','',truth,CameraModel(**data['frame']['camera']))
    uv,_,_=project_points(frame,np.array([c['xyz'] for c in data['controls']]))
    for control,pixel in zip(data['controls'],uv):control['pixels']=pixel.tolist()
    report=check_or_fit_camera(validate_camera_case(data,path));assert report['status']=='LOCAL_POSE_CANDIDATE_REVIEW_REQUIRED',report['reasons']
    np.testing.assert_allclose(report['candidateC2w'],truth,atol=1e-7)


def test_pose_at_bound_is_withheld_even_if_residual_reduces(camera_case):
    data,_,path=camera_case;data['bounds']['translationM']=[.001,.001,.001]
    report=check_or_fit_camera(validate_camera_case(data,path))
    assert report['status']=='REJECTED' and 'POSE_BOUND_REACHED' in report['reasons']


def test_pixel_uncertainty_cannot_be_inflated_by_candidate_depth(camera_case):
    data,_,path=camera_case;case=validate_camera_case(data,path);report=check_or_fit_camera(case)
    before=report['before']['fit']['controls'];after=report['after']['fit']['controls']
    assert [c['frozenTotalPixelSigma'] for c in before]==[c['frozenTotalPixelSigma'] for c in after]
    assert any(abs(a['depthM']-b['depthM'])>.01 for a,b in zip(before,after))


def test_independent_review_binds_exact_frame_and_uncertainty_scope(camera_case):
    data,_,path=camera_case;report=check_or_fit_camera(validate_camera_case(data,path))
    review={'schema':'camera-control-refinement-review-v1','reviewer':{'actorId':'reviewer','runId':'review-run','role':'reviewer','toolPolicy':'readonly',
            'toolPolicyId':'reviewer-readonly-v1','toolPolicySha256':sha256(REVIEW_POLICY_PATH)},
            'reportSha256':'f'*64,'inputSha256':report['inputSha256'],'frameId':report['frameId'],'imageSha256':report['imageSha256'],
            'inspectedControlIds':[c['id'] for c in data['controls']],'reasons':['Synthetic controls independently checked.'],
            'verdict':'ACCEPT_WITHIN_STATED_CONTROL_UNCERTAINTY'}
    receipt=verify_camera_review(report,'f'*64,review);assert receipt['acceptedWithinStatedControlUncertainty']
    assert receipt['frameId']=='frame-1' and receipt['rawCloudBytesRehashed'] is False
    review['frameId']='another-frame'
    with pytest.raises(MultiviewError,match='STALE_CAMERA_REVIEW_BINDING'):verify_camera_review(report,'f'*64,review)
    review['frameId']=report['frameId'];review['reviewer']['actorId']='camera-author'
    with pytest.raises(MultiviewError,match='AUTHOR_SELF_REVIEW'):verify_camera_review(report,'f'*64,review)


def test_camera_report_binds_all_executed_producers_and_runtime(camera_case):
    data,_,path=camera_case;report=check_or_fit_camera(validate_camera_case(data,path))
    assert set(report['provenance']['softwareVersions'])=={'Python','numpy','scipy'}
    assert set(report['provenance']['producerSha256'])=={'scene-core/camera_control_fit.py','scene-core/multiview_fit.py','scene-core/photo_projection.py','scripts/camera_control_refinement.py'}
    report['provenance']['producerSha256']['scripts/camera_control_refinement.py']='0'*64
    with pytest.raises(MultiviewError,match='STALE_PRODUCER_BINDING'):verify_camera_review(report,'f'*64,{})


def test_cli_unicode_and_nonoverwrite(camera_case):
    data,_,path=camera_case;source=path/'相机输入.json';source.write_text(json.dumps(data),encoding='utf-8');out=path/'相机结果.json'
    command=[sys.executable,str(ROOT/'scripts/camera_control_refinement.py'),'fit','--input',str(source),'--output',str(out)]
    result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8');assert result.returncode==0,result.stderr
    assert json.loads(out.read_text(encoding='utf-8'))['cameraVerified'] is False
    again=subprocess.run(command,capture_output=True,text=True,encoding='utf-8');assert again.returncode==2 and 'OUTPUT_EXISTS' in again.stderr


def test_camera_fit_and_review_cannot_overwrite_bound_inputs(camera_case):
    data,_,path=camera_case;case_path=path/'case.json';case_path.write_text(json.dumps(data),encoding='utf-8')
    bundle=load_camera_case(case_path);assert str(case_path) in {b['path'] for b in bundle['bindings']}
    report=check_or_fit_camera(bundle);report_path=path/'report.json';report_path.write_text(json.dumps(report),encoding='utf-8')
    review={'schema':'camera-control-refinement-review-v1','reviewer':{'actorId':'reviewer','runId':'independent-review-run','role':'reviewer','toolPolicy':'readonly',
            'toolPolicyId':'reviewer-readonly-v1','toolPolicySha256':sha256(REVIEW_POLICY_PATH)},
            'reportSha256':sha256(report_path),'inputSha256':report['inputSha256'],'frameId':report['frameId'],'imageSha256':report['imageSha256'],
            'inspectedControlIds':[c['id'] for c in data['controls']],'reasons':['Synthetic independent controls checked.'],
            'verdict':'ACCEPT_WITHIN_STATED_CONTROL_UNCERTAINTY'}
    review_path=path/'review.json';review_path.write_text(json.dumps(review),encoding='utf-8');image_path=path/'source-image.png'
    source_path=path/'source-controls.json';protected={p:p.read_bytes() for p in [case_path,image_path,source_path,report_path,review_path]}
    routes=[['fit','--input',str(case_path),'--output',str(image_path)],
            ['check','--input',str(case_path),'--output',str(case_path)],
            ['review','--report',str(report_path),'--review',str(review_path),'--output',str(image_path)],
            ['review','--report',str(report_path),'--review',str(review_path),'--output',str(case_path)],
            ['review','--report',str(report_path),'--review',str(review_path),'--output',str(source_path)]]
    for args in routes:
        result=subprocess.run([sys.executable,str(ROOT/'scripts/camera_control_refinement.py'),*args],capture_output=True,text=True,encoding='utf-8')
        assert result.returncode==2 and 'OUTPUT_OVERWRITES_INPUT' in result.stderr,result.stderr
        assert all(p.read_bytes()==original for p,original in protected.items())
