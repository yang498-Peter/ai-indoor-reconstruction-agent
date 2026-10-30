"""Check or locally refine one camera using fixed, uncertainty-bearing controls.

This produces conditional presentation calibration evidence, never survey truth.
It never optimizes intrinsics, source 3D controls, or object geometry. Pose deltas
are camera-local translations and rotation vectors about the original camera.
"""
from __future__ import annotations
import copy,math
from pathlib import Path
import numpy as np
from PIL import Image
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation
from photo_projection import CameraModel,Frame,project_points
from multiview_fit import (MultiviewError,_read_json,_finite,_fail,atomic_json,sha256,
                           canonical_hash,verify_bindings,REVIEW_POLICY_PATH,REVIEW_POLICY_ID,producer_binding,verify_producer_binding)

INPUT_SCHEMA='camera-control-refinement-input-v1'
REPORT_SCHEMA='camera-control-refinement-report-v1'


def _sigma(value,count,name):
    if isinstance(value,(int,float)) and not isinstance(value,bool):value=[value]*count
    result=_finite(value,(count,),name);_fail(np.all(result>0),name);return result


def _project(frame,xyz,near):
    uv,depth,front=project_points(frame,np.asarray(xyz).reshape(-1,3),near=near)
    _fail(front.all() and np.isfinite(uv).all(),'CONTROL_BEHIND_CAMERA')
    return uv,depth


def updated_pose(initial,delta):
    matrix=np.eye(4);matrix[:3,:3]=initial[:3,:3]@Rotation.from_rotvec(delta[3:]).as_matrix()
    matrix[:3,3]=initial[:3,3]+initial[:3,:3]@delta[:3]
    return matrix


def _frame(bundle,delta):
    original=bundle['frame']
    return Frame(original.frame_id,original.file_path,updated_pose(original.c2w,delta),original.camera,image_path=original.image_path)


def _control_covariance(frame,control,near):
    """Linearized world-point covariance, frozen at the original pose per run."""
    xyz=control['xyz'];sigma=control['sigma3d'];epsilon=max(1e-6,float(sigma.min())*1e-3)
    columns=[]
    for axis in range(3):
        direction=np.eye(3)[axis]*epsilon
        plus,_=_project(frame,[xyz+direction],near);minus,_=_project(frame,[xyz-direction],near)
        columns.append((plus[0]-minus[0])/(2*epsilon))
    jac=np.column_stack(columns)
    covariance=jac@np.diag(sigma*sigma)@jac.T+np.diag(control['sigma2d']**2)
    return covariance,np.linalg.cholesky(covariance)


def load_camera_case(path):
    path=Path(path).resolve(strict=True);data=_read_json(path)
    result=validate_camera_case(data,path.parent)
    result['inputSha256']=sha256(path);result['bindings'].append({'path':str(path),'sha256':result['inputSha256']})
    return result


