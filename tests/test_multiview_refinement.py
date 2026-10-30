"""Observable multi-view recovery and fail-closed evidence separation tests."""
from pathlib import Path
import copy,hashlib,json,subprocess,sys
import numpy as np
from PIL import Image,ImageDraw
import pytest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scene-core'))
from photo_projection import CameraModel,Frame,project_points
from multiview_fit import (EDGES,MultiviewError,atomic_json,corners,fit_bundle,fit_object,
                           load_bundle,parameters,sha256,validate_bundle,verify_review,REVIEW_POLICY_PATH,render_report,_mark_residual)


def c2w(center,target=(0,0,1)):
    center=np.array(center,dtype=float);z=center-np.array(target);z/=np.linalg.norm(z)
    x=np.cross([0,0,1],z);x/=np.linalg.norm(x);y=np.cross(z,x)
    m=np.eye(4);m[:3,:3]=np.column_stack([x,y,z]);m[:3,3]=center;return m


@pytest.fixture
def scene(tmp_path):
    truth=np.array([.2,-.1,1.1,2.,1.1,1.6,.25])
    initial=truth+np.array([.12,-.1,.05,.2,-.12,.1,.1])
    proof=tmp_path/'calibration.json';proof.write_text(json.dumps({'source':'synthetic known camera geometry','residualPx':0}),encoding='utf-8')
    model=tmp_path/'baseline-model.json';model.write_text(json.dumps({'parameters':initial.tolist()}),encoding='utf-8')
    bundle={'schema':'multiview-refinement-input-v1','units':'metre','upAxis':'Z',
            'source':{'modelSha256':sha256(model),'coordinateFrame':'synthetic metre Z-up'},
            'sourceBindings':[{'path':model.name,'sha256':sha256(model)}],
            'author':{'actorId':'test-author','runId':'author-1'},
            'policy':{'minFitViews':2,'minHoldoutViews':1,'minCameraBaselineM':.1,
                      'maxRmsPx':3.,'maxHoldoutWorseningPx':.1,'minImprovementPx':.01,
                      'lossScalePx':2.,'maxNfev':500,'conditionLimit':1e7},
            'objects':[{'id':'cuboid','room':'room-a','initial':{'center':initial[:3].tolist(),'size':initial[3:6].tolist(),'yaw':initial[6]},
                        'bounds':{'lower':(truth-np.array([1,1,.5,1,.8,1,.8])).tolist(),
                                  'upper':(truth+np.array([1,1,.5,1,.8,1,.8])).tolist()}}],
            'views':[]}
    for index,position in enumerate([[4,-6,3],[-4,-5,2.8],[4,5,2.3]]):
        cam={'width':800,'height':600,'fx':700.,'fy':700.,'cx':400.,'cy':300.,'model':'pinhole'}
        matrix=c2w(position);frame=Frame(str(index),'',matrix,CameraModel(**cam));uv,_,_=project_points(frame,corners(truth))
        image=Image.new('RGB',(800,600),(30+index*25,35,45));draw=ImageDraw.Draw(image)
        for a,b in EDGES:draw.line([tuple(uv[a]),tuple(uv[b])],fill='white',width=2)
        path=tmp_path/f'view-{index}.png';image.save(path);digest=sha256(path)
        marks=[{'objectId':'cuboid','kind':'corner','corner':i,'pixels':[point.tolist()], 'uncertaintyPx':1.,
                'provenance':{'kind':'manual_image','annotator':'synthetic-ground-truth-fixture','sourceImageSha256':digest}}
               for i,point in enumerate(uv)]
        bundle['views'].append({'id':str(index),'split':'fit' if index<2 else 'holdout','captureGroup':f'camera-{index}',
                                'image':{'path':path.name,'sha256':digest},'camera':cam,'c2w':matrix.tolist(),
                                'calibration':{'status':'VERIFIED','evidence':'known synthetic intrinsics and pose',
                                               'sourceSha256':sha256(proof),'evidenceBindings':[{'path':proof.name,'sha256':sha256(proof)}],
                                               'independentFeatureResidualPx':{'samples':8,'rms':0.,'p95':0.}},'annotations':marks})
    return bundle,truth,tmp_path


