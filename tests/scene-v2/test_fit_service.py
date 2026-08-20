"""Robust wall-line refinement tests against a synthetic corridor capture.

The fixture deliberately mixes clean wall faces, a dense furniture outlier
blob inside the fit corridor, a double-faced wall with a door gap, and two
parallel true walls, so the suite can prove the robust path converges where a
plain PCA fit is dragged off the wall.
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
fit_service = load("fit_service")

ORIGIN_X = 500_000.0
ORIGIN_Y = 7_000_000.0
Z_LEVELS = np.linspace(0.55, 2.35, 16)


def _face_points(x_lo: float, x_hi: float, y: float, *, step: float = 0.04,
                 gap: tuple[float, float] | None = None) -> list[tuple[float, float, float]]:
    rows = []
    for x in np.arange(x_lo, x_hi + step / 2, step):
        if gap is not None and gap[0] <= x - ORIGIN_X <= gap[1]:
            continue
        rows.extend((float(x), y, float(z)) for z in Z_LEVELS)
    return rows


def synthetic_corridor(path: Path) -> None:
    rows: list[tuple[float, float, float]] = []
    # Region 1: single wall face at local y=2.0 plus a furniture blob whose
    # lateral offset (0.5-0.78 m) sits inside the rough-line search corridor.
    rows.extend(_face_points(ORIGIN_X, ORIGIN_X + 7.0, ORIGIN_Y + 2.0))
    for x in np.linspace(ORIGIN_X + 3.0, ORIGIN_X + 4.4, 30):
        for y in np.linspace(ORIGIN_Y + 2.5, ORIGIN_Y + 2.78, 8):
            rows.extend((float(x), float(y), float(z)) for z in np.linspace(0.6, 1.2, 6))
    # Region 2: double-faced wall (thickness 0.2) with a 0.9 m door gap.
    rows.extend(_face_points(ORIGIN_X, ORIGIN_X + 7.0, ORIGIN_Y + 4.0, gap=(5.0, 5.9)))
    rows.extend(_face_points(ORIGIN_X, ORIGIN_X + 7.0, ORIGIN_Y + 4.2, gap=(5.0, 5.9)))
    # Region 3: two parallel TRUE walls 0.9 m apart (beyond pairing range).
    rows.extend(_face_points(ORIGIN_X, ORIGIN_X + 6.0, ORIGIN_Y + 6.0))
    rows.extend(_face_points(ORIGIN_X, ORIGIN_X + 6.0, ORIGIN_Y + 6.9))
    # Sparse floor, below the structural band.
    for x in np.arange(ORIGIN_X, ORIGIN_X + 8.0, 0.2):
        for y in np.arange(ORIGIN_Y, ORIGIN_Y + 8.0, 0.2):
            rows.append((float(x), float(y), 0.0))

    xyz = np.asarray(rows, dtype=np.float64)
    header = laspy.LasHeader(point_format=3, version="1.2")
    header.scales = np.asarray([0.001, 0.001, 0.001])
    header.offsets = np.asarray([ORIGIN_X, ORIGIN_Y, 0.0])
    las = laspy.LasData(header)
    las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    las.write(path)


def _line_y_error(result: dict, face_local_y: float) -> float:
    """Max endpoint deviation from a wall face that runs along +x."""
    return max(
        abs(result["start"][1] - (ORIGIN_Y + face_local_y)),
        abs(result["end"][1] - (ORIGIN_Y + face_local_y)),
    )


def _rough_line(mid_local_y: float, *, x_lo: float, x_hi: float, tilt_deg: float):
    """A deliberately bad Agent line: lateral offset plus tilt about its midpoint."""
    half = (x_hi - x_lo) / 2.0
    dy = half * math.tan(math.radians(tilt_deg))
    return (
        [ORIGIN_X + x_lo, ORIGIN_Y + mid_local_y - dy],
        [ORIGIN_X + x_hi, ORIGIN_Y + mid_local_y + dy],
    )


class FitServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        cls.las = root / "corridor.las"
        synthetic_corridor(cls.las)
        cls.index_root = root / "capture-index"
        capture_index.build_index(cls.las, cls.index_root, tile_size_m=2.0)
        cls.index = capture_index.CaptureIndex.open(cls.index_root, validate_source=True)

    def test_robust_refine_converges_from_offset_and_tilted_rough_line(self):
        # 0.25 m lateral offset + 8 deg tilt: the documented Agent error budget.
        start, end = _rough_line(2.25, x_lo=0.5, x_hi=6.5, tilt_deg=8.0)
        result = fit_service.refine_wall_line(self.index, start, end, floor_z=0.0)
        self.assertEqual(result["status"], "FIT_OK", result["reason"])
        self.assertLess(_line_y_error(result, 2.0), 0.05)
        angle = result["angleDeg"]
        self.assertLessEqual(min(angle, 180.0 - angle), 1.0)
        self.assertGreater(result["supportPointCount"], 500)
        self.assertLess(result["residualP90M"], 0.03)
        self.assertGreater(result["inlierRatio"], 0.6)
        self.assertEqual(result["indexFingerprint"], self.index.manifest["indexFingerprint"])

    def test_floor_plane_input_matches_flat_floor_z(self):
        start, end = _rough_line(2.2, x_lo=0.5, x_hi=6.5, tilt_deg=4.0)
        result = fit_service.refine_wall_line(
            self.index, start, end,
            floor_plane={"a": 0.0, "b": 0.0, "c": 0.0},
        )
        self.assertEqual(result["status"], "FIT_OK", result["reason"])
        self.assertLess(_line_y_error(result, 2.0), 0.05)

    def test_non_robust_fit_is_pulled_by_furniture_outliers(self):
        start, end = _rough_line(2.25, x_lo=0.5, x_hi=6.5, tilt_deg=8.0)
        robust = fit_service.refine_wall_line(self.index, start, end, floor_z=0.0, robust=True)
        naive = fit_service.refine_wall_line(self.index, start, end, floor_z=0.0, robust=False)
        robust_error = _line_y_error(robust, 2.0)
        naive_error = _line_y_error(naive, 2.0)
        # The blob mass drags a plain PCA fit off the face by a decimetre-class
        # amount; the robust path must stay within survey tolerance.
        self.assertLess(robust_error, 0.05)
        self.assertGreater(naive_error, 0.12)
        self.assertLess(robust_error, naive_error)

    def test_double_faced_wall_reports_thickness_and_paired_centerline(self):
        start, end = _rough_line(4.08, x_lo=0.3, x_hi=6.8, tilt_deg=3.0)
        result = fit_service.refine_wall_line(self.index, start, end, floor_z=0.0)
        self.assertEqual(result["status"], "FIT_OK", result["reason"])
        # The fit lands on one of the two true faces.
        nearest_face = min(_line_y_error(result, 4.0), _line_y_error(result, 4.2))
        self.assertLess(nearest_face, 0.05)
        double = result["doubleSided"]
        self.assertTrue(double["detected"])
        self.assertAlmostEqual(double["thicknessM"], 0.2, delta=0.04)
        paired = double["pairedCenterline"]
        mid_y = (paired["start"][1] + paired["end"][1]) / 2.0
        self.assertAlmostEqual(mid_y - ORIGIN_Y, 4.1, delta=0.05)

    def test_two_parallel_true_walls_in_corridor_is_ambiguous(self):
        start, end = _rough_line(6.45, x_lo=0.3, x_hi=5.7, tilt_deg=0.0)
        result = fit_service.refine_wall_line(self.index, start, end, floor_z=0.0)
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertIn("competing lateral offsets", result["reason"])
        # Even when ambiguous, the returned line must sit on one real face.
        nearest_face = min(_line_y_error(result, 6.0), _line_y_error(result, 6.9))
        self.assertLess(nearest_face, 0.05)

    def test_empty_corridor_is_low_support(self):
        result = fit_service.refine_wall_line(
            self.index,
            [ORIGIN_X + 0.5, ORIGIN_Y + 10.5],
            [ORIGIN_X + 5.5, ORIGIN_Y + 10.5],
            floor_z=0.0,
        )
        self.assertEqual(result["status"], "LOW_SUPPORT")
        self.assertTrue(result["reason"])
        # The rough line is echoed back unchanged, never invented geometry.
        self.assertEqual(result["start"], [ORIGIN_X + 0.5, ORIGIN_Y + 10.5])

    def test_probe_plane_gap_finds_the_door_gap(self):
        result = fit_service.probe_plane_gap(
            self.index,
            [ORIGIN_X, ORIGIN_Y + 4.0],
            [ORIGIN_X + 7.0, ORIGIN_Y + 4.0],
            floor_z=0.0,
        )
        interior = [gap for gap in result["gaps"] if not gap["touchesLineEnd"]]
        self.assertEqual(len(interior), 1)
        gap = interior[0]
        self.assertAlmostEqual(gap["startM"], 5.0, delta=0.15)
        self.assertAlmostEqual(gap["endM"], 5.9, delta=0.15)
        self.assertGreater(result["occupiedRatio"], 0.8)
        self.assertGreater(result["supportPointCount"], 1000)

    def test_cli_refine_emits_json(self):
        start, end = _rough_line(2.2, x_lo=0.5, x_hi=6.5, tilt_deg=5.0)
        completed = subprocess.run(
            [
                sys.executable, str(CORE / "fit_service.py"), "refine-wall-line",
                f"--index={self.index_root}",
                f"--start={start[0]},{start[1]}",
                f"--end={end[0]},{end[1]}",
                "--floor-z=0.0",
                "--band=0.5,2.4",
            ],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "FIT_OK")
        self.assertLess(abs(payload["start"][1] - (ORIGIN_Y + 2.0)), 0.05)


if __name__ == "__main__":
    unittest.main()