def validate_camera_case(data,base):
    _fail(data.get('schema')==INPUT_SCHEMA and data.get('units')=='metre' and data.get('upAxis')=='Z','INVALID_CAMERA_CONTROL_SCHEMA')
    author=data.get('author',{})
    _fail(all(isinstance(author.get(k),str) and author[k] for k in ('actorId','runId')),'INVALID_AUTHOR')
    source=data.get('source',{});_fail(isinstance(source.get('coordinateFrame'),str) and source['coordinateFrame'],'MISSING_COORDINATE_FRAME')
    for name in ('sourceCloudSha256','poseSourceSha256','calibrationSourceSha256'):
        digest=source.get(name);_fail(isinstance(digest,str) and len(digest)==64 and all(c in '0123456789abcdef' for c in digest),'INVALID_SOURCE_DIGEST',field=name)
    bindings=[];source_bindings=data.get('sourceBindings',[])
    _fail(isinstance(source_bindings,list) and source_bindings,'MISSING_SOURCE_BINDINGS')
    for entry in source_bindings:
        p=(Path(base)/entry['path']).resolve(strict=True);_fail(sha256(p)==entry.get('sha256'),'SOURCE_HASH_MISMATCH',path=str(p))
        bindings.append({'path':str(p),'sha256':entry['sha256']})
    digests={b['sha256'] for b in bindings}
    _fail(source['poseSourceSha256'] in digests and source['calibrationSourceSha256'] in digests,'UNBOUND_CAMERA_SOURCE')
    # A measured-control artifact establishes explicit source lineage. We do
    # not pretend to rehash a multi-gigabyte raw cloud once for every camera.
    lineage=data.get('sourceEvidenceBinding',{});lineage_path=(Path(base)/lineage.get('path','')).resolve(strict=True)
    _fail(sha256(lineage_path)==lineage.get('sha256') and str(lineage_path) in {b['path'] for b in bindings},'UNBOUND_SOURCE_CONTROL_ARTIFACT')
    evidence=_read_json(lineage_path)
    _fail(evidence.get('sourceCloudSha256')==source['sourceCloudSha256'],'SOURCE_CLOUD_LINEAGE_MISMATCH')
    mapping=copy.deepcopy(data.get('sourceEvidenceMapping',{'items':'controls','position':'xyz','uncertainty':'uncertaintyM'}))
    _fail(isinstance(mapping,dict) and set(mapping)=={'items','position','uncertainty'}
          and all(isinstance(v,str) and v and '/' not in v and '.' not in v for v in mapping.values()),'INVALID_SOURCE_EVIDENCE_MAPPING')
    features=evidence.get(mapping['items'],[]);_fail(isinstance(features,list),'INVALID_SOURCE_CONTROL_ARTIFACT')
    fixed={}
    for feature in features:
        fid=feature.get('id');_fail(isinstance(fid,str) and fid and fid not in fixed,'DUPLICATE_SOURCE_CONTROL')
        fixed[fid]=feature
    frame_data=data.get('frame',{});image=frame_data.get('image',{});image_path=(Path(base)/image.get('path','')).resolve(strict=True)
    _fail(sha256(image_path)==image.get('sha256'),'IMAGE_HASH_MISMATCH')
    camera_data=copy.deepcopy(frame_data.get('camera',{}));camera_data['k']=tuple(camera_data.get('k',[0,0,0,0]))
    try:camera=CameraModel(**camera_data)
    except (TypeError,ValueError) as exc:raise MultiviewError('INVALID_CAMERA',str(exc)) from exc
    _fail(camera.is_valid() and camera.model in ('pinhole','fisheye-equidistant') and len(camera.k)==4 and np.isfinite(camera.k).all(),'INVALID_CAMERA')
    with Image.open(image_path) as im:_fail(im.size==(camera.width,camera.height),'IMAGE_DIMENSION_MISMATCH')
    matrix=_finite(frame_data.get('c2w'),(4,4),'INVALID_CAMERA_POSE')
    _fail(np.allclose(matrix[3],[0,0,0,1],atol=1e-8) and np.allclose(matrix[:3,:3].T@matrix[:3,:3],np.eye(3),atol=1e-5)
          and abs(np.linalg.det(matrix[:3,:3])-1)<1e-5,'NONRIGID_CAMERA_POSE')
    _fail(isinstance(frame_data.get('id'),str) and frame_data['id'],'INVALID_FRAME_ID')
    frame=Frame(frame_data['id'],image['path'],matrix,camera,image_path=image_path)
    bindings.append({'path':str(image_path),'sha256':image['sha256']})
    policy=copy.deepcopy(data.get('policy',{}))
    names=['minFitControls','minHoldoutControls','minHoldoutSeparationM','maxNormalizedRms','maxPerControlNormalizedError',
           'maxHoldoutWorseningNormalized','minImprovementNormalized','lossScale','maxNfev','conditionLimit','minSpatialSingularRatio']
    _fail(all(isinstance(policy.get(k),(int,float)) and not isinstance(policy[k],bool) and math.isfinite(policy[k]) and policy[k]>=0 for k in names),'INCOMPLETE_CAMERA_POLICY',required=names)
    _fail(policy['minFitControls']>=4 and policy['minHoldoutControls']>=2 and policy['minHoldoutSeparationM']>0
          and policy['maxNormalizedRms']>0 and policy['maxPerControlNormalizedError']>0 and policy['lossScale']>0
          and policy['maxNfev']>=1 and policy['conditionLimit']>1 and 0<policy['minSpatialSingularRatio']<1,'UNSAFE_CAMERA_POLICY')
    policy.setdefault('nearM',.01);policy.setdefault('rankRelativeTolerance',1e-7)
    policy.setdefault('minSpatialSpreadSigma',1.)
    _fail(isinstance(policy['nearM'],(int,float)) and 0<policy['nearM']<1 and 0<policy['rankRelativeTolerance']<1
          and policy['minSpatialSpreadSigma']>0,'INVALID_NUMERICAL_POLICY')
    bounds=data.get('bounds',{});translation=_finite(bounds.get('translationM'),(3,),'INVALID_TRANSLATION_BOUNDS')
    rotation=_finite(bounds.get('rotationRad'),(3,),'INVALID_ROTATION_BOUNDS')
    _fail(np.all(translation>0) and np.all(rotation>0) and np.all(rotation<math.pi/2),'UNSAFE_CAMERA_BOUNDS')
    controls=[];ids=set();positions=[]
    for entry in data.get('controls',[]):
        cid=entry.get('id');_fail(isinstance(cid,str) and cid and cid not in ids,'DUPLICATE_CONTROL_ID');ids.add(cid)
        _fail(entry.get('split') in ('fit','holdout') and entry.get('kind')=='static_architecture_junction','INVALID_CONTROL_ROLE')
        xyz=_finite(entry.get('xyz'),(3,),'INVALID_SOURCE_CONTROL');sigma3d=_sigma(entry.get('uncertaintyM'),3,'INVALID_3D_UNCERTAINTY')
        pixels=_finite(entry.get('pixels'),(2,),'INVALID_CONTROL_PIXELS');sigma2d=_sigma(entry.get('uncertaintyPx'),2,'INVALID_2D_UNCERTAINTY')
        _fail(np.all(pixels>=0) and pixels[0]<=camera.width-1 and pixels[1]<=camera.height-1,'CONTROL_PIXELS_OUTSIDE_IMAGE')
        _fail(cid in fixed,'CONTROL_NOT_IN_SOURCE_ARTIFACT',controlId=cid)
        source_feature=fixed[cid]
        _fail(np.allclose(xyz,_finite(source_feature.get(mapping['position']),(3,),'INVALID_ARTIFACT_XYZ'),atol=1e-10,rtol=0)
              and np.allclose(sigma3d,_sigma(source_feature.get(mapping['uncertainty']),3,'INVALID_ARTIFACT_UNCERTAINTY'),atol=1e-12,rtol=0),
              'SOURCE_CONTROL_WAS_CHANGED',controlId=cid)
        _fail(not any(np.linalg.norm(xyz-other)<1e-9 for other in positions),'DUPLICATE_CONTROL_POSITION');positions.append(xyz)
        provenance=entry.get('provenance',{})
        _fail(isinstance(provenance.get('annotator'),str) and bool(provenance['annotator'])
              and provenance.get('sourceImageSha256')==image['sha256'] and provenance.get('sourceCloudSha256')==source['sourceCloudSha256']
              and isinstance(provenance.get('featureDescription'),str) and bool(provenance['featureDescription'])
              and provenance.get('derivedFromTargetObject') is False,'INVALID_CONTROL_PROVENANCE',controlId=cid)
        control={'raw':copy.deepcopy(entry),'id':cid,'split':entry['split'],'xyz':xyz,'pixels':pixels,'sigma3d':sigma3d,'sigma2d':sigma2d}
        covariance,cholesky=_control_covariance(frame,control,policy['nearM']);control['covariance']=covariance;control['cholesky']=cholesky
        controls.append(control)
    return {'data':copy.deepcopy(data),'frame':frame,'controls':controls,'policy':policy,'bounds':np.concatenate([translation,rotation]),
            'bindings':bindings,'contentSha256':canonical_hash(data),
            'sourceLineage':{'verification':'HASH_BOUND_FIXED_CONTROL_ARTIFACT','artifactSha256':lineage['sha256'],'mapping':mapping,
                             'sourceCloudSha256':source['sourceCloudSha256'],'rawCloudBytesRehashed':False}}