def test_recovers_metric_cuboid_and_unseen_camera(scene):
    data,truth,path=scene;result=fit_bundle(validate_bundle(data,path));row=result['objects'][0]
    assert row['status']=='CANDIDATE_REVIEW_REQUIRED' and row['reviewStatus']=='NOT_RUN'
    np.testing.assert_allclose(parameters(row['candidate']),truth,atol=1e-7)
    assert row['before']['fit']['summary']['rmsPx']>10
    assert row['after']['fit']['summary']['rmsPx']<1e-6
    assert row['after']['holdout']['summary']['rmsPx']<1e-6
    assert row['solver']['jacobianRank']==7
    assert len(row['after']['holdout']['observations'])==8


def test_holdout_does_not_enter_optimizer_and_contradiction_rejects(scene):
    data,_,path=scene;baseline=fit_object(validate_bundle(data,path),'cuboid')
    for mark in data['views'][2]['annotations']:mark['pixels'][0][0]+=70
    changed=fit_object(validate_bundle(data,path),'cuboid')
    np.testing.assert_allclose(parameters(baseline['candidate']),parameters(changed['candidate']),atol=1e-11)
    assert changed['status']=='REJECTED' and 'HOLDOUT_ERROR_EXCEEDS_POLICY' in changed['reasons']
    assert changed['after']['holdout']['summary']['rmsPx']>60


def test_holdout_regression_is_not_hidden_by_fit_improvement(scene):
    data,_,path=scene;view=data['views'][2]
    frame=Frame(view['id'],'',np.array(view['c2w']),CameraModel(**view['camera']))
    initial=parameters(data['objects'][0]['initial']);uv,_,_=project_points(frame,corners(initial))
    for mark,point in zip(view['annotations'],uv):mark['pixels']=[point.tolist()]
    row=fit_object(validate_bundle(data,path),'cuboid')
    assert row['before']['holdout']['summary']['rmsPx']<1e-10
    assert row['status']=='REJECTED' and 'HOLDOUT_REGRESSION' in row['reasons']


def test_bad_individual_view_cannot_be_diluted_by_combined_metric(scene):
    data,_,path=scene
    data['policy']['maxRmsPx']=100;data['policy']['maxPerViewRmsPx']=3
    for mark in data['views'][2]['annotations']:mark['pixels'][0][0]+=8
    row=fit_object(validate_bundle(data,path),'cuboid')
    assert row['after']['holdout']['summary']['rmsPx']<data['policy']['maxRmsPx']
    assert row['status']=='REJECTED' and 'VIEW_ERROR_EXCEEDS_POLICY' in row['reasons']
    assert row['perViewPolicyFailures'][0]['viewId']=='2'


def test_solution_hitting_parameter_bound_is_explicitly_withheld(scene):
    data,truth,path=scene;data['objects'][0]['bounds']['lower'][0]=truth[0]+.05
    row=fit_object(validate_bundle(data,path),'cuboid')
    assert row['status']=='REJECTED' and 'PARAMETER_BOUND_REACHED' in row['reasons']


def test_partial_edge_constraints_recover_and_report_pixel_error(scene):
    data,truth,path=scene
    for view in data['views']:
        frame=Frame(view['id'],'',np.array(view['c2w']),CameraModel(**view['camera']))
        uv,_,_=project_points(frame,corners(truth));marks=[]
        for a,b in EDGES:
            # Partial visible middle segments, not assumed whole-object endpoints.
            points=[((1-t)*uv[a]+t*uv[b]).tolist() for t in (.2,.5,.8)]
            marks.append({'objectId':'cuboid','kind':'edge','edge':[a,b],'pixels':points,'uncertaintyPx':1.,
                          'provenance':{'kind':'independent_image_edge','annotator':'synthetic-fixture','sourceImageSha256':view['image']['sha256']}})
        view['annotations']=marks
    row=fit_object(validate_bundle(data,path),'cuboid')
    assert row['status']=='CANDIDATE_REVIEW_REQUIRED'
    np.testing.assert_allclose(parameters(row['candidate']),truth,atol=1e-5)
    assert row['after']['holdout']['summary']['rmsPx']<1e-5


