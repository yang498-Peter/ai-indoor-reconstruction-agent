"""Source-bound multi-view cuboid fitting; image annotations are the observations.

This is a presentation measurement helper, not a Scene V2 acceptance evaluator.
World coordinates are metres and Z-up. Cuboid corners use bit 0=x, 1=y, 2=z;
yaw rotates around +Z. Camera poses and projection use photo_projection.Frame.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import math
import os
import platform
from pathlib import Path
import tempfile

import numpy as np
from PIL import Image,ImageDraw
from scipy.optimize import least_squares
import scipy

from photo_projection import CameraModel, Frame, project_points

INPUT_SCHEMA = 'multiview-refinement-input-v1'
REPORT_SCHEMA = 'multiview-refinement-report-v1'
PARAMETERS = ['centerX', 'centerY', 'centerZ', 'sizeX', 'sizeY', 'sizeZ', 'yaw']
EDGES = tuple((i, i ^ bit) for bit in (1, 2, 4) for i in range(8) if not i & bit)
REVIEW_POLICY_PATH = Path(__file__).resolve().parents[1]/'schemas/tool-policies-v1.json'
REVIEW_POLICY_ID = 'reviewer-readonly-v1'


def producer_binding(kind='object'):
    root=Path(__file__).resolve().parents[1]
    names=['scene-core/multiview_fit.py','scene-core/photo_projection.py']
    names+=['scripts/multiview_refinement.py'] if kind=='object' else ['scene-core/camera_control_fit.py','scripts/camera_control_refinement.py']
    return {'producerSha256':{name:sha256(root/name) for name in names},
            'softwareVersions':{'Python':platform.python_version(),'numpy':np.__version__,'scipy':scipy.__version__}}


def verify_producer_binding(report,kind='object'):
    recorded=report.get('provenance',{})
    _fail(recorded.get('producerSha256')==producer_binding(kind)['producerSha256'],'STALE_PRODUCER_BINDING')
    versions=recorded.get('softwareVersions',{})
    _fail(all(isinstance(versions.get(name),str) and bool(versions[name]) for name in ('Python','numpy','scipy')),'MISSING_RUNTIME_PROVENANCE')


class MultiviewError(ValueError):
    def __init__(self, code, message='', **details):
        self.code, self.details = code, details
        super().__init__(message or code)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode('utf-8')).hexdigest()


def _fail(condition, code, **details):
    if not condition:
        raise MultiviewError(code, **details)


def _finite(value, shape, code):
    try:
        arr = np.asarray(value, dtype=float)
    except (TypeError, ValueError):
        raise MultiviewError(code)
    _fail(arr.shape == shape and np.isfinite(arr).all(), code)
    return arr


def parameters(value):
    return np.concatenate((_finite(value.get('center'), (3,), 'INVALID_CENTER'),
                           _finite(value.get('size'), (3,), 'INVALID_SIZE'),
                           _finite([value.get('yaw')], (1,), 'INVALID_YAW')))


def unpack(values):
    a = np.asarray(values, dtype=float)
    return {'center': a[:3].tolist(), 'size': a[3:6].tolist(), 'yaw': float(a[6])}


def corners(values):
    a = np.asarray(values, dtype=float)
    signs = np.array([[1 if i & bit else -1 for bit in (1, 2, 4)] for i in range(8)])
    c, s = math.cos(a[6]), math.sin(a[6])
    rotation = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
    return signs * a[3:6] / 2 @ rotation.T + a[:3]


def _read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _fail(key not in result, 'DUPLICATE_JSON_KEY', key=key)
            result[key] = value
        return result
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda x: (_ for _ in ()).throw(MultiviewError('NONFINITE_JSON')))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MultiviewError('INVALID_JSON', str(exc)) from exc


def load_bundle(path):
    path = Path(path).resolve(strict=True)
    data = _read_json(path)
    checked = validate_bundle(data, path.parent)
    checked['inputPath'] = str(path)
    checked['inputSha256'] = sha256(path)
    checked['bindings'].append({'path': str(path), 'sha256': checked['inputSha256']})
    return checked


def validate_bundle(data, base):
    """Validate image identity, calibration, splits and independently sourced marks.

    Inspecting an image's bytes does not establish calibration quality. The caller
    must provide its verified calibration evidence; this tool records that claim.
    """
    _fail(isinstance(data, dict) and data.get('schema') == INPUT_SCHEMA, 'INVALID_SCHEMA')
    _fail(data.get('units') == 'metre' and data.get('upAxis') == 'Z', 'INVALID_WORLD_FRAME')
    author = data.get('author', {})
    _fail(all(isinstance(author.get(k), str) and author[k] for k in ('actorId', 'runId')), 'INVALID_AUTHOR')
    source = data.get('source', {})
    _fail(isinstance(source.get('coordinateFrame'), str) and bool(source['coordinateFrame']), 'MISSING_COORDINATE_FRAME')
    _fail(isinstance(source.get('modelSha256'), str) and len(source['modelSha256']) == 64
          and all(c in '0123456789abcdef' for c in source['modelSha256']), 'INVALID_MODEL_SHA256')
    policy = copy.deepcopy(data.get('policy', {}))
    required = ['minFitViews', 'minHoldoutViews', 'minCameraBaselineM', 'maxRmsPx',
                'maxHoldoutWorseningPx', 'minImprovementPx', 'lossScalePx', 'maxNfev', 'conditionLimit']
    _fail(all(k in policy for k in required), 'INCOMPLETE_POLICY', required=required)
    _fail(all(isinstance(policy[k], (int, float)) and not isinstance(policy[k], bool)
              and math.isfinite(policy[k]) and policy[k] >= 0 for k in required), 'INVALID_POLICY')
    _fail(policy['minFitViews'] >= 2 and policy['minHoldoutViews'] >= 1 and policy['minCameraBaselineM'] > 0
          and policy['maxRmsPx'] > 0 and policy['lossScalePx'] > 0 and policy['maxNfev'] >= 1
          and policy['conditionLimit'] > 1, 'UNSAFE_POLICY')
    policy.setdefault('nearM', .01)
    policy.setdefault('rankRelativeTolerance', 1e-7)
    policy.setdefault('minHoldoutSeparationM', policy['minCameraBaselineM'])
    policy.setdefault('maxPerViewRmsPx', policy['maxRmsPx'])
    _fail(0 < policy['nearM'] < 1 and 0 < policy['rankRelativeTolerance'] < 1, 'INVALID_NUMERICAL_POLICY')
    _fail(isinstance(policy['minHoldoutSeparationM'], (int,float)) and math.isfinite(policy['minHoldoutSeparationM'])
          and policy['minHoldoutSeparationM'] > 0 and isinstance(policy['maxPerViewRmsPx'], (int,float))
          and math.isfinite(policy['maxPerViewRmsPx']) and policy['maxPerViewRmsPx'] > 0, 'INVALID_VIEW_POLICY')
    objects = data.get('objects', [])
    _fail(isinstance(objects, list) and bool(objects), 'NO_OBJECTS')
    object_map = {}
    for obj in objects:
        oid = obj.get('id')
        _fail(isinstance(oid, str) and oid and oid not in object_map, 'DUPLICATE_OR_INVALID_OBJECT_ID')
        a = parameters(obj.get('initial', {}))
        lower = _finite(obj.get('bounds', {}).get('lower'), (7,), 'INVALID_LOWER_BOUNDS')
        upper = _finite(obj.get('bounds', {}).get('upper'), (7,), 'INVALID_UPPER_BOUNDS')
        vary = obj.get('vary', [True] * 7)
        _fail(isinstance(vary, list) and len(vary) == 7 and all(type(v) is bool for v in vary), 'INVALID_VARY')
        vary = np.asarray(vary, dtype=bool)
        _fail(np.all(a[3:6] > 0) and np.all(lower[3:6] > 0), 'NONPOSITIVE_SIZE')
        _fail(np.all(lower <= a) and np.all(a <= upper) and np.all(lower <= upper)
              and np.all(upper[vary] > lower[vary]) and vary.any(), 'INVALID_PARAMETER_BOUNDS')
        object_map[oid] = {'raw': copy.deepcopy(obj), 'initial': a, 'lower': lower,
                           'upper': upper, 'vary': vary}
    views = data.get('views', [])
    _fail(isinstance(views, list) and views, 'NO_VIEWS')
    view_map, identities, groups, bindings = {}, {}, {}, []
    for v in views:
        vid, split, group = v.get('id'), v.get('split'), v.get('captureGroup')
        _fail(isinstance(vid, str) and vid and vid not in view_map, 'DUPLICATE_OR_INVALID_VIEW_ID')
        _fail(split in ('fit', 'holdout') and isinstance(group, str) and group, 'INVALID_VIEW_PARTITION')
        image = v.get('image', {})
        _fail(isinstance(image.get('path'), str) and bool(image['path']), 'MISSING_IMAGE')
        path = (Path(base) / image['path']).resolve(strict=True)
        digest = sha256(path)
        _fail(digest == image.get('sha256'), 'IMAGE_HASH_MISMATCH', viewId=vid)
        _fail(digest not in identities, 'DUPLICATE_IMAGE_CONTENT', viewId=vid, otherView=identities.get(digest))
        _fail(group not in groups or groups[group] == split, 'CAPTURE_GROUP_LEAKAGE', captureGroup=group)
        identities[digest], groups[group] = vid, split
        cam_data = copy.deepcopy(v.get('camera', {}))
        try:
            cam_data['k'] = tuple(cam_data.get('k', (0, 0, 0, 0)))
            camera = CameraModel(**cam_data)
        except (ValueError, TypeError) as exc:
            raise MultiviewError('INVALID_CAMERA', str(exc), viewId=vid) from exc
        _fail(camera.is_valid() and camera.model in ('pinhole', 'fisheye-equidistant')
              and len(camera.k) == 4 and np.isfinite(camera.k).all(), 'INVALID_CAMERA', viewId=vid)
        with Image.open(path) as im:
            _fail(im.size == (camera.width, camera.height), 'IMAGE_DIMENSION_MISMATCH', viewId=vid)
        matrix = _finite(v.get('c2w'), (4, 4), 'INVALID_CAMERA_POSE')
        _fail(np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-8)
              and np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), atol=1e-5)
              and abs(np.linalg.det(matrix[:3, :3]) - 1) < 1e-5, 'NONRIGID_CAMERA_POSE', viewId=vid)
        calibration = v.get('calibration', {})
        _fail(calibration.get('status') in ('VERIFIED', 'DIAGNOSTIC', 'UNVERIFIED'), 'INVALID_CALIBRATION_STATUS', viewId=vid)
        if calibration['status'] == 'VERIFIED':
            evidence = calibration.get('evidenceBindings', [])
            residual = calibration.get('independentFeatureResidualPx', {})
            _fail(isinstance(calibration.get('evidence'), str) and bool(calibration['evidence'])
                  and isinstance(evidence, list) and bool(evidence)
                  and type(residual.get('samples')) is int and residual['samples'] > 0
                  and all(isinstance(residual.get(k), (int, float)) and math.isfinite(residual[k]) and residual[k] >= 0
                          for k in ('rms', 'p95'))
                  and isinstance(calibration.get('sourceSha256'), str) and len(calibration['sourceSha256']) == 64,
                  'INCOMPLETE_CALIBRATION_EVIDENCE', viewId=vid)
            _fail(calibration['sourceSha256'] in {entry.get('sha256') for entry in evidence},
                  'UNBOUND_CALIBRATION_SOURCE', viewId=vid)
            for entry in evidence:
                p = (Path(base)/entry['path']).resolve(strict=True)
                _fail(sha256(p) == entry.get('sha256'), 'CALIBRATION_EVIDENCE_HASH_MISMATCH', viewId=vid)
                bindings.append({'path': str(p), 'sha256': entry['sha256']})
        annotations = v.get('annotations') or []
        _fail(isinstance(annotations, list), 'INVALID_ANNOTATIONS')
        seen_annotations = set()
        for mark in annotations:
            _fail(mark.get('objectId') in object_map, 'UNKNOWN_ANNOTATED_OBJECT', viewId=vid)
            provenance = mark.get('provenance', {})
            _fail(provenance.get('kind') in ('manual_image', 'independent_image_edge')
                  and isinstance(provenance.get('annotator'), str) and bool(provenance['annotator'])
                  and provenance.get('sourceImageSha256') == digest,
                  'UNSUPPORTED_ANNOTATION_PROVENANCE', viewId=vid)
            _fail(not provenance.get('derivedFromModel', False), 'MODEL_DERIVED_TARGET', viewId=vid)
            uncertainty = mark.get('uncertaintyPx')
            _fail(isinstance(uncertainty, (int, float)) and math.isfinite(uncertainty) and uncertainty > 0,
                  'INVALID_ANNOTATION_UNCERTAINTY', viewId=vid)
            pixels = np.asarray(mark.get('pixels'), dtype=float)
            _fail(pixels.ndim == 2 and pixels.shape[1] == 2 and np.isfinite(pixels).all(), 'INVALID_PIXELS')
            _fail(np.all(pixels >= 0) and np.all(pixels[:, 0] <= camera.width - 1)
                  and np.all(pixels[:, 1] <= camera.height - 1), 'PIXELS_OUTSIDE_IMAGE', viewId=vid)
            if mark.get('kind') == 'corner':
                _fail(type(mark.get('corner')) is int and 0 <= mark['corner'] < 8 and len(pixels) == 1,
                      'INVALID_CORNER_MARK')
                key = (mark['objectId'], 'corner', mark['corner'])
            elif mark.get('kind') == 'edge':
                edge = mark.get('edge', [])
                _fail(isinstance(edge, list) and len(edge) == 2 and all(type(i) is int for i in edge)
                      and tuple(sorted(edge)) in EDGES and len(pixels) >= 2
                      and np.linalg.norm(pixels[-1] - pixels[0]) > 1e-6, 'INVALID_EDGE_MARK')
                key = (mark['objectId'], 'edge', tuple(sorted(edge)))
            else:
                raise MultiviewError('INVALID_MARK_KIND')
            _fail(key not in seen_annotations, 'DUPLICATE_ANNOTATION', viewId=vid)
            seen_annotations.add(key)
        frame = Frame(vid, image['path'], matrix, camera, image_path=path)
        view_map[vid] = {'raw': copy.deepcopy(v), 'frame': frame, 'split': split,
                         'calibrationVerified': calibration['status'] == 'VERIFIED',
                         'annotations': copy.deepcopy(annotations)}
        bindings.append({'path': str(path), 'sha256': digest})
    source_bindings=data.get('sourceBindings', [])
    _fail(isinstance(source_bindings,list) and any(isinstance(entry,dict) and entry.get('sha256')==source['modelSha256']
                                                 for entry in source_bindings), 'UNBOUND_SOURCE_MODEL')
    for source_binding in source_bindings:
        p = (Path(base) / source_binding['path']).resolve(strict=True)
        _fail(sha256(p) == source_binding.get('sha256'), 'SOURCE_HASH_MISMATCH', path=str(p))
        bindings.append({'path': str(p), 'sha256': source_binding['sha256']})
    return {'data': copy.deepcopy(data), 'objects': object_map, 'views': view_map,
            'policy': policy, 'bindings': bindings, 'contentSha256': canonical_hash(data)}


def verify_bindings(bundle):
    for binding in bundle['bindings']:
        _fail(sha256(binding['path']) == binding['sha256'], 'INPUT_CHANGED_DURING_RUN', path=binding['path'])


def _curve_distances(pixels, curve):
    """Signed point-to-projected-polyline distances, retaining partial visibility."""
    if curve.ndim==3:start,delta=curve[:,0],curve[:,1]-curve[:,0]
    else:start,delta=curve[:-1],np.diff(curve,axis=0)
    length2 = np.einsum('ij,ij->i', delta, delta)
    length2 = np.maximum(length2, 1e-16)
    out = []
    for pixel in pixels:
        t = np.clip(np.einsum('ij,ij->i', pixel - start, delta) / length2, 0, 1)
        difference = pixel - (start + t[:, None] * delta)
        distance2 = np.einsum('ij,ij->i', difference, difference)
        index = int(np.argmin(distance2))
        sign = np.sign(delta[index, 0] * difference[index, 1] - delta[index, 1] * difference[index, 0])
        out.append(math.sqrt(distance2[index]) * (sign or 1.))
    return np.asarray(out)


def _project_edge(frame, a, b, near):
    """Clip a physical 3D edge at the near plane, then project its visible part.

    The complete cuboid edge need not be visible. Image-rectangle clipping keeps
    the residual on finite projected segments, without bridging off-image gaps.
    """
    a,b=np.asarray(a).copy(),np.asarray(b).copy()
    depth=frame.world_to_camera(np.array([a,b]))[:,2]
    threshold=near*(1+1e-6)
    if np.all(depth<=threshold):return np.empty((0,2,2))
    if depth[0]<=threshold:a=a+(b-a)*((threshold-depth[0])/(depth[1]-depth[0]))
    elif depth[1]<=threshold:b=a+(b-a)*((threshold-depth[0])/(depth[1]-depth[0]))
    points=a+np.linspace(0,1,65)[:,None]*(b-a)
    uv,_,front=project_points(frame,points,near=near)
    if not front.all() or not np.isfinite(uv).all():return np.empty((0,2,2))
    segments=[];xmax=frame.camera.width-1;ymax=frame.camera.height-1
    for start,end in zip(uv[:-1],uv[1:]):
        delta=end-start;lo,hi=0.,1.;valid=True
        for p,q in [(-delta[0],start[0]),(delta[0],xmax-start[0]),
                    (-delta[1],start[1]),(delta[1],ymax-start[1])]:
            if abs(p)<1e-14:
                if q<0:valid=False;break
            else:
                t=q/p
                if p<0:lo=max(lo,t)
                else:hi=min(hi,t)
                if lo>hi:valid=False;break
        if valid:
            segment=np.array([start+lo*delta,start+hi*delta])
            if np.linalg.norm(segment[1]-segment[0])>1e-10:segments.append(segment)
    return np.asarray(segments).reshape(-1,2,2)


def _mark_residual(values, view, mark, near):
    xyz = corners(values)
    if mark['kind'] == 'corner':
        points = xyz[[mark['corner']]]
    else:
        a, b = xyz[mark['edge']]
        curve=_project_edge(view['frame'],a,b,near)
        if not len(curve):return np.full(len(mark['pixels']),1e6),False
        return _curve_distances(np.asarray(mark['pixels']),curve),True
    uv, depth, front = project_points(view['frame'], points, near=near)
    count = 2 if mark['kind'] == 'corner' else len(mark['pixels'])
    if not front.all() or not np.isfinite(uv).all():
        return np.full(count, 1e6 + max(0, near - float(depth.min())) * 1e4), False
    pixels = np.asarray(mark['pixels'])
    return (uv - pixels).ravel(), True


def observations(bundle, oid, split):
    return [(v, a) for v in bundle['views'].values() if v['split'] == split
            for a in v['annotations'] if a['objectId'] == oid]


def residuals(values, marks, near, weighted=False):
    result = []
    for view, mark in marks:
        r, _ = _mark_residual(values, view, mark, near)
        result.extend(r / mark['uncertaintyPx'] if weighted else r)
    return np.asarray(result, dtype=float)


def score(values, marks, near):
    rows, distances = [], []
    for view, mark in marks:
        r, valid = _mark_residual(values, view, mark, near)
        # Corner observations are one Euclidean pixel error; edge annotations
        # contribute one point-to-curve distance per supplied visible sample.
        errors = [float(np.linalg.norm(r))] if mark['kind'] == 'corner' else np.abs(r).tolist()
        distances.extend(errors)
        rows.append({'viewId': view['frame'].frame_id, 'kind': mark['kind'],
                     'feature': mark.get('corner', mark.get('edge')),
                     'validProjection': valid, 'errorsPx': errors})
    views = {}
    for row in rows:
        views.setdefault(row['viewId'], []).extend(row['errorsPx'])
    def summary(items):
        a = np.asarray(items)
        return {'samples': len(items), 'rmsPx': float(np.sqrt(np.mean(a*a))),
                'medianPx': float(np.median(a)), 'p95Px': float(np.quantile(a, .95)), 'maxPx': float(a.max())} if len(items) else None
    return {'summary': summary(distances), 'views': {key: summary(value) for key, value in views.items()},
            'observations': rows, 'validProjections': all(r['validProjection'] for r in rows)}


def _baseline(views):
    centers = [v['frame'].center for v in views]
    return max((float(np.linalg.norm(a-b)) for a in centers for b in centers), default=0.)


def fit_object(bundle, oid):
    obj, policy = bundle['objects'][oid], bundle['policy']
    initial = obj['initial']; fit = observations(bundle, oid, 'fit'); holdout = observations(bundle, oid, 'holdout')
    fit_views = {v['frame'].frame_id: v for v, _ in fit}
    holdout_views = {v['frame'].frame_id: v for v, _ in holdout}
    row = {'objectId': oid, 'room': obj['raw'].get('room'), 'status': 'REJECTED', 'reasons': [],
           'initial': unpack(initial), 'candidate': None, 'reviewStatus': 'NOT_RUN',
           'fitViewIds': sorted(fit_views), 'holdoutViewIds': sorted(holdout_views),
           'fitCameraBaselineM': _baseline(fit_views.values()),
           'cameraCalibration': {vid: view['raw']['calibration'] for vid, view in {**fit_views, **holdout_views}.items()},
           'annotatedViews': sorted(set(fit_views) | set(holdout_views)),
           'before': {'fit': score(initial, fit, policy['nearM']), 'holdout': score(initial, holdout, policy['nearM'])}}
    reasons = row['reasons']
    if len(fit_views) < policy['minFitViews']:reasons.append('INSUFFICIENT_FIT_VIEWS')
    if len(holdout_views) < policy['minHoldoutViews']:reasons.append('INSUFFICIENT_HOLDOUT_VIEWS')
    if row['fitCameraBaselineM'] < policy['minCameraBaselineM']:reasons.append('INSUFFICIENT_CAMERA_BASELINE')
    holdout_separation = min((float(np.linalg.norm(a['frame'].center-b['frame'].center))
                              for a in holdout_views.values() for b in fit_views.values()), default=0.)
    row['holdoutMinimumCameraSeparationM'] = holdout_separation
    row['crossSplitCameraPairs'] = [
        {'fitViewId': fit_id, 'holdoutViewId': holdout_id,
         'distanceM': float(np.linalg.norm(a['frame'].center-b['frame'].center)),
         'rotationDifferenceDeg': float(math.degrees(math.acos(float(np.clip(
             (np.trace(a['frame'].c2w[:3,:3].T@b['frame'].c2w[:3,:3])-1)/2, -1, 1)))))}
        for fit_id,b in fit_views.items() for holdout_id,a in holdout_views.items()]
    if holdout_separation < policy['minHoldoutSeparationM']:reasons.append('HOLDOUT_CAMERA_NOT_INDEPENDENT')
    if not row['before']['fit']['validProjections'] or not row['before']['holdout']['validProjections']:reasons.append('INVALID_INITIAL_PROJECTION')
    if len(residuals(initial, fit, policy['nearM'])) < int(obj['vary'].sum()):reasons.append('INSUFFICIENT_CONSTRAINTS')
    if reasons:
        row['status'] = 'INSUFFICIENT_EVIDENCE';return row
    active = obj['vary']
    def restore(free):
        full = initial.copy();full[active] = free;return full
    # Only fit observations are closed over here. Holdout values are evaluated
    # once after optimization and never used by the objective or retry search.
    result = least_squares(lambda x: residuals(restore(x), fit, policy['nearM'], True),
                           initial[active], bounds=(obj['lower'][active], obj['upper'][active]),
                           loss='soft_l1', f_scale=policy['lossScalePx'], max_nfev=int(policy['maxNfev']),
                           x_scale='jac', ftol=1e-10, xtol=1e-10, gtol=1e-10)
    candidate = restore(result.x)
    after_fit = score(candidate, fit, policy['nearM']);after_holdout = score(candidate, holdout, policy['nearM'])
    row['candidate'] = unpack(candidate);row['after'] = {'fit': after_fit, 'holdout': after_holdout}
    scale = np.linalg.norm(result.jac, axis=0)
    normalized_jacobian = result.jac / np.where(scale > 1e-14, scale, 1.)
    singular = np.linalg.svd(normalized_jacobian, compute_uv=False)
    rank = int(np.sum(singular > singular[0] * policy['rankRelativeTolerance'])) if len(singular) and singular[0] else 0
    condition = float(singular[0] / singular[-1]) if len(singular) and singular[-1] > 0 else None
    row['solver'] = {'success': bool(result.success), 'termination': str(result.message), 'nfev': result.nfev,
                     'activeParameters': [p for p, free in zip(PARAMETERS, active) if free],
                     'jacobianRank': rank, 'normalizedJacobianCondition': condition,
                     'singularValues': singular.tolist()}
    row['parameterChange'] = {'centerDisplacementM': float(np.linalg.norm(candidate[:3]-initial[:3])),
                              'sizeDeltaM': (candidate[3:6]-initial[3:6]).tolist(),
                              'yawDeltaRad': float(candidate[6]-initial[6])}
    if not result.success:reasons.append('SOLVER_DID_NOT_CONVERGE')
    if np.any(result.active_mask):reasons.append('PARAMETER_BOUND_REACHED')
    if not all(v['calibrationVerified'] for v in list(fit_views.values())+list(holdout_views.values())):reasons.append('CAMERA_UNVERIFIED')
    if rank < active.sum() or condition is None or condition > policy['conditionLimit']:reasons.append('DEGENERATE_FIT')
    if not after_fit['validProjections'] or not after_holdout['validProjections']:reasons.append('INVALID_CANDIDATE_PROJECTION')
    if after_fit['summary']['rmsPx'] > policy['maxRmsPx']:reasons.append('FIT_ERROR_EXCEEDS_POLICY')
    if after_holdout['summary']['rmsPx'] > policy['maxRmsPx']:reasons.append('HOLDOUT_ERROR_EXCEEDS_POLICY')
    row['perViewPolicyFailures'] = [{'split': split, 'viewId': vid, 'rmsPx': values['rmsPx']}
                                   for split, scored in [('fit',after_fit),('holdout',after_holdout)]
                                   for vid,values in scored['views'].items()
                                   if values['rmsPx'] > policy['maxPerViewRmsPx']]
    if row['perViewPolicyFailures']:reasons.append('VIEW_ERROR_EXCEEDS_POLICY')
    improvement = row['before']['fit']['summary']['rmsPx'] - after_fit['summary']['rmsPx']
    holdout_change = after_holdout['summary']['rmsPx'] - row['before']['holdout']['summary']['rmsPx']
    row['fitImprovementPx'] = improvement;row['holdoutChangePx'] = holdout_change
    if holdout_change > policy['maxHoldoutWorseningPx']:reasons.append('HOLDOUT_REGRESSION')
    if improvement < policy['minImprovementPx']:
        if max(row['before'][s]['summary']['rmsPx'] for s in ('fit', 'holdout')) <= policy['maxRmsPx'] and not reasons:
            row['status'] = 'UNCHANGED_REVIEW_REQUIRED';row['candidate'] = unpack(initial)
            row['after'] = copy.deepcopy(row['before']);row['parameterChange'] = {'centerDisplacementM': 0., 'sizeDeltaM': [0.,0.,0.], 'yawDeltaRad': 0.}
            row['fitImprovementPx'] = 0.;row['holdoutChangePx'] = 0.;return row
        reasons.append('NO_FIT_IMPROVEMENT')
    row['status'] = 'REJECTED' if reasons else 'CANDIDATE_REVIEW_REQUIRED'
    return row


def fit_bundle(bundle):
    verify_bindings(bundle)
    provenance=producer_binding()
    rows = []
    for oid in bundle['objects']:
        try:row = fit_object(bundle, oid)
        except (ValueError, FloatingPointError, np.linalg.LinAlgError) as exc:
            row = {'objectId': oid, 'room': bundle['objects'][oid]['raw'].get('room'), 'status': 'REJECTED',
                   'reasons': ['FIT_EXECUTION_ERROR'], 'error': str(exc), 'reviewStatus': 'NOT_RUN'}
        rows.append(row)
    verify_bindings(bundle)
    _fail(producer_binding()==provenance,'PRODUCER_CHANGED_DURING_RUN')
    return {'schema': REPORT_SCHEMA, 'inputSha256': bundle.get('inputSha256', bundle['contentSha256']),
            'source': bundle['data']['source'], 'author': bundle['data']['author'],
            'provenance':provenance,
            'bindings': copy.deepcopy(bundle['bindings']),
            'reviewPolicy': {'policyId': REVIEW_POLICY_ID, 'sha256': sha256(REVIEW_POLICY_PATH)},
            'policy': bundle['policy'], 'objects': rows,
            'status': 'REVIEW_REQUIRED', 'reviewStatus': 'NOT_RUN',
            'allObjectsProducedCandidates': all(r['status'] in ('CANDIDATE_REVIEW_REQUIRED','UNCHANGED_REVIEW_REQUIRED') for r in rows),
            'limitations': ['Pixel residuals are against declared image annotations, not independent metric accuracy.',
                            'Source calibration and annotation semantics require independent visual review.',
                            'No candidate is applied to model geometry or authority by this tool.']}


def inspect_bundle(bundle):
    verify_bindings(bundle)
    return {'schema': 'multiview-refinement-inspection-v1', 'inputSha256': bundle.get('inputSha256', bundle['contentSha256']),
            'provenance':producer_binding(),
            'objects': [{ 'objectId': oid, 'fitViewIds': sorted({v['frame'].frame_id for v,_ in observations(bundle,oid,'fit')}),
                          'holdoutViewIds': sorted({v['frame'].frame_id for v,_ in observations(bundle,oid,'holdout')})}
                        for oid in bundle['objects']],
            'views': len(bundle['views']), 'bindingsVerified': len(bundle['bindings']),
            'status': 'INPUT_INSPECTED_NOT_ACCEPTED'}


def verify_review(report, report_sha256, review):
    verify_producer_binding(report)
    _fail(review.get('schema') == 'multiview-refinement-review-v1', 'INVALID_REVIEW_SCHEMA')
    identity = review.get('reviewer', {})
    _fail(identity.get('role') == 'reviewer' and identity.get('toolPolicy') == 'readonly'
          and all(isinstance(identity.get(k),str) and identity[k] for k in ('actorId','runId')), 'INVALID_REVIEW_IDENTITY')
    author = report['author']
    _fail(identity['actorId'] != author['actorId'] and identity['runId'] != author['runId'], 'AUTHOR_SELF_REVIEW')
    _fail(review.get('fitReportSha256') == report_sha256 and review.get('inputSha256') == report['inputSha256'], 'STALE_REVIEW_BINDING')
    policy_sha = sha256(REVIEW_POLICY_PATH)
    _fail(report.get('reviewPolicy') == {'policyId': REVIEW_POLICY_ID, 'sha256': policy_sha}
          and identity.get('toolPolicyId') == REVIEW_POLICY_ID and identity.get('toolPolicySha256') == policy_sha,
          'REVIEW_POLICY_BINDING_MISMATCH')
    checked_policy = _read_json(REVIEW_POLICY_PATH)['policies'].get(REVIEW_POLICY_ID, {})
    _fail(set(checked_policy.get('operations', [])) == {'scene:read','evidence:read','pointcloud:query',
                                                     'render:write-derivative','pipeline:submit-verdict'},
          'REVIEW_POLICY_NOT_READONLY')
    _fail(isinstance(report.get('bindings'), list) and bool(report['bindings']), 'MISSING_REPORT_BINDINGS')
    verify_bindings(report)
    verdicts = review.get('objects', [])
    _fail(isinstance(verdicts,list), 'INVALID_REVIEW_OBJECTS')
    indexed = {}
    for item in verdicts:
        oid = item.get('objectId');_fail(oid not in indexed, 'DUPLICATE_REVIEW_OBJECT')
        _fail(item.get('verdict') in ('ACCEPT','REJECT','INSUFFICIENT_EVIDENCE'), 'INVALID_REVIEW_VERDICT')
        _fail(isinstance(item.get('reasons'),list) and bool(item['reasons']), 'MISSING_REVIEW_REASON')
        indexed[oid] = item
    _fail(set(indexed) == {r['objectId'] for r in report['objects']}, 'INCOMPLETE_REVIEW_COVERAGE')
    accepted = []
    for row in report['objects']:
        item = indexed[row['objectId']]
        _fail(set(item.get('inspectedViews', [])) == set(row.get('annotatedViews', [])), 'INCOMPLETE_REVIEW_VIEWS', objectId=row['objectId'])
        if item['verdict'] == 'ACCEPT':
            _fail(row['status'] in ('CANDIDATE_REVIEW_REQUIRED','UNCHANGED_REVIEW_REQUIRED'), 'REJECTED_FIT_CANNOT_BE_ACCEPTED', objectId=row['objectId'])
            accepted.append(row['objectId'])
    return {'reviewer': identity, 'acceptedObjectIds': accepted,
            'allObjectsReviewedAndAccepted': len(accepted) == len(report['objects']),
            'status': 'REVIEW_RECORDED_NOT_AUTHORITY_ACCEPTANCE'}


def atomic_json(path, value, *, overwrite=False):
    path = Path(path)
    _fail(overwrite or not path.exists(), 'OUTPUT_EXISTS', path=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            name = stream.name;json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False);stream.write('\n')
            stream.flush();os.fsync(stream.fileno())
        if overwrite:os.replace(name,path)
        else:
            # Hard-link creation is atomic and refuses an existing destination;
            # use replace only for explicitly authorized overwriting.
            os.link(name,path);os.unlink(name)
        name = None
    finally:
        if name is not None and Path(name).exists():Path(name).unlink()


def protect_output(path, protected_paths, *, directory=False):
    """Reject output aliases of inputs and directories containing bound inputs."""
    target=Path(path).resolve(strict=False)
    for raw in protected_paths:
        source=Path(raw).resolve(strict=False)
        same=target==source
        if not same and target.exists() and source.exists():
            same=target.samefile(source)
        _fail(not same and (not directory or target not in source.parents),
              'OUTPUT_OVERWRITES_INPUT',path=str(path),protected=str(source))


def render_report(bundle, report, output_dir, panel_width=800):
    """Render source-derived private review pairs, never substitute them for marks.

    Errors remain in full-resolution pixels. Image resizing is solely for review.
    No raw image is written back or copied into a public package by this helper.
    """
    verify_bindings(bundle)
    verify_producer_binding(report)
    _fail(report.get('schema') == REPORT_SCHEMA and report.get('inputSha256') == bundle.get('inputSha256',bundle['contentSha256'])
          and report.get('source') == bundle['data']['source'], 'REPORT_INPUT_MISMATCH')
    output_dir=Path(output_dir)
    protect_output(output_dir,[entry['path'] for entry in bundle['bindings']+report.get('bindings',[])],directory=True)
    _fail(not output_dir.exists() and not output_dir.is_symlink(),'OUTPUT_EXISTS',path=str(output_dir))
    _fail(type(panel_width) is int and 200 <= panel_width <= 1600,'INVALID_PANEL_WIDTH')
    rows={row['objectId']:row for row in report['objects']}
    _fail(set(rows)==set(bundle['objects']),'REPORT_OBJECT_COVERAGE_MISMATCH')
    # Build outside the final path, then rename the complete directory. Refuse
    # existing output so an old review cannot be silently replaced.
    output_dir.parent.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix='.multiview-render-',dir=output_dir.parent))
    sections=[];images=[]
    try:
        for oid,obj in bundle['objects'].items():
            row=rows[oid];figures=[]
            for vid,view in bundle['views'].items():
                marks=[m for m in view['annotations'] if m['objectId']==oid]
                if not marks:continue
                frame=view['frame'];scale=min(1.,panel_width/frame.camera.width)
                width=int(round(frame.camera.width*scale));height=int(round(frame.camera.height*scale))
                panels=[]
                for label,values,color in [('Before',obj['initial'],(45,110,245)),
                                            ('Candidate',parameters(row['candidate']) if row.get('candidate') else obj['initial'],(245,120,25))]:
                    with Image.open(frame.image_path) as raw:im=raw.convert('RGB').resize((width,height),Image.Resampling.LANCZOS)
                    draw=ImageDraw.Draw(im);xyz=corners(values)
                    for a,b in EDGES:
                        segments=_project_edge(frame,xyz[a],xyz[b],bundle['policy']['nearM'])
                        for segment in segments:draw.line([tuple(p*scale) for p in segment],fill=color,width=2)
                    for mark in marks:
                        points=[tuple(np.asarray(p)*scale) for p in mark['pixels']]
                        if mark['kind']=='edge':draw.line(points,fill=(20,220,90),width=3)
                        for x,y in points:draw.ellipse((x-3,y-3,x+3,y+3),fill=(20,220,90))
                    panel=Image.new('RGB',(width,height+42),'#ffffff');panel.paste(im,(0,42));d=ImageDraw.Draw(panel)
                    d.text((8,6),f'{label} | {vid} | {view["split"]}',fill='black')
                    d.text((8,23),f'Image marks: green; box: {"blue" if label=="Before" else "orange"}; display scale {scale:.3f}',fill='black')
                    panels.append(panel)
                pair=Image.new('RGB',(width*2,height+42),'white');pair.paste(panels[0],(0,0));pair.paste(panels[1],(width,0))
                filename=hashlib.sha256((oid+'\0'+vid).encode('utf-8')).hexdigest()[:20]+'.png'
                pair.save(staging/filename,format='PNG');images.append({'objectId':oid,'viewId':vid,'file':filename,'sha256':sha256(staging/filename),'scale':scale,
                                                                       'originalWidth':frame.camera.width,'originalHeight':frame.camera.height})
                figures.append(f'<figure><img loading="lazy" src="{filename}" alt="{html.escape(oid)} {html.escape(vid)} before and candidate"><figcaption>{html.escape(vid)} / {view["split"]}</figcaption></figure>')
            metrics=[]
            for split in ('fit','holdout'):
                before=row.get('before',{}).get(split,{}).get('summary');after=row.get('after',{}).get(split,{}).get('summary')
                metrics.append(f'{split}: '+(f'{before["rmsPx"]:.3f} → {after["rmsPx"]:.3f} px RMS' if before and after else 'NOT_FITTED'))
            sections.append('<section><h2>'+html.escape(oid)+'</h2><p>'+html.escape(row['status'])+' · '+html.escape(', '.join(row.get('reasons',[])))+'</p><p>'+html.escape(' | '.join(metrics))+'</p>'+''.join(figures)+'</section>')
        page='<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Multiview refinement review</title><style>body{font:15px system-ui;max-width:1700px;margin:24px auto;padding:0 16px;background:#eef1ed;color:#25352e}section{background:white;padding:20px;margin:20px 0;border-radius:10px}figure{margin:16px 0}img{display:block;max-width:100%;height:auto}figcaption{color:#58685e;margin-top:6px}</style><h1>Multiview refinement review</h1><p>Source-derived private review images. Numeric errors use original-resolution pixels. Blue: baseline; orange: candidate; green: image annotations. These overlays do not create acceptance or observation truth.</p>'+''.join(sections)+'</html>'
        (staging/'index.html').write_text(page,encoding='utf-8')
        atomic_json(staging/'render-manifest.json',{'inputSha256':report['inputSha256'],'fitReportContentSha256':canonical_hash(report),'images':images,'sourceDerivedPrivateReview':True})
        verify_bindings(bundle);_fail(not output_dir.exists(),'OUTPUT_EXISTS');os.rename(staging,output_dir)
    except Exception:
        # Preserve incomplete derivatives for diagnosis; do not delete a shared
        # directory or overwrite a previous report on a failed render.
        raise
    return {'objects':len(rows),'images':len(images),'output':str(output_dir/'index.html'),'status':'VISUAL_REVIEW_NOT_RUN'}
