from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

import laspy
import numpy as np


CORE = Path(__file__).resolve().parents[2] / "scene-core"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, CORE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


capture_index = load("capture_index")
proposals = load("structural_proposals")
levels = load("level_estimation")


def write_las(path: Path, xyz: np.ndarray) -> None:
    header = laspy.LasHeader(point_format=3, version="1.2")
    header.scales = np.asarray([0.001, 0.001, 0.001])
    las = laspy.LasData(header)
    las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    las.red = np.full(len(xyz), 20_000, dtype=np.uint16)
    las.green = np.full(len(xyz), 30_000, dtype=np.uint16)
    las.blue = np.full(len(xyz), 40_000, dtype=np.uint16)
    las.write(path)


class LevelEstimationDegradedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_clear_floor_and_ceiling_peaks_stay_review_required(self) -> None:
        rows: list[tuple[float, float, float]] = []
        rows.extend((0.0, 0.0, 0.0) for _ in range(2))
        rows.extend((float(i % 20) * 0.1, float(i // 20) * 0.1, 0.2) for i in range(400))
        rows.extend((0.5, 0.5, 0.2 + 2.7 * i / 199.0) for i in range(200))
        rows.extend((float(i % 20) * 0.1, float(i // 20) * 0.1, 2.9) for i in range(400))
        rows.extend((0.0, 0.0, 3.0) for _ in range(2))
        las = self.root / "room.las"
        write_las(las, np.asarray(rows, dtype=np.float64))
        result = levels.estimate_levels(las)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["degradationReasons"], [])
        self.assertEqual(result["floorSource"], "histogram-peak")
        self.assertEqual(result["ceilingSource"], "histogram-peak")
        self.assertAlmostEqual(result["floorZ"], 0.205, delta=0.02)
        self.assertAlmostEqual(result["ceilingZ"], 2.905, delta=0.02)

    def test_short_column_degrades_with_best_guesses_instead_of_raising(self) -> None:
        rows: list[tuple[float, float, float]] = []
        rows.extend((0.0, 0.0, 0.0) for _ in range(2))
        rows.extend((float(i % 20) * 0.1, float(i // 20) * 0.1, 0.3) for i in range(300))
        rows.extend((0.5, 0.5, 0.3 + 1.2 * i / 49.0) for i in range(50))
        las = self.root / "short.las"
        write_las(las, np.asarray(rows, dtype=np.float64))
        result = levels.estimate_levels(las)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertGreaterEqual(len(result["degradationReasons"]), 2)
        self.assertTrue(
            any("Z span" in reason for reason in result["degradationReasons"])
        )
        self.assertEqual(result["floorSource"], "histogram-peak")
        self.assertAlmostEqual(result["floorZ"], 0.305, delta=0.02)
        self.assertEqual(result["ceilingSource"], "quantile-fallback")
        self.assertEqual(result["ceilingAlternatives"], [])
        self.assertGreater(len(result["floorAlternatives"]), 0)
        # A best guess is still emitted so the Agent has a starting point.
        self.assertGreater(result["ceilingZ"], result["floorZ"])


class StructuralProposalsDegradedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _index(self, las: Path, tile_size_m: float):
        index_dir = self.root / f"{las.stem}-index"
        capture_index.build_index(las, index_dir, tile_size_m=tile_size_m, every=1)
        return capture_index.CaptureIndex.open(index_dir)

    def test_sparse_band_returns_empty_degraded_proposals(self) -> None:
        las = self.root / "sparse.las"
        write_las(
            las,
            np.asarray(
                [
                    [0.0, 0.0, 0.0],
                    [1.0, 1.0, 1.0],
                    [2.0, 1.5, 1.5],
                    [2.5, 2.0, 2.5],
                ],
                dtype=np.float64,
            ),
        )
        index = self._index(las, tile_size_m=1.0)
        report = proposals.build_proposals(index, floor_z=0.0)
        self.assertEqual(report["status"], "DEGRADED")
        self.assertIn("too few indexed points", report["degradationReason"])
        self.assertEqual(report["wallCandidates"], [])
        self.assertEqual(report["faceObservations"], [])
        self.assertEqual(report["axisFamilies"], [])
        self.assertEqual(
            report["indexFingerprint"], index.manifest["indexFingerprint"]
        )

    def test_oversized_raster_coarsens_cell_instead_of_raising(self) -> None:
        rows: list[tuple[float, float, float]] = []
        for i in range(25):
            rows.append((float(i % 5) * 0.25, float(i // 5) * 0.25, 1.0))
        for i in range(25):
            rows.append((400.0 + float(i % 5) * 0.25, 400.0 + float(i // 5) * 0.25, 1.0))
        las = self.root / "wide.las"
        write_las(las, np.asarray(rows, dtype=np.float64))
        index = self._index(las, tile_size_m=200.0)
        report = proposals.build_proposals(index, floor_z=0.0, raster_cell_m=0.05)
        self.assertEqual(report["status"], "DEGRADED")
        self.assertIn("coarsened", report["degradationReason"])
        self.assertEqual(report["parameters"]["rasterCellM"], 0.05)
        self.assertAlmostEqual(report["parameters"]["degradedRasterCellM"], 0.1)
        self.assertAlmostEqual(report["raster"]["cellM"], 0.1)
        self.assertLessEqual(
            report["raster"]["widthPx"] * report["raster"]["heightPx"], 50_000_000
        )


if __name__ == "__main__":
    unittest.main()