def test_duplicate_image_content_rejected_even_under_other_filename(scene):
    data,_,path=scene
    other=path/'different-name.png';other.write_bytes((path/data['views'][0]['image']['path']).read_bytes())
    data['views'][2]['image']={'path':other.name,'sha256':sha256(other)}
    with pytest.raises(MultiviewError,match='DUPLICATE_IMAGE_CONTENT'):validate_bundle(data,path)


def test_capture_group_and_camera_position_leakage_rejected(scene):
    data,_,path=scene;data['views'][2]['captureGroup']=data['views'][0]['captureGroup']
    with pytest.raises(MultiviewError,match='CAPTURE_GROUP_LEAKAGE'):validate_bundle(data,path)
    data['views'][2]['captureGroup']='independent-label'
    data['views'][2]['c2w']=copy.deepcopy(data['views'][0]['c2w'])
    row=fit_object(validate_bundle(data,path),'cuboid')
    assert 'HOLDOUT_CAMERA_NOT_INDEPENDENT' in row['reasons']


def test_rank_insufficiency_and_no_marks_remain_in_denominator(scene):
    data,_,path=scene
    for view in data['views']:view['annotations']=view['annotations'][:1]
    result=fit_bundle(validate_bundle(data,path));assert not result['allObjectsProducedCandidates']
    assert 'INSUFFICIENT_CONSTRAINTS' in result['objects'][0]['reasons']
    for view in data['views']:view['annotations']=None
    result=fit_bundle(validate_bundle(data,path));assert len(result['objects'])==1
    assert result['objects'][0]['status']=='INSUFFICIENT_EVIDENCE'


def test_observed_single_face_has_unobservable_height_not_false_confidence(scene):
    data,_,path=scene
    for view in data['views'][:2]:view['annotations']=view['annotations'][:4]
    row=fit_object(validate_bundle(data,path),'cuboid')
    assert 'DEGENERATE_FIT' in row['reasons'] and row['solver']['jacobianRank']<7


def test_fisheye_curved_edges_are_sampled_in_3d(scene):
    data,truth,path=scene
    for index,view in enumerate(data['views']):
        view['camera']['model']='fisheye-equidistant';view['camera']['k']=[.1,-.03,.005,0]
        frame=Frame(view['id'],'',np.array(view['c2w']),CameraModel(**view['camera']))
        xyz=corners(truth);marks=[];im=Image.new('RGB',(800,600),(20+index*20,25,30));draw=ImageDraw.Draw(im)
        sampled=[]
        for a,b in EDGES:
            points=xyz[a]+np.array([.15,.5,.85])[:,None]*(xyz[b]-xyz[a]);uv,_,_=project_points(frame,points)
            sampled.append(([a,b],uv));draw.line([tuple(p) for p in uv],fill='white',width=2)
        image_path=path/view['image']['path'];im.save(image_path);view['image']['sha256']=sha256(image_path)
        for edge,uv in sampled:
            marks.append({'objectId':'cuboid','kind':'edge','edge':edge,'pixels':uv.tolist(),'uncertaintyPx':1.,
                          'provenance':{'kind':'independent_image_edge','annotator':'synthetic-fixture','sourceImageSha256':view['image']['sha256']}})
        view['annotations']=marks
    row=fit_object(validate_bundle(data,path),'cuboid')
    assert row['status']=='CANDIDATE_REVIEW_REQUIRED',row['reasons']
    assert row['after']['holdout']['summary']['rmsPx']<.03
    np.testing.assert_allclose(parameters(row['candidate']),truth,atol=.002)