def _residual(bundle,delta,controls):
    frame=_frame(bundle,delta);result=[]
    for control in controls:
        try:uv,_=_project(frame,[control['xyz']],bundle['policy']['nearM']);r=np.linalg.solve(control['cholesky'],uv[0]-control['pixels'])
        except MultiviewError:r=np.full(2,1e6)
        result.extend(r)
    return np.array(result)


def score_camera(bundle,delta,split):
    frame=_frame(bundle,delta);rows=[]
    for control in bundle['controls']:
        if control['split']!=split:continue
        try:
            uv,depth=_project(frame,[control['xyz']],bundle['policy']['nearM']);difference=uv[0]-control['pixels'];valid=True
            normalized=np.linalg.solve(control['cholesky'],difference)
            error_px=float(np.linalg.norm(difference));error_normalized=float(np.linalg.norm(normalized));projected=uv[0].tolist();distance=float(depth[0])
        except MultiviewError:error_px=error_normalized=1e6;projected=None;distance=None;valid=False
        rows.append({'id':control['id'],'observedPixels':control['pixels'].tolist(),'projectedPixels':projected,'errorPx':error_px,
                     'sourceXYZ':control['xyz'].tolist(),
                     'normalizedError':error_normalized,'validProjection':valid,'depthM':distance,
                     'sourceUncertaintyM':control['sigma3d'].tolist(),'imageUncertaintyPx':control['sigma2d'].tolist(),
                     'frozenTotalPixelSigma':np.sqrt(np.diag(control['covariance'])).tolist()})
    def rms(key):return float(np.sqrt(np.mean([r[key]**2 for r in rows]))) if rows else None
    return {'controls':rows,'count':len(rows),'rmsPx':rms('errorPx'),'normalizedRms':rms('normalizedError'),
            'maxErrorPx':max((r['errorPx'] for r in rows),default=None),'maxNormalizedError':max((r['normalizedError'] for r in rows),default=None),
            'validProjections':all(r['validProjection'] for r in rows)}


