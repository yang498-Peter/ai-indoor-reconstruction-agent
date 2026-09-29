"""Synthetic, offline portability smoke: rotated capture, GLB, cache and IFC.

Not a generic scene generator or a customer-reconstruction benchmark.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from types import SimpleNamespace

import laspy
import numpy as np
import trimesh

from indoor_presentation_evidence import prepare, atomic_json
from presentation_alignment import run
from validate_presentation_ifc import validate


def make_ifc(path):
    import ifcopenshell
    import ifcopenshell.api

    f = ifcopenshell.file(schema='IFC4')
    api = lambda operation, **kwargs: ifcopenshell.api.run(operation, f, **kwargs)
    project = api('root.create_entity', ifc_class='IfcProject', name='Synthetic portability')
    unit = api('unit.add_si_unit', unit_type='LENGTHUNIT')
    api('unit.assign_unit', units=[unit])
    context = api('context.add_context', context_type='Model')
    body = api('context.add_context', context_type='Model', context_identifier='Body', target_view='MODEL_VIEW', parent=context)
    site = api('root.create_entity', ifc_class='IfcSite')
    building = api('root.create_entity', ifc_class='IfcBuilding')
    storey = api('root.create_entity', ifc_class='IfcBuildingStorey')
    for parent, child in [(project, site), (site, building), (building, storey)]:
        api('aggregate.assign_object', products=[child], relating_object=parent)
    wall = api('root.create_entity', ifc_class='IfcWall', name='Synthetic wall')
    api('spatial.assign_container', products=[wall], relating_structure=storey)
    representation = api('geometry.add_wall_representation', context=body, length=4., height=3., thickness=.2)
    api('geometry.assign_representation', product=wall, representation=representation)
    pose = np.eye(4); pose[:3, 3] = [17., -11., 2.]
    api('geometry.edit_object_placement', product=wall, matrix=pose)
    f.write(str(path))


def make_capture(root, origin, yaw):
    capture = root / 'capture'; capture.mkdir(parents=True)
    cloud = capture / 'synthetic.las'
    yy, zz = np.meshgrid(np.linspace(-4.2, -3.8, 24), np.linspace(1.8, 2.2, 20))
    cache_points = np.column_stack((np.full(yy.size, 3.5), yy.ravel(), zz.ravel()))
    c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    inverse = np.array([[c, -s, 0], [-s, -c, 0], [0, 0, 1]])
    source = cache_points @ inverse.T + np.asarray(origin)
    las = laspy.create(point_format=3, file_version='1.2')
    las.header.offsets = np.asarray(origin); las.header.scales = [.0001] * 3
    las.x, las.y, las.z = source.T
    las.write(cloud)
    work = root / 'cache'
    args = SimpleNamespace(cloud=cloud, work=work, origin=origin, yaw_deg=yaw)
    prepare(args)
    assert prepare(args)['reused']
    scene = trimesh.Scene()
    pose = np.eye(4); pose[:3, 3] = [3, 2, -4]
    scene.add_geometry(trimesh.creation.box([1, 1, 1]), node_name='fixture', transform=pose)
    model = root / 'synthetic.glb'; model.write_bytes(scene.export(file_type='glb'))
    queries = root / 'queries.json'
    atomic_json(queries, {'modelToCache': [[1, 0, 0, 0], [0, 0, 1, 0], [0, 1, 0, 0], [0, 0, 0, 1]],
                         'queries': [{'id': 'fixture-face', 'roi': [[3.2, 3.8], [-4.3, -3.7], [1.7, 2.3]],
                                      'axis': 0, 'binM': .01, 'radiusM': .01, 'nodes': ['fixture'], 'side': 'max'}]})
    return SimpleNamespace(work=work, cloud=cloud, queries=queries, model=model,
                           before_model=None, output=root / 'alignment.json')


def smoke(work):
    if work.exists():
        raise ValueError('Use a new work directory; smoke never deletes previous evidence')
    work.mkdir(parents=True)
    reports = []
    for index, (origin, yaw) in enumerate([([107., -83., 12.], 31.), ([-51., 222., 7.], -23.)]):
        args = make_capture(work / f'variant-{index}', origin, yaw)
        result = run(args)
        assert abs(result['observations'][0]['differenceM']) < .0002
        reports.append({'sourceSha256': result['sourceSha256'], 'differenceM': result['observations'][0]['differenceM']})
    assert reports[0]['sourceSha256'] != reports[1]['sourceSha256']
    ifc = work / 'synthetic.ifc'; make_ifc(ifc)
    checked = validate(ifc)
    assert checked['checksSucceeded'], checked
    np.testing.assert_allclose(checked['products'][0]['worldBoundsM'], [[17, -11, 2], [21, -10.8, 5]], atol=1e-6)
    atomic_json(work / 'ifc-readback.json', checked)
    result = {'syntheticSmokeSucceeded': True, 'variants': reports, 'ifcSha256': checked['ifcSha256'],
              'scope': 'Synthetic portable tools only. No customer geometry, browser or external BIM/SketchUp GUI acceptance.'}
    atomic_json(work / 'smoke.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(smoke(args.work.resolve()), indent=2))
    except (OSError, ValueError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    main()