@pytest.mark.parametrize('model',['pinhole','fisheye-equidistant'])
def test_visible_partial_edge_is_near_clipped_but_hidden_corner_rejected(model):
    frame=Frame('close-view','',np.eye(4),CameraModel(800,600,100,100,400,300,model=model))
    values=np.array([0.,0.,-.4,.4,.4,2.,0.])
    # Corner 0 is forward, corner 4 is behind the camera. The finite observed
    # front portion of their shared physical edge is legitimate image evidence.
    pixels,_,_=project_points(frame,np.array([[-.2,-.2,-1.2],[-.2,-.2,-.8]]))
    edge={'kind':'edge','edge':[0,4],'pixels':pixels.tolist()}
    residual,valid=_mark_residual(values,{'frame':frame},edge,.01)
    assert valid and np.max(np.abs(residual))<1e-6
    corner={'kind':'corner','corner':4,'pixels':[[400,300]]}
    _,valid=_mark_residual(values,{'frame':frame},corner,.01);assert not valid
    behind=values.copy();behind[2]=2.
    _,valid=_mark_residual(behind,{'frame':frame},edge,.01);assert not valid


def test_behind_camera_and_negative_dimensions_rejected(scene):
    data,_,path=scene;data['objects'][0]['initial']['center']=[30.,-40.,10.]
    data['objects'][0]['bounds']['lower'][:3]=[20.,-50.,0.];data['objects'][0]['bounds']['upper'][:3]=[40.,-30.,20.]
    row=fit_object(validate_bundle(data,path),'cuboid');assert 'INVALID_INITIAL_PROJECTION' in row['reasons']
    data['objects'][0]['initial']['size'][0]=-1
    with pytest.raises(MultiviewError,match='NONPOSITIVE_SIZE'):validate_bundle(data,path)


def test_unverified_camera_is_inventoryable_but_not_accepted(scene):
    data,_,path=scene;data['views'][0]['calibration']={'status':'DIAGNOSTIC','evidence':'screening only'}
    row=fit_object(validate_bundle(data,path),'cuboid')
    assert row['candidate'] is not None and 'CAMERA_UNVERIFIED' in row['reasons'] and row['status']=='REJECTED'
    data['views'][0]['calibration']={'status':'VERIFIED','evidence':'unsupported word only'}
    with pytest.raises(MultiviewError,match='INCOMPLETE_CALIBRATION_EVIDENCE'):validate_bundle(data,path)


@pytest.mark.parametrize('damage',['derived','wrong_hash','outside','nan','duplicate'])
def test_invalid_observations_and_provenance_fail_closed(scene,damage):
    data,_,path=scene;view=data['views'][0];mark=view['annotations'][0]
    if damage=='derived':mark['provenance']['derivedFromModel']=True
    elif damage=='wrong_hash':mark['provenance']['sourceImageSha256']='b'*64
    elif damage=='outside':mark['pixels']=[[-1,20]]
    elif damage=='nan':mark['pixels']=[[float('nan'),20]]
    else:view['annotations'].append(copy.deepcopy(mark))
    with pytest.raises(MultiviewError):validate_bundle(data,path)


def test_source_change_during_run_and_atomic_nonoverwrite(scene):
    data,_,path=scene;bundle=validate_bundle(data,path)
    (path/data['views'][0]['image']['path']).write_bytes(b'changed')
    with pytest.raises(MultiviewError,match='INPUT_CHANGED_DURING_RUN'):fit_bundle(bundle)
    output=path/'result.json';atomic_json(output,{'test':'ok'})
    with pytest.raises(MultiviewError,match='OUTPUT_EXISTS'):atomic_json(output,{'test':'replace'})
    assert json.loads(output.read_text(encoding='utf-8'))=={'test':'ok'}