def _geometry(bundle):
    fit=[c for c in bundle['controls'] if c['split']=='fit'];holdout=[c for c in bundle['controls'] if c['split']=='holdout']
    if fit:
        xyz=np.array([c['xyz'] for c in fit]);s=np.linalg.svd(xyz-xyz.mean(0),compute_uv=False)
        spread=float(s[-1]/math.sqrt(len(xyz))) if len(s)==3 else 0.
        ratio=float(s[-1]/s[0]) if len(s)==3 and s[0]>0 else 0.
        sigma=float(np.median([c['sigma3d'].max() for c in fit]))
    else:s=[];spread=ratio=sigma=0.
    separation=min((float(np.linalg.norm(a['xyz']-b['xyz'])) for a in fit for b in holdout),default=0.)
    return {'fit':fit,'holdout':holdout,'fitSpatialSingularValuesM':list(map(float,s)), 'fitSpatialSingularRatio':ratio,
            'fitMinimumRmsSpreadM':spread,'medianSourceSigmaM':sigma,'holdoutMinimumSeparationM':separation}


def _jacobian(bundle,delta,fit):
    columns=[]
    for axis in range(6):
        step=np.eye(6)[axis]*1e-6
        columns.append((_residual(bundle,delta+step,fit)-_residual(bundle,delta-step,fit))/(2e-6))
    jac=np.column_stack(columns);norm=np.linalg.norm(jac,axis=0);scaled=jac/np.where(norm>1e-14,norm,1.)
    singular=np.linalg.svd(scaled,compute_uv=False);rank=int(np.sum(singular>singular[0]*bundle['policy']['rankRelativeTolerance'])) if len(singular) and singular[0]>0 else 0
    condition=float(singular[0]/singular[-1]) if len(singular) and singular[-1]>0 else None
    return {'rank':rank,'columnNormalizedCondition':condition,'singularValues':singular.tolist()}


