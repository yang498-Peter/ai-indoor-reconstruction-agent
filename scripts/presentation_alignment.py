"""Batch source-bound face observations; never an acceptance or scene writer.

Consumes the portable evidence helper's cache. Model and cache axes must be
related explicitly; GLB scene-node transforms are applied before measurement.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
from indoor_presentation_evidence import atomic_json, sha256


def matrix(value):
    m = np.asarray(value, dtype=float)
    if (m.shape != (4, 4) or not np.isfinite(m).all()
            or not np.allclose(m[3], [0, 0, 0, 1])
            or not np.allclose(m[:3, :3].T @ m[:3, :3], np.eye(3), atol=1e-7)):
        raise ValueError('INVALID_RIGID_AXIS_MAPPING')
    return m


def roi_points(points, roi):
    bounds = np.asarray(roi, dtype=float)
    if (bounds.shape != (3, 2) or not np.isfinite(bounds).all()
            or np.any(bounds[:, 0] >= bounds[:, 1])):
        raise ValueError('INVALID_ROI: three increasing axis intervals required')
    return points[np.all((points >= bounds[:, 0]) & (points < bounds[:, 1]), axis=1)]


def observe(points, query):
    axis = query['axis']
    if isinstance(axis, bool) or axis not in (0, 1, 2):
        raise ValueError('INVALID_AXIS')
    step, radius = float(query['binM']), float(query['radiusM'])
    if not np.isfinite([step, radius]).all() or min(step, radius) <= 0:
        raise ValueError('INVALID_HISTOGRAM_PARAMETERS')
    q = roi_points(points, query['roi'])
    lo, hi = query['roi'][axis]
    n = int(np.ceil((hi - lo) / step))
    if n > 1000000:
        raise ValueError('HISTOGRAM_TOO_LARGE')
    base = {'roi': query['roi'], 'roiPoints': len(q), 'axis': axis,
            'binM': step, 'radiusM': radius}
    if not len(q):
        return {**base, 'status': 'NO_SUPPORT', 'medianM': None, 'peaks': []}
    hist, edges = np.histogram(q[:, axis], bins=lo + np.arange(n + 1) * step)
    peak = (edges[hist.argmax()] + edges[hist.argmax() + 1]) / 2
    support = q[np.abs(q[:, axis] - peak) <= radius, axis]
    # A narrow radius can miss every return, even when the bin is populated.
    if not len(support):
        return {**base, 'status': 'NO_SUPPORT', 'medianM': None, 'peaks': []}
    ids = np.argsort(-hist, kind='stable')[:5]
    return {**base, 'status': 'OBSERVATIONS_ONLY', 'modeM': float(peak),
            'supportPoints': len(support), 'medianM': float(np.median(support)),
            'p05P95M': np.quantile(support, [.05, .95]).tolist(),
            'peaks': [{'centreM': float((edges[i] + edges[i + 1]) / 2),
                       'points': int(hist[i])} for i in ids if hist[i] > 0]}


def model_vertices(path, transform):
    import trimesh
    scene = trimesh.load_scene(path, process=False)
    result = {}
    for name in scene.graph.nodes_geometry:
        world, geometry = scene.graph[name]
        v = np.asarray(scene.geometry[geometry].vertices)
        combined = transform @ world
        p = v @ combined[:3, :3].T + combined[:3, 3]
        if not np.isfinite(p).all():
            raise ValueError('NONFINITE_MODEL')
        result[name] = p
    return result


def face_value(nodes, query):
    names = query['nodes']
    if not names or any(name not in nodes for name in names):
        raise ValueError('MODEL_NODE_NOT_FOUND')
    v = np.concatenate([nodes[name] for name in names])
    if 'modelRoi' in query:
        v = roi_points(v, query['modelRoi'])
    if not len(v):
        raise ValueError('NO_MODEL_VERTICES_IN_ROI')
    side = query['side']
    if side not in ('min', 'max'):
        raise ValueError('INVALID_MODEL_FACE')
    return float((np.min if side == 'min' else np.max)(v[:, query['axis']]))


def run(args):
    start = time.perf_counter()
    work = args.work.resolve(strict=True)
    meta_path, cache = work / 'evidence-cache.json', work / 'evidence-cache.npz'
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    source = args.cloud.resolve(strict=True)
    output = args.output.resolve()
    if output == source or source.parent == output.parent or source.parent in output.parents:
        raise ValueError('OUTPUT_INSIDE_CAPTURE')
    protected = [meta_path, cache, args.queries.resolve(), args.model.resolve()]
    if args.before_model:
        protected.append(args.before_model.resolve())
    if output in protected:
        raise ValueError('OUTPUT_OVERWRITES_INPUT')
    if meta['lengthUnit'] != 'metre' or meta['sourceUp'] != 'Z':
        raise ValueError('UNSUPPORTED_SOURCE_FRAME')
    if meta['cacheAxes'] != ['x', 'depth', 'height']:
        raise ValueError('UNSUPPORTED_CACHE_AXES')
    if sha256(source) != meta['sourceSha256'] or sha256(cache) != meta['cacheSha256']:
        raise ValueError('SOURCE_OR_CACHE_CHANGED: run prepare for this capture')
    with np.load(cache, allow_pickle=False) as stored:
        points = stored['points']
    if points.shape != (meta['pointCount'], 3) or not np.isfinite(points).all():
        raise ValueError('INVALID_CACHE_POINTS')
    loaded = time.perf_counter()
    config = json.loads(args.queries.read_text(encoding='utf-8'))
    transform = matrix(config['modelToCache'])
    nodes = model_vertices(args.model, transform)
    previous = model_vertices(args.before_model, transform) if args.before_model else None
    queries = config['queries']
    if not queries or len({q['id'] for q in queries}) != len(queries):
        raise ValueError('EMPTY_OR_DUPLICATE_QUERIES')
    rows = []
    for query in queries:
        row = {'id': query['id'], **observe(points, query)}
        row['modelFaceM'] = face_value(nodes, query)
        row['modelNodes'] = query['nodes']
        row['side'] = query['side']
        if 'modelRoi' in query:
            row['modelRoi'] = query['modelRoi']
        row['differenceM'] = None if row['medianM'] is None else row['modelFaceM'] - row['medianM']
        if previous is not None:
            # Missing old geometry is explicit, not a zero residual.
            before_names = query.get('beforeNodes', query['nodes'])
            before_query = {**query, 'nodes': before_names}
            old = None if not all(name in previous for name in before_names) else face_value(previous, before_query)
            row['beforeFaceM'] = old
            row['beforeDifferenceM'] = None if old is None or row['medianM'] is None else old - row['medianM']
        rows.append(row)
    result = {'status': 'OBSERVATIONS_ONLY', 'sourceSha256': meta['sourceSha256'],
              'cacheSha256': meta['cacheSha256'], 'bindingSha256': sha256(meta_path),
              'queriesSha256': sha256(args.queries), 'modelSha256': sha256(args.model),
              'beforeModelSha256': sha256(args.before_model) if args.before_model else None,
              'producerSha256': sha256(Path(__file__)), 'modelToCache': transform.tolist(),
              'cacheAxes': meta['cacheAxes'], 'sourcePointCount': len(points), 'observations': rows,
              'timingSeconds': {'verifyAndLoad': loaded - start, 'queriesAndModel': time.perf_counter() - loaded},
              'scope': 'Conditional source peaks and explicitly selected model faces; no scene acceptance, plane fit, survey accuracy or independent validation.'}
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--cloud', type=Path, required=True)
    parser.add_argument('--queries', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--before-model', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args)
        print(json.dumps({'status': result['status'], 'regions': len(result['observations']),
                          'modelSha256': result['modelSha256']}))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    main()
