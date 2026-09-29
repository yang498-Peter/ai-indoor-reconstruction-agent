"""Portable IFC schema/geometry readback, not survey or receiving-app acceptance."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from indoor_presentation_evidence import atomic_json, sha256


def validate(path):
    import ifcopenshell
    import ifcopenshell.geom
    import ifcopenshell.validate
    import ifcopenshell.util.unit

    f = ifcopenshell.open(str(path))
    logger = ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(f, logger, express_rules=True)
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    products = [p for p in f.by_type('IfcProduct') if p.Representation]
    observations, failures = [], []
    for product in products:
        try:
            shape = ifcopenshell.geom.create_shape(settings, product)
            vertices = np.asarray(shape.geometry.verts).reshape(-1, 3)
            faces = np.asarray(shape.geometry.faces).reshape(-1, 3)
            if not len(vertices) or not len(faces) or not np.isfinite(vertices).all():
                raise ValueError('EMPTY_OR_NONFINITE_GEOMETRY')
            if faces.min() < 0 or faces.max() >= len(vertices):
                raise ValueError('INDEX_OUT_OF_RANGE')
            observations.append({'guid': product.GlobalId, 'class': product.is_a(),
                                 'triangles': len(faces), 'worldBoundsM': [vertices.min(0).tolist(), vertices.max(0).tolist()]})
        except Exception as error:
            failures.append({'guid': product.GlobalId, 'error': str(error)})
    errors = [{'level': item.get('level'), 'message': item.get('message')}
              for item in logger.statements]
    return {'ifcSha256': sha256(path), 'schema': f.schema,
            'ifcopenshellVersion': ifcopenshell.version,
            'declaredLengthUnitToMetre': ifcopenshell.util.unit.calculate_unit_scale(f),
            'representedProducts': len(products), 'schemaFindings': errors,
            'geometryFailures': failures, 'products': observations,
            'checksSucceeded': bool(products) and not errors and not failures,
            'receivingSoftwareAcceptance': 'NOT_RUN', 'independentDimensionalAcceptance': 'NOT_RUN',
            'scope': 'IFC schema/EXPRESS and IfcOpenShell world-coordinate tessellation only; no source overlap, solid closure, MVD/IDS, construction or external GUI acceptance.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ifc', dest='source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        parser.error('Output must not overwrite IFC input')
    try:
        result = validate(args.source)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(args.output, result)
        print(json.dumps({key: value for key, value in result.items() if key != 'products'}))
        return 0 if result['checksSucceeded'] else 1
    except (ImportError, OSError, ValueError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
