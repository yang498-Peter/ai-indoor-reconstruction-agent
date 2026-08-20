from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scene-core"))

import pointcloud_scene_metrics as metrics  # noqa: E402


class FakeIndex:
    def __init__(self, x: np.ndarray, y: np.ndarray, z: np.ndarray) -> None:
        self.x = np.asarray(x, dtype=np.float64)
        self.y = np.asarray(y, dtype=np.float64)
        self.z = np.asarray(z, dtype=np.float64)
        self.manifest = {"bounds": {
            "minX": float(self.x.min()), "minY": float(self.y.min()), "minZ": float(self.z.min()),
            "maxX": float(self.x.max()), "maxY": float(self.y.max()), "maxZ": float(self.z.max()),
        }}

    def query_bbox(self, x_min, y_min, x_max, y_max, *, z_min=None, z_max=None):
        keep = (self.x >= x_min) & (self.x <= x_max) & (self.y >= y_min) & (self.y <= y_max)
        if z_min is not None:
            keep &= self.z >= z_min
        if z_max is not None:
            keep &= self.z <= z_max
        return SimpleNamespace(
            x=self.x[keep], y=self.y[keep], z=self.z[keep],
            point_count=int(np.count_nonzero(keep)),
            stats={"tilesRead": 1, "pointsRead": len(self.x), "pointsReturned": int(np.count_nonzero(keep))},
        )

    def iter_bbox(self, x_min, y_min, x_max, y_max, *, z_min=None, z_max=None):
        yield self.query_bbox(x_min, y_min, x_max, y_max, z_min=z_min, z_max=z_max)


def scene_and_wall(length: float = 4.0, *, kind: str = "solid", openings: list[dict] | None = None):
    wall = {
        "id": "wall", "type": "wall", "wallKind": kind, "parentId": "level",
        "children": [], "start": [0.0, 0.0], "end": [length, 0.0],
        "height": 2.8, "thickness": 0.20 if kind == "solid" else 0.045,
    }
    nodes = {"level": {"id": "level", "type": "level", "elevation": 0.0}, "wall": wall}
    for opening in openings or []:
        nodes[opening["id"]] = opening
        wall["children"].append(opening["id"])
    return {"nodes": nodes, "evidence": {"wall": {"status": "accepted-measured"}}}, wall


def wall_points(length: float, *, omitted_s: list[tuple[float, float]] | None = None,
                omitted_cells: set[int] | None = None, opening: dict | None = None,
                faces: tuple[float, ...] = (-0.10, 0.10), step: float = 0.10):
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for column, s in enumerate(np.arange(step / 2, length, step)):
        if any(lo <= s <= hi for lo, hi in (omitted_s or [])) or column in (omitted_cells or set()):
            continue
        for h in np.arange(step / 2, 2.8, step):
            if opening is not None:
                half = opening["width"] / 2
                in_s = opening["hostOffsetM"] - half <= s <= opening["hostOffsetM"] + half
                in_h = opening.get("sillHeight", 0.0) <= h <= opening.get("sillHeight", 0.0) + opening["height"]
                if in_s and in_h:
                    continue
            for face in faces:
                xs.extend([s, s])
                ys.extend([face - 0.002, face + 0.002])
                zs.extend([h, h])
    return np.asarray(xs), np.asarray(ys), np.asarray(zs)


def evaluate(scene, wall, points, **overrides):
    config = {
        "residual_p90_max_m": 0.06,
        "support_ratio_min": 0.80,
        "support_tolerance_m": 0.04,
        "bin_size_m": 0.10,
    }
    config.update(overrides)
    return metrics.evaluate_wall(scene, wall, FakeIndex(*points), **config)