def test_declared_model_digest_requires_actual_model_byte_binding(scene):
    data,_,path=scene;data['sourceBindings']=[]
    with pytest.raises(MultiviewError,match='UNBOUND_SOURCE_MODEL'):validate_bundle(data,path)
    data['sourceBindings']=[{'path':'baseline-model.json','sha256':'f'*64}]
    data['source']['modelSha256']='f'*64
    with pytest.raises(MultiviewError,match='SOURCE_HASH_MISMATCH'):validate_bundle(data,path)


def test_review_requires_distinct_actor_run_complete_views_and_exact_hash(scene):
    data,_,path=scene;report=fit_bundle(validate_bundle(data,path));row=report['objects'][0]
    review={'schema':'multiview-refinement-review-v1','reviewer':{'actorId':'independent-reviewer','runId':'review-1','role':'reviewer','toolPolicy':'readonly',
            'toolPolicyId':'reviewer-readonly-v1','toolPolicySha256':sha256(REVIEW_POLICY_PATH)},
            'fitReportSha256':'c'*64,'inputSha256':report['inputSha256'],
            'objects':[{'objectId':'cuboid','verdict':'ACCEPT','inspectedViews':row['annotatedViews'],'reasons':['Synthetic observations independently checked.']}]}
    assert verify_review(report,'c'*64,review)['allObjectsReviewedAndAccepted']
    review['reviewer']['actorId']='test-author'
    with pytest.raises(MultiviewError,match='AUTHOR_SELF_REVIEW'):verify_review(report,'c'*64,review)
    review['reviewer']['actorId']='independent-reviewer';review['objects'][0]['inspectedViews']=['0','1']
    with pytest.raises(MultiviewError,match='INCOMPLETE_REVIEW_VIEWS'):verify_review(report,'c'*64,review)
    review['objects'][0]['inspectedViews']=row['annotatedViews'];review['fitReportSha256']='d'*64
    with pytest.raises(MultiviewError,match='STALE_REVIEW_BINDING'):verify_review(report,'c'*64,review)


def test_review_rechecks_policy_and_original_images(scene):
    data,_,path=scene;report=fit_bundle(validate_bundle(data,path));row=report['objects'][0]
    identity={'actorId':'reviewer','runId':'review-run','role':'reviewer','toolPolicy':'readonly',
              'toolPolicyId':'reviewer-readonly-v1','toolPolicySha256':sha256(REVIEW_POLICY_PATH)}
    review={'schema':'multiview-refinement-review-v1','reviewer':identity,'fitReportSha256':'e'*64,'inputSha256':report['inputSha256'],
            'objects':[{'objectId':'cuboid','verdict':'ACCEPT','inspectedViews':row['annotatedViews'],'reasons':['Synthetic view check.']}]}
    identity['toolPolicySha256']='f'*64
    with pytest.raises(MultiviewError,match='REVIEW_POLICY_BINDING_MISMATCH'):verify_review(report,'e'*64,review)
    identity['toolPolicySha256']=sha256(REVIEW_POLICY_PATH)
    (path/data['views'][1]['image']['path']).write_bytes(b'changed after fitting')
    with pytest.raises(MultiviewError,match='INPUT_CHANGED_DURING_RUN'):verify_review(report,'e'*64,review)


def test_report_records_producer_runtime_and_rejects_relabelled_producer(scene):
    data,_,path=scene;report=fit_bundle(validate_bundle(data,path))
    assert set(report['provenance']['softwareVersions'])=={'Python','numpy','scipy'}
    assert set(report['provenance']['producerSha256'])=={'scene-core/multiview_fit.py','scene-core/photo_projection.py','scripts/multiview_refinement.py'}
    report['provenance']['producerSha256']['scene-core/photo_projection.py']='0'*64
    with pytest.raises(MultiviewError,match='STALE_PRODUCER_BINDING'):render_report(validate_bundle(data,path),report,path/'stale-render')
    assert not (path/'stale-render').exists()


