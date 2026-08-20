import sys
import unittest
from pathlib import Path

import numpy as np


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scene-core"))

import opening_candidates as oc  # noqa: E402
import scene_api  # noqa: E402


class _SyntheticIndex:
    """query_bbox-compatible stub over a fixed synthetic point array."""

    def __init__(self, points: np.ndarray, fingerprint: str = "f" * 64):
        self._points = points
        self.manifest = {
            "indexFingerprint": fingerprint,
            "sourceIdentity": {"contentSha256": "c" * 64},
        }

    def query_bbox(self, min_x, min_y, max_x, max_y, *, z_min=None, z_max=None, every=1):
        p = self._points
        keep = (p[:, 0] >= min_x) & (p[:, 0] <= max_x) & (p[:, 1] >= min_y) & (p[:, 1] <= max_y)
        if z_min is not None:
            keep &= p[:, 2] >= z_min
        if z_max is not None:
            keep &= p[:, 2] <= z_max
        selected = p[keep][::every]

        class _Query:
            x = selected[:, 0]
            y = selected[:, 1]
            z = selected[:, 2]
            rgb = np.zeros((len(selected), 3), dtype=np.uint8)
            point_count = len(selected)
            stats = {"pointsReturned": len(selected)}

        return _Query()


def _wall_fixture() -> _SyntheticIndex:
    """Wall along x from (0,0) to (6,0), two faces at y = +-0.05, 0..2.7 m.

    - full-height door hole at x in [1.5, 2.4] (0.9 m wide);
    - window hole at x in [3.6, 4.8], z in [0.9, 2.1], filled with 5 %-density
      glass returns;
    - wall coverage physically ends at x = 5.2: the trailing hole reaches the
      wall end and must not become an opening candidate.
    """
    rng = np.random.default_rng(42)
    step = 0.02
    xs = np.arange(0.0, 5.2, step)
    zs = np.arange(0.0, 2.7, step)
    grid_x, grid_z = np.meshgrid(xs, zs)
    grid_x, grid_z = grid_x.ravel(), grid_z.ravel()
    in_door = (grid_x >= 1.5) & (grid_x <= 2.4)
    in_window = (grid_x >= 3.6) & (grid_x <= 4.8) & (grid_z >= 0.9) & (grid_z <= 2.1)
    solid = ~(in_door | in_window)
    glass = in_window & (rng.random(len(grid_x)) < 0.05)
    keep_x = np.concatenate((grid_x[solid], grid_x[glass]))
    keep_z = np.concatenate((grid_z[solid], grid_z[glass]))
    points = []
    for face_y in (-0.05, 0.05):
        points.append(np.column_stack((keep_x, np.full_like(keep_x, face_y), keep_z)))
    return _SyntheticIndex(np.vstack(points))


class OpeningDetectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = oc.detect_openings(
            _wall_fixture(), (0.0, 0.0), (6.0, 0.0), 0.1, floor_z=0.0,
        )

    def test_door_and_window_each_detected_once(self):
        kinds = [candidate["type"] for candidate in self.report["candidates"]]
        self.assertEqual(sorted(kinds), ["door", "window"])
        self.assertTrue(all(
            candidate["status"] == "candidate" for candidate in self.report["candidates"]
        ))

    def test_door_geometry_within_tolerance(self):
        door = next(c for c in self.report["candidates"] if c["type"] == "door")
        self.assertLess(abs(door["hostOffsetM"] - 1.95), 0.10)
        self.assertLess(abs(door["widthM"] - 0.9), 0.10)
        self.assertEqual(door["sillM"], 0.0)
        self.assertGreaterEqual(door["headM"], 1.9)
        self.assertLess(door["densityContrast"], 0.15)
        self.assertGreater(door["confidence"], 0.5)

    def test_window_geometry_within_tolerance(self):
        window = next(c for c in self.report["candidates"] if c["type"] == "window")
        self.assertLess(abs(window["hostOffsetM"] - 4.2), 0.10)
        self.assertLess(abs(window["widthM"] - 1.2), 0.10)
        self.assertLess(abs(window["sillM"] - 0.9), 0.10)
        self.assertLess(abs(window["headM"] - 2.1), 0.10)
        # Sparse glass must still read as a hole via relative density.
        self.assertLess(window["densityContrast"], 0.15)
        self.assertGreater(window["confidence"], 0.5)

    def test_wall_end_hole_is_not_an_opening(self):
        # Coverage stops at 5.2 m; the [5.2, 6.0] hole touches the end margin.
        for candidate in self.report["candidates"]:
            edge = candidate["hostOffsetM"] + candidate["widthM"] * 0.5
            self.assertLess(edge, 5.2)

    def test_report_carries_lineage_and_fingerprints(self):
        self.assertEqual(self.report["indexFingerprint"], "f" * 64)
        self.assertEqual(self.report["rootContentSha256s"], ["c" * 64])
        self.assertEqual(len(self.report["parametersFingerprint"]), 64)
        self.assertEqual(self.report["lineageId"], scene_api.evidence_lineage_id(
            ["c" * 64], oc.PRODUCER, self.report["parameters"],
        ))

    def test_floor_plane_reference_matches_scalar_floor(self):
        report = oc.detect_openings(
            _wall_fixture(), (0.0, 0.0), (6.0, 0.0), 0.1,
            floor_plane={"a": 0.0, "b": 0.0, "c": 0.0},
        )
        self.assertEqual(
            [(c["type"], c["hostOffsetM"]) for c in report["candidates"]],
            [(c["type"], c["hostOffsetM"]) for c in self.report["candidates"]],
        )


if __name__ == "__main__":
    unittest.main()
