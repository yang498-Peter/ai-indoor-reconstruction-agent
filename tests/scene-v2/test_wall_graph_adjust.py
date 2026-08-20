from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np


CORE = Path(__file__).resolve().parents[2] / "scene-core"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, CORE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


adjust = load("wall_graph_adjust")


def perturbed_wall(start, end, angle_deg: float, offset_m: float):
    """Rotate a wall about its midpoint and shift it along its normal."""
    first = np.asarray(start, dtype=np.float64)
    second = np.asarray(end, dtype=np.float64)
    mid = (first + second) * 0.5
    theta = math.radians(angle_deg)
    rotation = np.asarray([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])
    direction = (second - first) / np.linalg.norm(second - first)
    normal = np.asarray([-direction[1], direction[0]])
    return mid + rotation @ (first - mid) + normal * offset_m, mid + rotation @ (second - mid) + normal * offset_m


RECT_SPEC = [
    ("wall-south", (0.0, 0.0), (6.0, 0.0), 2.0, 0.05, 0.12),
    ("wall-east", (6.0, 0.0), (6.0, 4.0), -1.5, -0.04, 0.13),
    ("wall-north", (0.0, 4.0), (6.0, 4.0), -2.0, -0.05, 0.12),
    ("wall-west", (0.0, 0.0), (0.0, 4.0), 1.8, 0.03, 0.14),
]

# corner -> the two walls whose endpoints must close there
RECT_CORNERS = {
    (0.0, 0.0): ("wall-south", "wall-west"),
    (6.0, 0.0): ("wall-south", "wall-east"),
    (6.0, 4.0): ("wall-east", "wall-north"),
    (0.0, 4.0): ("wall-north", "wall-west"),
}


def rectangle_candidates(wall_ids: set[str] | None = None) -> list[dict]:
    rows = []
    for wall_id, start, end, angle_deg, offset_m, thickness in RECT_SPEC:
        if wall_ids is not None and wall_id not in wall_ids:
            continue
        noisy_start, noisy_end = perturbed_wall(start, end, angle_deg, offset_m)
        rows.append(
            {
                "id": wall_id,
                "rawCenterline": {
                    "start": [float(v) for v in noisy_start],
                    "end": [float(v) for v in noisy_end],
                },
                "thicknessM": thickness,
                "supportPointCount": 800,
                "fitResidualP90M": 0.02,
            }
        )
    return rows


def wall_row(report: dict, wall_id: str) -> dict:
    for row in report["walls"]:
        if row["wallId"] == wall_id:
            return row
    raise AssertionError(f"wall {wall_id} missing from adjustment report")


def endpoint_near(row: dict, corner) -> np.ndarray:
    target = np.asarray(corner, dtype=np.float64)
    points = [np.asarray(row["after"]["start"]), np.asarray(row["after"]["end"])]
    return min(points, key=lambda point: float(np.linalg.norm(point - target)))


class WallGraphAdjustTest(unittest.TestCase):
    def test_noisy_rectangle_recovers_corners_and_right_angles(self):
        report = adjust.adjust_from_documents({"wallCandidates": rectangle_candidates()})
        self.assertEqual(report["status"], "ADJUSTMENT_PROPOSED")
        self.assertTrue(report["summary"]["converged"])
        self.assertEqual(report["summary"]["freeWallCount"], 4)
        self.assertGreaterEqual(report["constraintCounts"]["corner"], 4)
        self.assertEqual(report["constraintCounts"]["axis"], 4)

        for corner, (first_id, second_id) in RECT_CORNERS.items():
            first_point = endpoint_near(wall_row(report, first_id), corner)
            second_point = endpoint_near(wall_row(report, second_id), corner)
            gap = float(np.linalg.norm(first_point - second_point))
            self.assertLess(gap, 0.02, f"corner {corner} gap {gap:.4f} m")
        self.assertLess(report["summary"]["maxCornerGapAfterM"], 0.02)
        self.assertGreater(report["summary"]["maxCornerGapBeforeM"], 0.05)

        south_angle = wall_row(report, "wall-south")["after"]["angleDeg"]
        west_angle = wall_row(report, "wall-west")["after"]["angleDeg"]
        north_angle = wall_row(report, "wall-north")["after"]["angleDeg"]
        right_angle = abs((west_angle - south_angle) % 180.0)
        self.assertLess(abs(right_angle - 90.0), 0.2)
        parallel = abs((north_angle - south_angle + 90.0) % 180.0 - 90.0)
        self.assertLess(parallel, 0.2)

        for row in report["walls"]:
            self.assertIn("deltaStartM", row)
            self.assertIn("deltaEndM", row)
            self.assertIn("deltaAngleDeg", row)
            self.assertTrue(row["constraints"])
            # The solve corrects noise, it must not teleport the wall.
            self.assertLess(row["deltaStartM"], 0.30)

    def test_thickness_family_shrinks_toward_group_median(self):
        report = adjust.adjust_from_documents({"wallCandidates": rectangle_candidates()})
        self.assertEqual(report["constraintCounts"]["thicknessGroups"], 1)
        west = wall_row(report, "wall-west")
        # 0.14 belongs to the 0.12/0.12/0.13/0.14 family (median 0.125) and
        # must shrink toward it without being forced all the way.
        self.assertLess(west["after"]["thicknessM"], 0.14)
        self.assertGreater(west["after"]["thicknessM"], 0.125 - 1e-6)

    def test_anchored_wall_stays_fixed_and_attracts_free_corners(self):
        scene = {
            "nodes": {
                "wall_south": {
                    "type": "wall",
                    "start": [0.0, 0.0],
                    "end": [6.0, 0.0],
                    "thickness": 0.12,
                }
            },
            "evidence": {"wall_south": {"status": "accepted-measured"}},
        }
        proposals_doc = {"wallCandidates": rectangle_candidates({"wall-east", "wall-north", "wall-west"})}
        report = adjust.adjust_from_documents(proposals_doc, scene)
        self.assertTrue(report["summary"]["converged"])
        self.assertEqual(report["summary"]["anchorWallCount"], 1)

        south = wall_row(report, "wall_south")
        self.assertTrue(south["anchored"])
        self.assertEqual(south["deltaStartM"], 0.0)
        self.assertEqual(south["deltaEndM"], 0.0)
        self.assertEqual(south["after"], south["before"])

        west_corner = endpoint_near(wall_row(report, "wall-west"), (0.0, 0.0))
        self.assertLess(float(np.linalg.norm(west_corner - np.asarray([0.0, 0.0]))), 0.02)
        east_corner = endpoint_near(wall_row(report, "wall-east"), (6.0, 0.0))
        self.assertLess(float(np.linalg.norm(east_corner - np.asarray([6.0, 0.0]))), 0.02)

    def test_cli_writes_reviewable_proposal_document(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proposals_path = root / "structural-proposals.json"
            proposals_path.write_text(
                json.dumps({"wallCandidates": rectangle_candidates()}), encoding="utf-8"
            )
            output_path = root / "wall-graph-adjustment.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CORE / "wall_graph_adjust.py"),
                    f"--proposals={proposals_path}",
                    f"--output={output_path}",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ADJUSTMENT_PROPOSED")
            self.assertEqual(report["kind"], "wall-graph-adjustment")
            self.assertEqual(len(report["walls"]), 4)
            self.assertIn("authorityRule", report)
            summary = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertTrue(summary["ok"])


if __name__ == "__main__":
    unittest.main()