class WallSurfaceMetricsV2Test(unittest.TestCase):
    def test_complete_solid_wall_passes_with_two_dimensional_coverage(self):
        scene, wall = scene_and_wall()
        result = evaluate(scene, wall, wall_points(4.0))
        self.assertEqual(result["status"], "PASS")
        self.assertGreater(result["coverageAreaRatio"], 0.98)
        self.assertGreater(result["verticalCoverageRatio"], 0.98)
        self.assertGreater(result["twoFaceSupportRatio"], 0.98)
        self.assertLess(result["surfaceResidualP99M"], 0.01)

    def test_contiguous_1_2_metre_gap_fails_on_actual_longest_run(self):
        scene, wall = scene_and_wall()
        result = evaluate(scene, wall, wall_points(4.0, omitted_s=[(1.4, 2.6)]))
        self.assertEqual(result["status"], "FAIL")
        self.assertGreaterEqual(result["maxUnsupportedRunM"], 1.1)
        self.assertTrue(result["unsupportedIntervals"])

    def test_same_missing_area_dispersed_as_small_occlusions_is_review(self):
        scene, wall = scene_and_wall()
        result = evaluate(scene, wall, wall_points(4.0, omitted_cells=set(range(1, 40, 3))))
        self.assertEqual(result["status"], "REVIEW")
        self.assertLess(result["maxUnsupportedRunM"], 0.3)

    def test_door_void_is_excluded_only_below_its_head(self):
        door = {"id": "door", "type": "door", "parentId": "wall", "hostOffsetM": 2.0,
                "width": 1.0, "height": 2.1, "sillHeight": 0.0}
        scene, wall = scene_and_wall(openings=[door])
        result = evaluate(scene, wall, wall_points(4.0, opening=door))
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(any(mask["id"] == "door" and mask["hMaxM"] == 2.1 for mask in result["excludedOpeningMasks"]))

    def test_missing_wall_below_window_is_not_excluded(self):
        window = {"id": "window", "type": "window", "parentId": "wall", "hostOffsetM": 2.0,
                  "width": 1.0, "height": 1.2, "sillHeight": 0.9}
        scene, wall = scene_and_wall(openings=[window])
        x, y, z = wall_points(4.0, opening=window)
        keep = ~((x >= 1.5) & (x <= 2.5) & (z < 0.9))
        result = evaluate(scene, wall, (x[keep], y[keep], z[keep]))
        self.assertEqual(result["status"], "FAIL")
        self.assertGreaterEqual(result["maxUnsupportedRunM"], 0.9)

    def test_points_inside_window_void_do_not_count_as_wall_support(self):
        window = {"id": "window", "type": "window", "parentId": "wall", "hostOffsetM": 2.0,
                  "width": 1.0, "height": 1.2, "sillHeight": 0.9}
        scene, wall = scene_and_wall(openings=[window])
        x = np.repeat(np.arange(1.55, 2.46, 0.1), 20)
        z = np.tile(np.linspace(0.95, 2.05, 20), len(np.arange(1.55, 2.46, 0.1)))
        y = np.tile(np.asarray([-0.102, -0.098] * 10), len(x) // 20)
        result = evaluate(scene, wall, (x, y, z))
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["supportedCellCount"], 0)

    def test_dense_single_face_cabinet_plane_cannot_make_solid_wall_pass(self):
        scene, wall = scene_and_wall()
        points = wall_points(4.0, faces=(0.10,))
        result = evaluate(scene, wall, points)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["twoFaceSupportRatio"], 0.0)
        self.assertIn("two-face-support-ratio<0.050", result["reviewReasons"])

    def test_twenty_two_metre_false_extension_is_blocked_by_longest_run(self):
        scene, wall = scene_and_wall(22.0)
        result = evaluate(scene, wall, wall_points(5.0))
        self.assertEqual(result["status"], "FAIL")
        self.assertGreater(result["maxUnsupportedRunM"], 16.5)

    def test_glass_profile_accepts_sparse_single_face_return_but_reports_profile(self):
        scene, wall = scene_and_wall(kind="glass")
        points = wall_points(4.0, faces=(0.0225,), step=0.20)
        result = evaluate(scene, wall, points)
        self.assertIn(result["status"], {"PASS", "REVIEW"})
        self.assertEqual(result["supportProfile"], "glass-weak-return-v1")
        self.assertEqual(result["twoFaceSupportRatio"], 0.0)


class PointToModelAuditTest(unittest.TestCase):
    def test_accepted_column_explains_its_own_full_height_returns(self):
        scene, _ = scene_and_wall(3.0)
        scene["nodes"]["column"] = {
            "id": "column", "type": "column", "parentId": "level", "center": [2.0, 1.5],
            "size": [0.4, 0.4], "height": 2.8, "yaw": 0.0,
        }
        scene["evidence"]["column"] = {"status": "accepted-measured"}
        wall = wall_points(3.0)
        cx, cy, cz = wall_points(0.3)
        cx = cx + 1.85
        cy = cy + 1.5
        index = FakeIndex(
            np.concatenate([wall[0], cx]),
            np.concatenate([wall[1], cy]),
            np.concatenate([wall[2], cz]),
        )
        result = metrics.evaluate_point_to_model(scene, index, grid_size_m=0.10)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["acceptedColumnCount"], 1)

    def test_presentation_layer_cannot_be_used_as_authority_metric_input(self):
        scene, _ = scene_and_wall(3.0)
        scene["sceneLayer"] = "presentation"
        with self.assertRaisesRegex(ValueError, "POINTCLOUD_METRICS_AUTHORITY_REQUIRED"):
            metrics.evaluate_scene(scene, FakeIndex(*wall_points(3.0)))

    def test_unmodeled_full_height_structure_is_reported(self):
        modeled_scene, _ = scene_and_wall(3.0)
        modeled = wall_points(3.0)
        missing_x, missing_y, missing_z = wall_points(3.0)
        missing_x, missing_y = missing_y + 2.0, missing_x
        index = FakeIndex(
            np.concatenate([modeled[0], missing_x]),
            np.concatenate([modeled[1], missing_y]),
            np.concatenate([modeled[2], missing_z]),
        )
        result = metrics.evaluate_point_to_model(modeled_scene, index, grid_size_m=0.10)
        self.assertEqual(result["status"], "FAIL")
        self.assertGreater(result["unexplainedStructuralAreaM2"], 0.1)
        self.assertGreater(result["maxUnexplainedRunM"], 2.5)


if __name__ == "__main__":
    unittest.main()