def check_or_fit_camera(bundle,optimize=True):
    verify_bindings(bundle);provenance=producer_binding('camera');p=bundle['policy'];geometry=_geometry(bundle);zero=np.zeros(6);reasons=[]
    report={'schema':REPORT_SCHEMA,'inputSha256':bundle.get('inputSha256',bundle['contentSha256']),
            'source':bundle['data']['source'],'sourceLineage':bundle['sourceLineage'],'author':bundle['data']['author'],
            'provenance':provenance,
            'frameId':bundle['frame'].frame_id,'imageSha256':bundle['data']['frame']['image']['sha256'],
            'originalC2w':bundle['frame'].c2w.tolist(),'originalC2wSha256':canonical_hash(bundle['frame'].c2w.tolist()),
            'intrinsics':copy.deepcopy(bundle['data']['frame']['camera']),
            'policy':copy.deepcopy(p),'boundsCameraLocal':bundle['data']['bounds'],'bindings':copy.deepcopy(bundle['bindings']),
            'reviewPolicy':{'policyId':REVIEW_POLICY_ID,'sha256':sha256(REVIEW_POLICY_PATH)},
            'status':'REJECTED','reasons':reasons,'independentReviewStatus':'NOT_RUN','cameraVerified':False,
            'before':{split:score_camera(bundle,zero,split) for split in ('fit','holdout')},
            'geometry':{k:v for k,v in geometry.items() if k not in ('fit','holdout')},
            'scope':'Conditional projection of fixed source-cloud architecture controls with stated 3D and 2D uncertainty. This exact frame only; not intrinsic calibration or survey acceptance.',
            'uncertaintyMethod':'J_projection * diag(source_sigma_m^2) * J_projection.T + diag(image_sigma_px^2), frozen at the original camera pose.'}
    if len(geometry['fit'])<p['minFitControls']:reasons.append('INSUFFICIENT_FIT_CONTROLS')
    if len(geometry['holdout'])<p['minHoldoutControls']:reasons.append('INSUFFICIENT_HOLDOUT_CONTROLS')
    if geometry['fitSpatialSingularRatio']<p['minSpatialSingularRatio'] or geometry['fitMinimumRmsSpreadM']<=p['minSpatialSpreadSigma']*geometry['medianSourceSigmaM']:
        reasons.append('COLLINEAR_OR_COPLANAR_CONTROL_AMBIGUITY')
    if geometry['holdoutMinimumSeparationM']<p['minHoldoutSeparationM']:reasons.append('HOLDOUT_CONTROLS_NOT_SPATIALLY_INDEPENDENT')
    if reasons:
        verify_bindings(bundle);_fail(producer_binding('camera')==provenance,'PRODUCER_CHANGED_DURING_RUN');return report
    fit_before=report['before']['fit'];candidate=zero.copy()
    fit_already_within=(fit_before['normalizedRms']<=p['maxNormalizedRms'] and fit_before['maxNormalizedError']<=p['maxPerControlNormalizedError'])
    solver={'optimized':False,'reason':'FIT_WITHIN_STATED_UNCERTAINTY' if fit_already_within else 'CHECK_ONLY'}
    if optimize and not fit_already_within:
        result=least_squares(lambda delta:_residual(bundle,delta,geometry['fit']),zero,bounds=(-bundle['bounds'],bundle['bounds']),
                             loss='soft_l1',f_scale=p['lossScale'],max_nfev=int(p['maxNfev']),x_scale='jac',ftol=1e-10,xtol=1e-10,gtol=1e-10)
        candidate=result.x;solver={'optimized':True,'success':bool(result.success),'nfev':result.nfev,'termination':str(result.message)}
        if not result.success:reasons.append('SOLVER_DID_NOT_CONVERGE')
        if np.any(result.active_mask):reasons.append('POSE_BOUND_REACHED')
    jacobian=_jacobian(bundle,candidate,geometry['fit']);solver['jacobian']=jacobian
    if jacobian['rank']<6 or jacobian['columnNormalizedCondition'] is None or jacobian['columnNormalizedCondition']>p['conditionLimit']:reasons.append('DEGENERATE_POSE_FIT')
    after={split:score_camera(bundle,candidate,split) for split in ('fit','holdout')}
    for split in ('fit','holdout'):
        if not after[split]['validProjections']:reasons.append('INVALID_'+split.upper()+'_PROJECTION')
        if after[split]['normalizedRms']>p['maxNormalizedRms'] or after[split]['maxNormalizedError']>p['maxPerControlNormalizedError']:
            reasons.append(split.upper()+'_ERROR_EXCEEDS_STATED_UNCERTAINTY')
    if after['holdout']['normalizedRms']-report['before']['holdout']['normalizedRms']>p['maxHoldoutWorseningNormalized']:reasons.append('HOLDOUT_REGRESSION')
    improvement=fit_before['normalizedRms']-after['fit']['normalizedRms']
    if solver['optimized'] and improvement<p['minImprovementNormalized']:reasons.append('NO_FIT_IMPROVEMENT')
    matrix=updated_pose(bundle['frame'].c2w,candidate)
    report.update({'after':after,'solver':solver,'candidateC2w':matrix.tolist(),'candidateC2wSha256':canonical_hash(matrix.tolist()),
                   'deltaCameraLocal':{'translationM':candidate[:3].tolist(),'rotationVectorRad':candidate[3:].tolist()},
                   'translationMagnitudeM':float(np.linalg.norm(candidate[:3])),'rotationMagnitudeDeg':float(np.degrees(np.linalg.norm(candidate[3:]))),
                   'fitImprovementNormalized':improvement})
    if not reasons:report['status']='UNCHANGED_WITHIN_STATED_UNCERTAINTY_REVIEW_REQUIRED' if not solver['optimized'] else 'LOCAL_POSE_CANDIDATE_REVIEW_REQUIRED'
    verify_bindings(bundle);_fail(producer_binding('camera')==provenance,'PRODUCER_CHANGED_DURING_RUN');return report


