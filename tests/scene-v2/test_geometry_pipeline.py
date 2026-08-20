from __future__ import annotations

import hashlib
import importlib.util
import math
import os
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
metrics = load("pointcloud_scene_metrics")
scene_api = load("scene_api")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def synthetic_room(path: Path) -> tuple[float, float]:
    """Write a large-coordinate, two-face rectangular room for numeric tests."""
    origin_x = 500_000.0
    origin_y = 7_000_000.0
    point_rows: list[tuple[float, float, float]] = []
    z_values = np.linspace(0.35, 2.55, 12)

    for x in np.linspace(origin_x, origin_x + 6.0, 121):
        for y in (origin_y - 0.10, origin_y + 0.10, origin_y + 3.90, origin_y + 4.10):
            point_rows.extend((x, y, float(z)) for z in z_values)
    for y in np.linspace(origin_y, origin_y + 4.0, 81):
        for x in (origin_x - 0.10, origin_x + 0.10, origin_x + 5.90, origin_x + 6.10):
            point_rows.extend((x, y, float(z)) for z in z_values)

    # A low, dense interior object must not become a structural wall proposal.
    for x in np.linspace(origin_x + 2.2, origin_x + 3.8, 30):
        for y in np.linspace(origin_y + 1.6, origin_y + 2.4, 18):
            point_rows.append((float(x), float(y), 0.75))

    xyz = np.asarray(point_rows, dtype=np.float64)
    header = laspy.LasHeader(point_format=3, version="1.2")
    header.scales = np.asarray([0.001, 0.001, 0.001])
    header.offsets = np.asarray([origin_x, origin_y, 0.0])
    las = laspy.LasData(header)
    las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    las.red = np.full(len(xyz), 42_000, dtype=np.uint16)
    las.green = np.full(len(xyz), 35_000, dtype=np.uint16)
    las.blue = np.full(len(xyz), 28_000, dtype=np.uint16)
    las.write(path)
    return origin_x, origin_y


def write_colored_cloud(path: Path, rows: list[tuple[float, float, float]], colors: list[tuple[int, int, int]]) -> None:
    xyz = np.asarray(rows, dtype=np.float64)
    rgb = np.asarray(colors, dtype=np.uint16) * 257
    header = laspy.LasHeader(point_format=3, version="1.2")
    header.scales = np.asarray([0.001, 0.001, 0.001])
    las = laspy.LasData(header)
    las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    las.red, las.green, las.blue = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    las.write(path)


def face_points(y: float, color: tuple[int, int, int]) -> tuple[list, list]:
    rows: list[tuple[float, float, float]] = []
    colors: list[tuple[int, int, int]] = []
    for x in np.linspace(0.0, 6.0, 121):
        for z in np.linspace(0.35, 2.55, 12):
            rows.append((float(x), y, float(z)))
            colors.append(color)
    return rows, colors


def cavity_points(y_values: list[float], z_count: int, color: tuple[int, int, int]) -> tuple[list, list]:
    rows: list[tuple[float, float, float]] = []
    colors: list[tuple[int, int, int]] = []
    for x in np.linspace(0.05, 5.95, 61):
        for y in y_values:
            for z in np.linspace(0.4, 2.4, z_count):
                rows.append((float(x), float(y), float(z)))
                colors.append(color)
    return rows, colors


class CaptureIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.las = self.root / "room.las"
        self.origin_x, self.origin_y = synthetic_room(self.las)
        self.source_hash = sha256(self.las)
        self.index_dir = self.root / "capture-index"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_index_is_source_read_only_local_and_large_coordinate_safe(self):
        manifest = capture_index.build_index(
            self.las,
            self.index_dir,
            tile_size_m=2.0,
            every=1,
        )
        self.assertEqual(sha256(self.las), self.source_hash)
        self.assertEqual(manifest["format"], "capture-index-v2")
        self.assertGreater(len(manifest["tiles"]), 4)
        self.assertTrue(all(len(tile["contentSha256"]) == 64 for tile in manifest["tiles"].values()))
        self.assertEqual(manifest["sourceIdentity"]["contentSha256"], self.source_hash)

        index = capture_index.CaptureIndex.open(self.index_dir, validate_source=True)
        result = index.query_bbox(
            self.origin_x + 0.8,
            self.origin_y - 0.3,
            self.origin_x + 1.2,
            self.origin_y + 0.3,
            z_min=0.3,
            z_max=2.6,
        )
        self.assertGreater(result.point_count, 0)
        self.assertLess(result.stats["pointsRead"], manifest["indexedPointCount"])
        self.assertLess(float(np.max(np.abs((result.x - self.origin_x) * 1000 - np.rint((result.x - self.origin_x) * 1000)))), 1e-4)
        self.assertLess(float(np.max(np.abs((result.y - self.origin_y) * 1000 - np.rint((result.y - self.origin_y) * 1000)))), 1e-4)
        self.assertEqual(result.stats["ioMode"], "mmap")

    def test_same_size_tile_tampering_is_rejected_before_query(self):
        manifest = capture_index.build_index(self.las, self.index_dir, tile_size_m=2.0, every=1)
        key, item = next(iter(manifest["tiles"].items()))
        tile = self.index_dir / item["path"]
        payload = bytearray(tile.read_bytes())
        payload[0] ^= 0x01
        tile.write_bytes(payload)
        index = capture_index.CaptureIndex.open(self.index_dir)
        bounds = manifest["bounds"]
        with self.assertRaisesRegex(capture_index.CaptureIndexError, "CAPTURE_INDEX_TILE_CHECKSUM_MISMATCH"):
            index.query_bbox(bounds["minX"], bounds["minY"], bounds["maxX"], bounds["maxY"])

    def test_source_identity_change_invalidates_cache(self):
        capture_index.build_index(self.las, self.index_dir, tile_size_m=2.0, every=1)
        index = capture_index.CaptureIndex.open(self.index_dir, validate_source=True)
        index.validate_source()
        stat = self.las.stat()
        os.utime(self.las, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        with self.assertRaises(capture_index.CaptureIndexError):
            index.validate_source()

    def test_same_size_same_mtime_source_content_change_invalidates_cache(self):
        capture_index.build_index(self.las, self.index_dir, tile_size_m=2.0, every=1)
        index = capture_index.CaptureIndex.open(self.index_dir, validate_source=True)
        stat = self.las.stat()
        payload = bytearray(self.las.read_bytes())
        payload[-1] ^= 0x01
        self.las.write_bytes(payload)
        os.utime(self.las, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with self.assertRaises(capture_index.CaptureIndexError):
            index.validate_source()


class GeometryProposalAndMetricTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.las = self.root / "room.las"
        self.origin_x, self.origin_y = synthetic_room(self.las)
        self.index_dir = self.root / "capture-index"
        capture_index.build_index(self.las, self.index_dir, tile_size_m=2.0, every=1)
        self.index = capture_index.CaptureIndex.open(self.index_dir)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_face_pairing_preserves_raw_measurement_and_suggests_centerline(self):
        faces = [
            proposals.LineObservation.from_endpoints([0.0, -0.1], [6.0, -0.1], support_count=400),
            proposals.LineObservation.from_endpoints([0.0, 0.1], [6.0, 0.1], support_count=380),
        ]
        walls, drift_merged = proposals.pair_wall_faces(faces, min_thickness_m=0.08, max_thickness_m=0.35)
        self.assertEqual(drift_merged, [])
        self.assertEqual(len(walls), 1)
        self.assertAlmostEqual(walls[0]["thicknessM"], 0.2, places=3)
        self.assertAlmostEqual(walls[0]["rawCenterline"]["start"][1], 0.0, places=3)
        self.assertEqual(len(walls[0]["sourceFaceIds"]), 2)

    def test_global_proposals_find_room_axis_families_without_accepting_them(self):
        report = proposals.build_proposals(
            self.index,
            floor_z=0.0,
            band_min_m=0.45,
            band_max_m=2.45,
            raster_cell_m=0.04,
            min_length_m=1.0,
            max_points=100_000,
        )
        angles = [family["angleDeg"] % 180.0 for family in report["axisFamilies"]]
        self.assertTrue(any(min(abs(angle), abs(angle - 180.0)) <= 3.0 for angle in angles))
        self.assertTrue(any(abs(angle - 90.0) <= 3.0 for angle in angles))
        self.assertGreaterEqual(len(report["wallCandidates"]), 4)
        self.assertTrue(all(item["status"] == "candidate" for item in report["wallCandidates"]))
        self.assertTrue(all("rawCenterline" in item and "suggestedCenterline" in item for item in report["wallCandidates"]))

    def _scene_with_south_wall(self, shift_y: float = 0.0) -> dict:
        scene = scene_api.new_scene("synthetic-room", 2.8, 0.0, "author-a")
        level = scene_api.default_level_id(scene)
        scene_api.op_create_wall(scene, {
            "id": "wall_south",
            "start": [self.origin_x, self.origin_y + shift_y],
            "end": [self.origin_x + 6.0, self.origin_y + shift_y],
            "height": 2.8,
            "thickness": 0.2,
            "level": level,
        }, "author-a")
        scene["evidence"]["wall_south"]["status"] = "accepted-measured"
        return scene

    def test_pointcloud_metric_is_a_hard_gate_and_binds_scene_and_index(self):
        good = metrics.evaluate_scene(
            self._scene_with_south_wall(),
            self.index,
            residual_p90_max_m=0.06,
            support_ratio_min=0.80,
        )
        self.assertEqual(good["status"], "PASS")
        self.assertEqual(good["wallMetrics"][0]["status"], "PASS")
        self.assertEqual(good["indexFingerprint"], self.index.manifest["indexFingerprint"])

        shifted = metrics.evaluate_scene(
            self._scene_with_south_wall(shift_y=0.35),
            self.index,
            residual_p90_max_m=0.06,
            support_ratio_min=0.80,
        )
        self.assertEqual(shifted["status"], "FAIL")
        self.assertTrue(shifted["hardGateFailures"])
        self.assertGreater(shifted["wallMetrics"][0]["residualP90M"], 0.06)


class DriftPhantomWallTest(unittest.TestCase):
    """P1-3 regression: loop-drift twin faces must merge, real walls must pair."""

    BASE_COLOR = (180, 150, 120)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _index_for(self, rows: list, colors: list, name: str = "cloud"):
        las = self.root / f"{name}.las"
        write_colored_cloud(las, rows, colors)
        index_dir = self.root / f"{name}-index"
        capture_index.build_index(las, index_dir, tile_size_m=2.0, every=1)
        return capture_index.CaptureIndex.open(index_dir)

    def _faces(self):
        return [
            proposals.LineObservation.from_endpoints(
                [0.0, 0.0], [6.0, 0.0], support_count=1452, observation_id="face-a"
            ),
            proposals.LineObservation.from_endpoints(
                [0.0, 0.15], [6.0, 0.15], support_count=1452, observation_id="face-b"
            ),
        ]

    def _pair(self, index):
        return proposals.pair_wall_faces(
            self._faces(),
            index=index,
            z_min=0.3,
            z_max=2.6,
            min_thickness_m=0.08,
            max_thickness_m=0.35,
        )

    def test_drift_double_line_merges_into_single_face(self):
        rows, colors = face_points(0.0, self.BASE_COLOR)
        more, more_colors = face_points(0.15, self.BASE_COLOR)
        rows += more
        colors += more_colors
        # The drift smear leaves points all through the would-be wall cavity.
        cavity, cavity_colors = cavity_points(list(np.linspace(0.04, 0.11, 5)), 6, self.BASE_COLOR)
        rows += cavity
        colors += cavity_colors
        walls, merged = self._pair(self._index_for(rows, colors))
        self.assertEqual(walls, [])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["driftMergedFrom"], ["face-a", "face-b"])
        self.assertGreater(merged[0]["cavityPointRatio"], 0.25)
        self.assertIsNotNone(merged[0]["faceColorDeltaNorm"])
        self.assertLess(merged[0]["faceColorDeltaNorm"], 0.12)
        observation = merged[0]["observation"]
        # Equal support -> the merged face sits at the support-weighted middle.
        self.assertAlmostEqual(float(observation.start[1]), 0.075, places=3)
        self.assertAlmostEqual(float(observation.end[1]), 0.075, places=3)
        self.assertEqual(observation.support_count, 2904)

    def test_true_wall_with_empty_cavity_still_pairs(self):
        rows, colors = face_points(0.0, self.BASE_COLOR)
        more, more_colors = face_points(0.15, self.BASE_COLOR)
        rows += more
        colors += more_colors
        walls, merged = self._pair(self._index_for(rows, colors))
        self.assertEqual(merged, [])
        self.assertEqual(len(walls), 1)
        self.assertAlmostEqual(walls[0]["thicknessM"], 0.15, places=3)
        self.assertLess(walls[0]["cavityPointRatio"], 0.05)
        self.assertIsNotNone(walls[0]["faceColorDeltaNorm"])
        self.assertGreater(walls[0]["confidence"], 0.9)

    def test_color_consistency_weights_gray_zone_but_never_decides_alone(self):
        gray_cavity = [0.05, 0.10]

        rows, colors = face_points(0.0, self.BASE_COLOR)
        more, more_colors = face_points(0.15, self.BASE_COLOR)
        cavity, cavity_colors = cavity_points(gray_cavity, 7, self.BASE_COLOR)
        walls, merged = self._pair(self._index_for(rows + more + cavity, colors + more_colors + cavity_colors))
        self.assertEqual(walls, [])
        self.assertEqual(len(merged), 1)
        self.assertLess(merged[0]["cavityPointRatio"], 0.25)

        rows, colors = face_points(0.0, (200, 60, 60))
        more, more_colors = face_points(0.15, (60, 60, 200))
        cavity, cavity_colors = cavity_points(gray_cavity, 7, (200, 60, 60))
        walls, merged = self._pair(
            self._index_for(rows + more + cavity, colors + more_colors + cavity_colors, name="two-tone")
        )
        self.assertEqual(merged, [])
        self.assertEqual(len(walls), 1)
        self.assertGreater(walls[0]["faceColorDeltaNorm"], 0.12)
        self.assertLess(walls[0]["cavityPointRatio"], 0.25)
        # The residual cavity occupancy still discounts the pair's confidence.
        self.assertLess(walls[0]["confidence"], 0.75)

    def test_build_proposals_reports_drift_merge_and_no_phantom_wall(self):
        rows, colors = face_points(0.0, self.BASE_COLOR)
        more, more_colors = face_points(0.15, self.BASE_COLOR)
        cavity, cavity_colors = cavity_points(list(np.linspace(0.04, 0.11, 5)), 6, self.BASE_COLOR)
        index = self._index_for(rows + more + cavity, colors + more_colors + cavity_colors)
        report = proposals.build_proposals(
            index,
            floor_z=0.0,
            band_min_m=0.45,
            band_max_m=2.45,
            raster_cell_m=0.04,
            min_length_m=1.0,
            max_points=100_000,
        )
        candidates = report["wallCandidates"]
        self.assertTrue(any("driftMergedFrom" in candidate for candidate in candidates))
        self.assertFalse(any(candidate["wallMode"] == "paired-faces" for candidate in candidates))
        self.assertTrue(any("loop-drift" in warning for warning in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
