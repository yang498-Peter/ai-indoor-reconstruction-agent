"""Meaningful source/transform/node and stale-artifact failure cases."""
import json
from pathlib import Path
import sys

import numpy as np
import pytest
import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import presentation_alignment as tool
from smoke_presentation import make_capture


def test_world_transform_and_rotated_capture(tmp_path):
    args = make_capture(tmp_path / 'new machine with spaces', [120, -350, 17], 37)
    result = tool.run(args)
    row = result['observations'][0]
    assert row['roiPoints'] == 480
    assert row['modelFaceM'] == 3.5
    assert abs(row['differenceM']) < .0002
    assert result['status'] == 'OBSERVATIONS_ONLY'
    assert result['modelSha256'] == tool.sha256(args.model)


def test_different_capture_and_stale_cache_fail(tmp_path):
    args = make_capture(tmp_path / 'first', [-50, 12, 9], -17)
    other = make_capture(tmp_path / 'other', [400, 500, 60], 15)
    args.cloud = other.cloud
    with pytest.raises(ValueError, match='SOURCE_OR_CACHE_CHANGED'):
        tool.run(args)
    args.cloud = tmp_path / 'first/capture/synthetic.las'
    with (args.work / 'evidence-cache.npz').open('ab') as stream:
        stream.write(b'changed')
    with pytest.raises(ValueError, match='SOURCE_OR_CACHE_CHANGED'):
        tool.run(args)


def test_before_face_not_copy_of_current_face(tmp_path):
    args = make_capture(tmp_path / 'data', [8, 7, 6], 25)
    scene = trimesh.Scene()
    transform = np.eye(4); transform[:3, 3] = [2, 2, -4]
    scene.add_geometry(trimesh.creation.box([1, 1, 1]), node_name='fixture', transform=transform)
    args.before_model = tmp_path / 'before.glb'
    args.before_model.write_bytes(scene.export(file_type='glb'))
    row = tool.run(args)['observations'][0]
    assert row['beforeFaceM'] == 2.5
    assert abs(row['beforeDifferenceM'] + 1) < .0002
    assert abs(row['differenceM']) < .0002


def test_competing_peak_and_empty_band_remain_observations():
    points = np.array([[0, .10, 1]] * 80 + [[0, .36, 1]] * 140)
    query = {'axis': 1, 'roi': [[-.5, .5], [0, .5], [.5, 1.5]], 'binM': .02, 'radiusM': .02}
    row = tool.observe(points, query)
    assert row['medianM'] == .36 and len(row['peaks']) == 2
    assert row['status'] == 'OBSERVATIONS_ONLY'  # Dominant return might be a cabinet.
    query['roi'][2] = [2, 3]
    assert tool.observe(points, query)['status'] == 'NO_SUPPORT'


def test_invalid_frame_missing_node_and_raw_overwrite(tmp_path):
    args = make_capture(tmp_path / 'data', [3, 4, 5], 10)
    config = json.loads(args.queries.read_text(encoding='utf-8'))
    config['queries'][0]['nodes'] = ['missing']
    args.queries.write_text(json.dumps(config), encoding='utf-8')
    with pytest.raises(ValueError, match='MODEL_NODE_NOT_FOUND'):
        tool.run(args)
    config['modelToCache'][0][0] = 0
    args.queries.write_text(json.dumps(config), encoding='utf-8')
    with pytest.raises(ValueError, match='INVALID_RIGID_AXIS_MAPPING'):
        tool.run(args)
    args.output = args.cloud
    with pytest.raises(ValueError, match='OUTPUT_INSIDE_CAPTURE'):
        tool.run(args)
    args.before_model = args.model
    args.output = args.before_model
    with pytest.raises(ValueError, match='OUTPUT_OVERWRITES_INPUT'):
        tool.run(args)


def test_no_partial_report_for_invalid_query(tmp_path):
    args = make_capture(tmp_path / 'data', [1, 2, 3], 45)
    config = json.loads(args.queries.read_text(encoding='utf-8'))
    config['queries'].append(dict(config['queries'][0]))
    args.queries.write_text(json.dumps(config), encoding='utf-8')
    with pytest.raises(ValueError, match='EMPTY_OR_DUPLICATE_QUERIES'):
        tool.run(args)
    assert not args.output.exists()
