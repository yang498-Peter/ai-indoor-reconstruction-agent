"""Fast-path evidence tests use synthetic geometry, never customer fixtures."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import json
import numpy as np
import pytest

spec=importlib.util.spec_from_file_location('presentation_evidence',Path(__file__).parents[1]/'scripts/indoor_presentation_evidence.py')
tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)


def test_explicit_axis_mapping():
    p=tool.aligned_points([[11,22,33]],[10,20,30],0)
    np.testing.assert_allclose(p,[[1,-2,3]])
    np.testing.assert_allclose(tool.aligned_points([[11,22,33]],[10,20,30],90),[[-2,-1,3]],atol=1e-6)


def test_region_negative_coordinates_and_bounds():
    p=np.array([[-4,-3,0],[-1,2,.12],[8,9,2]])
    assert tool.select_region(p,[-5,-4,0,3]).tolist()==[True,True,False]
    with pytest.raises(ValueError,match='INVALID_BOUNDS'):tool.select_region(p,[1,0,0,3])


def test_height_hints_do_not_claim_measurement():
    p=np.array([[0,0,.12]]*50+[[0,0,.75]]*80)
    result=tool.height_peaks(p,0,.3)
    assert result['count']==50 and result['peaks'][0]['points']==50
    assert 'not a fitted floor' in result['warning']
    assert tool.height_peaks(p,1,2)['quantilesM']==[]


def test_new_capture_reuse_and_binding_rejection(tmp_path):
    import laspy
    capture=tmp_path/'capture';capture.mkdir();source=capture/'different.las'
    las=laspy.create(point_format=3,file_version='1.2');las.x=[100,102,104];las.y=[200,203,206];las.z=[4,4.12,6.8];las.write(source)
    args=SimpleNamespace(cloud=source,work=tmp_path/'work',origin=[100,200,4],yaw_deg=0)
    assert tool.prepare(args)['reused'] is False
    assert tool.prepare(args)['reused'] is True
    cache=np.load(args.work/'evidence-cache.npz')
    np.testing.assert_allclose(cache['points'],[[0,0,0],[2,-3,.12],[4,-6,2.8]],atol=.001)
    meta=json.loads((args.work/'evidence-cache.json').read_text(encoding='utf-8'))
    assert meta['pointCount']==3 and meta['lengthUnit']=='metre'
    args.yaw_deg=10
    with pytest.raises(ValueError,match='WORK_BINDING_MISMATCH'):tool.prepare(args)
    args.work=capture/'outputs'
    with pytest.raises(ValueError,match='OUTPUT_INSIDE_CAPTURE'):tool.prepare(args)