def test_cli_unicode_paths_and_fit_exit_status(scene):
    data,_,path=scene;input_path=path/'输入.json';input_path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    output=path/'结果.json'
    result=subprocess.run([sys.executable,str(ROOT/'scripts/multiview_refinement.py'),'fit','--input',str(input_path),'--output',str(output)],capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stderr
    assert json.loads(output.read_text(encoding='utf-8'))['allObjectsProducedCandidates']
    assert json.loads(result.stdout)['status']=='REVIEW_REQUIRED'
    overwrite=subprocess.run([sys.executable,str(ROOT/'scripts/multiview_refinement.py'),'fit','--input',str(input_path),'--output',str(output)],capture_output=True,text=True,encoding='utf-8')
    assert overwrite.returncode==2 and 'OUTPUT_EXISTS' in overwrite.stderr


def test_all_cli_output_routes_protect_case_source_image_and_bound_model(scene):
    data,_,path=scene;case_path=path/'case.json';case_path.write_text(json.dumps(data),encoding='utf-8')
    bundle=load_bundle(case_path);assert str(case_path) in {b['path'] for b in bundle['bindings']}
    report=fit_bundle(bundle);report_path=path/'fit.json';atomic_json(report_path,report)
    review={'schema':'multiview-refinement-review-v1','reviewer':{'actorId':'independent-reviewer','runId':'review-run','role':'reviewer','toolPolicy':'readonly',
            'toolPolicyId':'reviewer-readonly-v1','toolPolicySha256':sha256(REVIEW_POLICY_PATH)},
            'fitReportSha256':sha256(report_path),'inputSha256':report['inputSha256'],
            'objects':[{'objectId':'cuboid','verdict':'ACCEPT','inspectedViews':report['objects'][0]['annotatedViews'],'reasons':['Synthetic independent inspection.']}]}
    review_path=path/'review.json';atomic_json(review_path,review)
    image_path=path/data['views'][0]['image']['path'];model_path=path/'baseline-model.json'
    protected={p:p.read_bytes() for p in [case_path,image_path,model_path,report_path,review_path]}
    routes=[['fit','--input',str(case_path),'--output',str(image_path)],
            ['inspect','--input',str(case_path),'--output',str(case_path)],
            ['report','--fit-report',str(report_path),'--review',str(review_path),'--output',str(image_path)],
            ['report','--fit-report',str(report_path),'--review',str(review_path),'--output',str(case_path)],
            ['report','--fit-report',str(report_path),'--review',str(review_path),'--output',str(model_path)],
            ['render','--input',str(case_path),'--fit-report',str(report_path),'--output-dir',str(path)]]
    for args in routes:
        result=subprocess.run([sys.executable,str(ROOT/'scripts/multiview_refinement.py'),*args],capture_output=True,text=True,encoding='utf-8')
        assert result.returncode==2 and 'OUTPUT_OVERWRITES_INPUT' in result.stderr,result.stderr
        assert all(p.read_bytes()==original for p,original in protected.items())


def test_paired_review_renders_use_original_images_and_bind_errors(scene):
    data,_,path=scene;bundle=validate_bundle(data,path);report=fit_bundle(bundle)
    numeric_report=copy.deepcopy(report)
    out=path/'paired';result=render_report(bundle,report,out,panel_width=400)
    assert result['images']==3 and result['status']=='VISUAL_REVIEW_NOT_RUN'
    manifest=json.loads((out/'render-manifest.json').read_text(encoding='utf-8'))
    for image in manifest['images']:
        assert image['originalWidth']==800 and image['scale']==.5
        assert sha256(out/image['file'])==image['sha256']
        with Image.open(out/image['file']) as im:
            assert im.size==(800,342)
            colors=np.array(im);assert np.count_nonzero(np.all(colors==[20,220,90],axis=2))>10
    assert report==numeric_report
    assert report['objects'][0]['after']['holdout']['summary']['rmsPx']<1e-6
    with pytest.raises(MultiviewError,match='OUTPUT_EXISTS'):render_report(bundle,report,out)