def verify_camera_review(report,report_digest,review):
    verify_producer_binding(report,'camera')
    _fail(review.get('schema')=='camera-control-refinement-review-v1','INVALID_CAMERA_REVIEW_SCHEMA')
    identity=review.get('reviewer',{});author=report['author'];policy_sha=sha256(REVIEW_POLICY_PATH)
    _fail(identity.get('role')=='reviewer' and identity.get('toolPolicy')=='readonly'
          and identity.get('toolPolicyId')==REVIEW_POLICY_ID and identity.get('toolPolicySha256')==policy_sha
          and report.get('reviewPolicy')=={'policyId':REVIEW_POLICY_ID,'sha256':policy_sha},'INVALID_CAMERA_REVIEW_POLICY')
    operations=_read_json(REVIEW_POLICY_PATH)['policies'].get(REVIEW_POLICY_ID,{}).get('operations',[])
    _fail(set(operations)=={'scene:read','evidence:read','pointcloud:query','render:write-derivative','pipeline:submit-verdict'},'REVIEW_POLICY_NOT_READONLY')
    _fail(all(isinstance(identity.get(k),str) and identity[k] for k in ('actorId','runId'))
          and identity['actorId']!=author['actorId'] and identity['runId']!=author['runId'],'AUTHOR_SELF_REVIEW')
    _fail(review.get('reportSha256')==report_digest and review.get('inputSha256')==report['inputSha256']
          and review.get('frameId')==report['frameId'] and review.get('imageSha256')==report['imageSha256'],'STALE_CAMERA_REVIEW_BINDING')
    _fail(set(review.get('inspectedControlIds',[]))=={c['id'] for split in report['before'].values() for c in split['controls']},'INCOMPLETE_CONTROL_REVIEW')
    _fail(isinstance(review.get('reasons'),list) and review['reasons'],'MISSING_REVIEW_REASON');verify_bindings(report)
    verdict=review.get('verdict');_fail(verdict in ('ACCEPT_WITHIN_STATED_CONTROL_UNCERTAINTY','REJECT','INSUFFICIENT_EVIDENCE'),'INVALID_CAMERA_REVIEW_VERDICT')
    accepted=verdict=='ACCEPT_WITHIN_STATED_CONTROL_UNCERTAINTY'
    if accepted:_fail(report['status'] in ('LOCAL_POSE_CANDIDATE_REVIEW_REQUIRED','UNCHANGED_WITHIN_STATED_UNCERTAINTY_REVIEW_REQUIRED'),'REJECTED_CAMERA_CANNOT_BE_ACCEPTED')
    return {'schema':'camera-control-reviewed-receipt-v1','frameId':report['frameId'],'imageSha256':report['imageSha256'],
            'reportSha256':report_digest,'inputSha256':report['inputSha256'],'source':report['source'],'reviewer':identity,
            'acceptedWithinStatedControlUncertainty':accepted,'candidateC2w':report.get('candidateC2w') if accepted else None,
            'candidateC2wSha256':report.get('candidateC2wSha256') if accepted else None,'scope':report['scope'],
            'rawCloudBytesRehashed':False,'status':'CONDITIONAL_FRAME_REVIEW_RECORDED'}
