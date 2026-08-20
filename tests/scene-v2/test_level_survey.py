"""RANSAC level-survey tests: flat floor, 2-degree tilted slab, outliers.

The fixture room carries vertical walls (must be rejected by the tilt gate),
a dense horizontal table surface (must stay an alternative, never the floor)
and a ceiling plane, so the survey has to rank multiple candidates and still
bind the true floor.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import laspy
import numpy as np

CORE = Path(__file__).resolve().parents[2] / "scene-core"


def load(name: str):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, CORE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


capture_index = load("capture_index")
level_survey = load("level_survey")

ORIGIN_X = 500_000.0
ORIGIN_Y = 7_000_000.0
TILT_SLOPE = math.tan(math.radians(2.0))


def synthetic_room(path: Path, *, tilt: bool) -> None:
    """8 x 6 m room: floor, ceiling at +2.8, four walls, one table at +0.75."""

    def floor_z(x: np.ndarray | float) -> np.ndarray | float:
        return TILT_SLOPE * (x - ORIGIN_X) if tilt else 0.0

    rows: list[tuple[float, float, float]] = []
    xs = np.arange(ORIGIN_X, ORIGIN_X + 8.0 + 1e-9, 0.08)
    ys = np.arange(ORIGIN_Y, ORIGIN_Y + 6.0 + 1e-9, 0.08)
    for x in xs:
        base = float(floor_z(x))
        for y in ys:
            rows.append((float(x), float(y), base))
            rows.append((float(x), float(y), base + 2.8))
    wall_z = np.linspace(0.3, 2.6, 20)
    for x in xs:
        base = float(floor_z(x))
        for y_edge in (ORIGIN_Y, ORIGIN_Y + 6.0):
            rows.extend((float(x), y_edge, base + float(z)) for z in wall_z)
    for y in ys:
        for x_edge in (ORIGIN_X, ORIGIN_X + 8.0):
            base = float(floor_z(x_edge))
            rows.extend((x_edge, float(y), base + float(z)) for z in wall_z)
    # Dense horizontal table surface: a classic false-floor outlier plane.
    for x in np.arange(ORIGIN_X + 2.0, ORIGIN_X + 4.0 + 1e-9, 0.05):
        base = float(floor_z(x))
        for y in np.arange(ORIGIN_Y + 2.0, ORIGIN_Y + 3.0 + 1e-9, 0.05):
            rows.append((float(x), float(y), base + 0.75))

    xyz = np.asarray(rows, dtype=np.float64)
    header = laspy.LasHeader(point_format=3, version="1.2")
    header.scales = np.asarray([0.001, 0.001, 0.001])
    header.offsets = np.asarray([ORIGIN_X, ORIGIN_Y, 0.0])
    las = laspy.LasData(header)
    las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    las.write(path)


def plane_z(plane: dict, x: float, y: float) -> float:
    return plane["a"] * x + plane["b"] * y + plane["c"]


def build_index(root: Path, *, tilt: bool):
    las = root / ("room-tilt.las" if tilt else "room-flat.las")
    synthetic_room(las, tilt=tilt)
    index_root = root / ("index-tilt" if tilt else "index-flat")
    capture_index.build_index(las, index_root, tile_size_m=4.0)
    return index_root, capture_index.CaptureIndex.open(index_root, validate_source=True)


class LevelSurveyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        cls.flat_root, cls.flat_index = build_index(root, tilt=False)
        cls.tilt_root, cls.tilt_index = build_index(root, tilt=True)

    def test_flat_floor_and_ceiling_are_found_despite_walls_and_table(self):
        result = level_survey.survey_levels(self.flat_index)
        center_x, center_y = ORIGIN_X + 4.0, ORIGIN_Y + 3.0
        self.assertLess(abs(plane_z(result["floor"], center_x, center_y)), 0.03)
        self.assertLess(result["tiltDeg"], 0.5)
        self.assertGreater(result["supportPointCount"], 5000)
        self.assertGreater(result["inlierRatio"], 0.2)
        self.assertEqual(result["ceilingSource"], "ransac-plane")
        self.assertAlmostEqual(plane_z(result["ceiling"], center_x, center_y), 2.8, delta=0.05)
        # The table plane must surface as a ranked alternative, not the floor.
        self.assertTrue(any(abs(item["medianZ"] - 0.75) < 0.05 for item in result["alternatives"]))
        supports = [item["supportPointCount"] for item in result["alternatives"]]
        self.assertEqual(supports, sorted(supports, reverse=True))

    def test_survey_binds_index_and_matches_macro_builder_contract(self):
        result = level_survey.survey_levels(self.flat_index)
        # cleanroom_macro_builder.build reads survey["floor"], survey["ceiling"]
        # and survey["source"]["indexFingerprint"] -- exactly these names.
        for key in ("floor", "ceiling"):
            self.assertEqual(set(result[key].keys()), {"a", "b", "c"})
        self.assertEqual(
            result["source"]["indexFingerprint"],
            self.flat_index.manifest["indexFingerprint"],
        )
        self.assertEqual(result["floorPlane"], result["floor"])
        self.assertEqual(result["ceilingPlane"], result["ceiling"])
        self.assertEqual(result["indexFingerprint"], result["source"]["indexFingerprint"])

    def test_tilted_slab_recovers_two_degree_tilt(self):
        result = level_survey.survey_levels(self.tilt_index)
        self.assertAlmostEqual(result["tiltDeg"], 2.0, delta=0.3)
        self.assertAlmostEqual(result["floor"]["a"], TILT_SLOPE, delta=0.005)
        self.assertLess(abs(plane_z(result["floor"], ORIGIN_X, ORIGIN_Y)), 0.05)
        # Vertical wall sheets must never pass the horizontality gate.
        for item in [result] + result["alternatives"]:
            self.assertLessEqual(item["tiltDeg"], 10.0)

    def test_robust_fit_ignores_outliers_that_wreck_least_squares(self):
        result = level_survey.survey_levels(self.flat_index)
        query = self.flat_index.query_all()
        design = np.column_stack((query.x - np.mean(query.x), query.y - np.mean(query.y), np.ones(query.point_count)))
        naive_c = float(np.linalg.lstsq(design, query.z, rcond=None)[0][2])
        center_x, center_y = ORIGIN_X + 4.0, ORIGIN_Y + 3.0
        robust_error = abs(plane_z(result["floor"], center_x, center_y))
        # Ceiling, walls and the table drag a plain least-squares plane about
        # a metre off the floor; the RANSAC survey must stay within 3 cm.
        self.assertGreater(abs(naive_c), 0.5)
        self.assertLess(robust_error, 0.03)

    def test_cli_writes_a_consumable_survey_file(self):
        output = Path(self.temp.name) / "survey.json"
        completed = subprocess.run(
            [
                sys.executable, str(CORE / "level_survey.py"),
                f"--index={self.flat_root}",
                f"--output={output}",
            ],
            capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "level-survey")
        self.assertIn("floor", payload)
        self.assertIn("ceiling", payload)
        self.assertEqual(
            payload["source"]["indexFingerprint"],
            self.flat_index.manifest["indexFingerprint"],
        )


if __name__ == "__main__":
    unittest.main()
