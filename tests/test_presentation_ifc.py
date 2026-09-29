from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from smoke_presentation import make_ifc
from validate_presentation_ifc import validate


def test_semantic_ifc_real_geometry_and_world_placement(tmp_path):
    path = tmp_path / 'synthetic.ifc'
    make_ifc(path)
    result = validate(path)
    assert result['checksSucceeded']
    assert result['schema'] == 'IFC4' and result['representedProducts'] == 1
    np.testing.assert_allclose(result['products'][0]['worldBoundsM'], [[17, -11, 2], [21, -10.8, 5]])
    assert result['receivingSoftwareAcceptance'] == 'NOT_RUN'


def test_empty_ifc_cannot_report_success(tmp_path):
    import ifcopenshell
    path = tmp_path / 'empty.ifc'
    ifcopenshell.file(schema='IFC4').write(str(path))
    result = validate(path)
    assert not result['checksSucceeded'] and result['representedProducts'] == 0
