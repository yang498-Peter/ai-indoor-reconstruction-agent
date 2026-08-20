from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import laspy
import numpy as np


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "scene-core"
SKILL_SCRIPTS = ROOT / ".codex" / "skills" / "reconstruct-indoor-scene" / "scripts"
for entry in (CORE, SKILL_SCRIPTS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


capture_index = load("capture_index_readiness_contract", CORE / "capture_index.py")
capture_readiness = load("capture_readiness_readiness_contract", CORE / "capture_readiness.py")
discover_capture = load("discover_capture_readiness_contract", SKILL_SCRIPTS / "discover_capture.py")


COORDINATE_FRAME = {
    "lengthUnit": "metre",
    "upAxis": "Z",
    "reference": "local",
}

NORMALIZED_COORDINATE_FRAME = {
    **COORDINATE_FRAME,
    "unitToMetre": 1.0,
    "referenceType": "local",
}


def write_las(path: Path) -> None:
    header = laspy.LasHeader(point_format=3, version="1.2")
    header.scales = np.asarray([0.001, 0.001, 0.001])
    las = laspy.LasData(header)
    las.x = np.asarray([0.0, 1.0, 2.0, 2.5])
    las.y = np.asarray([0.0, 1.0, 1.5, 2.0])
    las.z = np.asarray([0.0, 1.0, 1.5, 2.5])
    las.red = np.full(4, 20_000, dtype=np.uint16)
    las.green = np.full(4, 30_000, dtype=np.uint16)
    las.blue = np.full(4, 40_000, dtype=np.uint16)
    las.write(path)


def write_transforms(path: Path, image_path: str, translation=(1.0, 1.0, 1.0)) -> None:
    tx, ty, tz = translation
    value = {
        "camera_model": {
            "width": 100,
            "height": 100,
            "fl_x": 50.0,
            "fl_y": 50.0,
            "cx": 50.0,
            "cy": 50.0,
        },
        "frames": [
            {
                "file_path": image_path,
                "transform_matrix": [
                    [1.0, 0.0, 0.0, tx],
                    [0.0, 1.0, 0.0, ty],
                    [0.0, 0.0, 1.0, tz],
                    [0.0, 0.0, 0.0, 1.0],
                ],
            }
        ],
    }
    path.write_text(json.dumps(value), encoding="utf-8")


class CaptureReadinessContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.capture_temp = tempfile.TemporaryDirectory()
        self.output_temp = tempfile.TemporaryDirectory()
        self.capture = Path(self.capture_temp.name)
        self.output = Path(self.output_temp.name)

    def tearDown(self) -> None:
        self.output_temp.cleanup()
        self.capture_temp.cleanup()

    def test_discovery_rejects_a_point_cloud_without_a_supported_adapter(self):
        (self.capture / "scan.e57").write_bytes(b"not-an-e57")
        manifest = discover_capture.build_manifest(
            self.capture,
            coordinate_frame=COORDINATE_FRAME,
        )
        self.assertEqual(manifest["state"], "BLOCKED_UNSUPPORTED_POINT_CLOUD")
        self.assertEqual(manifest["recommended"], None)
        self.assertEqual(manifest["captureUnits"][0]["pointCloudAdapter"]["status"], "UNSUPPORTED")

    def test_coordinate_frame_is_a_fail_closed_input_gate(self):
        write_las(self.capture / "room.las")
        manifest = discover_capture.build_manifest(self.capture)
        self.assertEqual(manifest["state"], "BLOCKED_COORDINATE_FRAME_REQUIRED")
        self.assertIn("coordinate-frame", manifest["blockedCapabilities"])

    def test_capture_fingerprint_uses_content_not_mtime(self):
        write_las(self.capture / "room.las")
        image = self.capture / "frame.jpg"
        image.write_bytes(b"ABCD")
        first = discover_capture.build_manifest(
            self.capture,
            coordinate_frame=COORDINATE_FRAME,
        )
        stat = image.stat()
        image.write_bytes(b"WXYZ")
        os.utime(image, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        second = discover_capture.build_manifest(
            self.capture,
            coordinate_frame=COORDINATE_FRAME,
        )
        self.assertNotEqual(first["captureFingerprint"], second["captureFingerprint"])
        self.assertNotEqual(
            first["recommended"]["images"][0]["contentSha256"],
            second["recommended"]["images"][0]["contentSha256"],
        )

    def test_pose_requires_format_image_binding_and_pointcloud_alignment(self):
        write_las(self.capture / "room.las")
        images = self.capture / "images"
        images.mkdir()
        (images / "frame.jpg").write_bytes(b"jpeg-fixture")
        write_transforms(self.capture / "transforms.json", "images/frame.jpg")
        manifest = discover_capture.build_manifest(
            self.capture,
            coordinate_frame=COORDINATE_FRAME,
        )
        self.assertEqual(manifest["state"], "READY_GEOMETRY_ONLY")
        self.assertEqual(manifest["photoPoseAssociationGate"], "BLOCKED_ALIGNMENT_NOT_RUN")
        self.assertEqual(manifest["poseValidation"]["checks"]["format"], "PASS")
        self.assertEqual(manifest["poseValidation"]["checks"]["imageBindings"], "PASS")

        index_root = self.output / "capture-index"
        capture_index.build_index(self.capture / "room.las", index_root, tile_size_m=1.0)
        index = capture_index.CaptureIndex.open(index_root)
        report = discover_capture.validate_pose_alignment(manifest, index)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["checks"]["pointCloudAlignment"], "PASS")
        self.assertEqual(report["alignedFrameCount"], 1)

    def test_pose_outside_cloud_or_non_rigid_matrix_fails_closed(self):
        write_las(self.capture / "room.las")
        (self.capture / "frame.jpg").write_bytes(b"jpeg-fixture")
        write_transforms(self.capture / "transforms.json", "frame.jpg", translation=(1000.0, 1000.0, 1000.0))
        manifest = discover_capture.build_manifest(
            self.capture,
            coordinate_frame=COORDINATE_FRAME,
        )
        index_root = self.output / "capture-index"
        capture_index.build_index(self.capture / "room.las", index_root, tile_size_m=1.0)
        report = discover_capture.validate_pose_alignment(
            manifest,
            capture_index.CaptureIndex.open(index_root),
        )
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["checks"]["pointCloudAlignment"], "FAIL")

        value = json.loads((self.capture / "transforms.json").read_text(encoding="utf-8"))
        value["frames"][0]["transform_matrix"][0][0] = -1.0
        (self.capture / "transforms.json").write_text(json.dumps(value), encoding="utf-8")
        invalid = discover_capture.build_manifest(
            self.capture,
            coordinate_frame=COORDINATE_FRAME,
        )
        self.assertEqual(invalid["poseValidation"]["checks"]["coordinateConvention"], "FAIL")
        self.assertIn("posed-photo-association", invalid["blockedCapabilities"])

    def test_missing_pose_image_blocks_association(self):
        write_las(self.capture / "room.las")
        write_transforms(self.capture / "transforms.json", "missing.jpg")
        manifest = discover_capture.build_manifest(
            self.capture,
            coordinate_frame=COORDINATE_FRAME,
        )
        self.assertEqual(manifest["poseValidation"]["status"], "FAIL")
        self.assertEqual(manifest["poseValidation"]["checks"]["imageBindings"], "FAIL")
        self.assertIn("posed-photo-association", manifest["blockedCapabilities"])

    def test_manifest_binds_supported_adapter_coordinate_frame_and_index_lineage(self):
        write_las(self.capture / "room.las")
        manifest = discover_capture.build_manifest(
            self.capture,
            coordinate_frame=COORDINATE_FRAME,
        )
        manifest_path = self.output / "capture-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        index_root = self.output / "capture-index"
        index_manifest = capture_index.build_index(
            self.capture / "room.las",
            index_root,
            tile_size_m=1.0,
            capture_manifest=manifest_path,
        )
        self.assertEqual(index_manifest["captureBinding"]["coordinateFrame"], NORMALIZED_COORDINATE_FRAME)
        self.assertEqual(index_manifest["captureBinding"]["sourceSetDigest"], manifest["sourceSetDigest"])
        self.assertEqual(
            index_manifest["lineage"]["rootContentSha256s"],
            [manifest["recommended"]["pointCloud"]["contentSha256"]],
        )

    def test_v1_index_requires_explicit_rebuild(self):
        index_root = self.output / "capture-index"
        index_root.mkdir()
        (index_root / "capture-index.json").write_text(
            json.dumps({"format": "capture-index-v1"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(capture_index.CaptureIndexError, "CAPTURE_INDEX_MIGRATION_REQUIRED"):
            capture_index.CaptureIndex.open(index_root)

    def test_v2_capture_manifest_requires_explicit_rediscovery(self):
        write_las(self.capture / "room.las")
        manifest = discover_capture.build_manifest(
            self.capture,
            coordinate_frame=COORDINATE_FRAME,
        )
        manifest["schemaVersion"] = 2
        manifest_path = self.output / "capture-manifest-v2.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(capture_index.CaptureIndexError, "CAPTURE_MANIFEST_MIGRATION_REQUIRED"):
            capture_index.build_index(
                self.capture / "room.las",
                self.output / "capture-index-v2-manifest",
                capture_manifest=manifest_path,
            )

    def test_whole_scene_acceptance_tracks_geometry_readiness(self):
        write_las(self.capture / "room.las")
        manifest = discover_capture.build_manifest(
            self.capture,
            coordinate_frame=COORDINATE_FRAME,
        )
        self.assertEqual(manifest["state"], "READY_GEOMETRY_ONLY")
        # Whole-scene geometry acceptance depends only on geometric evidence,
        # so a supported cloud in a declared frame must not block it.
        self.assertNotIn("whole-scene-acceptance", manifest["blockedCapabilities"])
        self.assertIn("material-acceptance", manifest["blockedCapabilities"])
        self.assertIn("posed-photo-association", manifest["blockedCapabilities"])

        undeclared = discover_capture.build_manifest(self.capture)
        self.assertEqual(undeclared["state"], "BLOCKED_COORDINATE_FRAME_REQUIRED")
        self.assertIn("whole-scene-acceptance", undeclared["blockedCapabilities"])

    def test_ready_full_when_no_capability_is_blocked(self):
        write_las(self.capture / "room.las")
        (self.capture / "frame.jpg").write_bytes(b"jpeg-fixture")
        passing_pose = {
            "schemaVersion": "1.0",
            "artifactType": "pose-validation",
            "status": "PASS",
            "sourceSetDigest": "0" * 64,
            "checks": {
                "format": "PASS",
                "coordinateConvention": "PASS",
                "imageBindings": "PASS",
                "pointCloudAlignment": "PASS",
            },
            "sources": [],
            "frames": [],
            "errors": [],
        }
        with mock.patch.object(
            discover_capture, "validate_pose_sources", return_value=passing_pose
        ):
            manifest = discover_capture.build_manifest(
                self.capture,
                coordinate_frame=COORDINATE_FRAME,
            )
        self.assertEqual(manifest["state"], "READY_FULL")
        self.assertEqual(manifest["blockedCapabilities"], [])
        self.assertEqual(manifest["photoPoseAssociationGate"], "PASS")

    def test_non_metre_input_is_normalized_to_source_metres_in_the_index(self):
        write_las(self.capture / "room.las")
        manifest = discover_capture.build_manifest(
            self.capture,
            coordinate_frame={"lengthUnit": "foot", "upAxis": "Z", "reference": "local-feet"},
        )
        manifest_path = self.output / "capture-manifest-feet.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        index_root = self.output / "capture-index-feet"
        index_manifest = capture_index.build_index(
            self.capture / "room.las",
            index_root,
            tile_size_m=1.0,
            capture_manifest=manifest_path,
        )
        self.assertAlmostEqual(index_manifest["sourceUnitToMetre"], 0.3048, places=8)
        self.assertAlmostEqual(index_manifest["bounds"]["maxX"], 2.5 * 0.3048, places=6)
        points = capture_index.CaptureIndex.open(index_root).query_all()
        self.assertAlmostEqual(float(np.max(points.x)), 2.5 * 0.3048, places=5)


def look_at_c2w(position, target, up=(0.0, 0.0, 1.0)) -> np.ndarray:
    """OpenGL camera-to-world (camera looks along -Z, +Y up)."""
    position = np.asarray(position, dtype=np.float64)
    forward = np.asarray(target, dtype=np.float64) - position
    forward = forward / np.linalg.norm(forward)
    z_axis = -forward
    x_axis = np.cross(forward, np.asarray(up, dtype=np.float64))
    x_axis = x_axis / np.linalg.norm(x_axis)
    y_axis = np.cross(z_axis, x_axis)
    c2w = np.eye(4)
    c2w[:3, 0] = x_axis
    c2w[:3, 1] = y_axis
    c2w[:3, 2] = z_axis
    c2w[:3, 3] = position
    return c2w


class PoseReprojectionGateTest(unittest.TestCase):
    """Per-frame quantified pose gate (P2-3): usable frames pass individually."""

    GOOD_POSES = [
        ((2.0, -1.0, 1.5), (2.0, 2.0, 1.5)),
        ((2.0, -1.5, 2.0), (2.0, 2.0, 1.5)),
    ]

    def setUp(self) -> None:
        self.capture_temp = tempfile.TemporaryDirectory()
        self.output_temp = tempfile.TemporaryDirectory()
        self.capture = Path(self.capture_temp.name)
        self.output = Path(self.output_temp.name)

    def tearDown(self) -> None:
        self.output_temp.cleanup()
        self.capture_temp.cleanup()

    def _write_colored_las(self, path: Path, with_color: bool = True) -> np.ndarray:
        # a vertical gradient plane at y=2 plus a gray ground patch
        grid = np.linspace(0.0, 4.0, 60)
        plane_x, plane_z = np.meshgrid(grid, grid)
        plane = np.stack(
            [plane_x.ravel(), np.full(plane_x.size, 2.0), plane_z.ravel()], axis=1
        )
        gx, gy = np.meshgrid(grid, np.linspace(-2.0, 2.0, 40))
        ground = np.stack([gx.ravel(), gy.ravel(), np.zeros(gx.size)], axis=1)
        points = np.concatenate([plane, ground], axis=0)
        colors = np.concatenate(
            [
                np.stack(
                    [
                        plane[:, 0] / 4.0 * 220.0,
                        plane[:, 2] / 4.0 * 220.0,
                        np.full(len(plane), 128.0),
                    ],
                    axis=1,
                ),
                np.full((len(ground), 3), 100.0),
            ],
            axis=0,
        ).astype(np.uint16)
        header = laspy.LasHeader(point_format=3 if with_color else 0, version="1.2")
        header.scales = np.asarray([0.001, 0.001, 0.001])
        las = laspy.LasData(header)
        las.x, las.y, las.z = points[:, 0], points[:, 1], points[:, 2]
        if with_color:
            las.red = colors[:, 0] * 257
            las.green = colors[:, 1] * 257
            las.blue = colors[:, 2] * 257
        las.write(path)
        self._points = points
        self._colors = colors.astype(np.float64)
        return points

    def _render_view(self, frame, invert: bool = False):
        from PIL import Image

        photo_projection = sys.modules["photo_projection"]
        uv, depth, in_front = photo_projection.project_points(frame, self._points)
        visible = in_front & photo_projection.in_image(frame, uv)
        image = np.full((400, 400, 3), 120, dtype=np.uint8)
        order = np.argsort(depth[visible])[::-1]
        u = uv[visible, 0].astype(int)[order]
        v = uv[visible, 1].astype(int)[order]
        colors = self._colors[visible][order]
        if invert:
            colors = 255.0 - colors
        for du in range(-4, 5):
            for dv in range(-4, 5):
                pu = np.clip(u + du, 0, 399)
                pv = np.clip(v + dv, 0, 399)
                image[pv, pu] = colors
        return Image.fromarray(image)

    def _build_capture(self, *, with_color: bool = True):
        import photo_projection  # noqa: F401  (registers module for _render_view)

        self._write_colored_las(self.capture / "room.las", with_color=with_color)
        images = self.capture / "images"
        images.mkdir()
        cameras = list(self.GOOD_POSES) + [
            self.GOOD_POSES[0],  # right pose, wrong image content (inverted)
            ((1000.0, 1000.0, 1000.0), (1002.0, 1002.0, 1000.0)),  # far away
        ]
        frames = []
        model = {
            "width": 400,
            "height": 400,
            "intrinsic": [[200, 0, 200], [0, 200, 200], [0, 0, 1]],
        }
        camera_model = sys.modules["photo_projection"].CameraModel(
            width=400, height=400, fx=200, fy=200, cx=200, cy=200
        )
        for index, (position, target) in enumerate(cameras):
            c2w = look_at_c2w(position, target)
            frame = sys.modules["photo_projection"].Frame(
                frame_id=f"synthetic#{index}",
                file_path=f"images/frame{index}.png",
                c2w=c2w,
                camera=camera_model,
            )
            self._render_view(frame, invert=index == 2).save(
                images / f"frame{index}.png"
            )
            frames.append(
                {
                    "file_path": f"images/frame{index}.png",
                    "timestamp": 1000 + index,
                    "transform_matrix": c2w.tolist(),
                }
            )
        (self.capture / "transforms.json").write_text(
            json.dumps({"undistort_camera_model": model, "frames": frames}),
            encoding="utf-8",
        )
        manifest = discover_capture.build_manifest(
            self.capture, coordinate_frame=COORDINATE_FRAME
        )
        index_root = self.output / "capture-index"
        capture_index.build_index(self.capture / "room.las", index_root, tile_size_m=2.0)
        return manifest, capture_index.CaptureIndex.open(index_root)

    def test_frames_pass_individually_and_batch_survives_bad_poses(self):
        manifest, index = self._build_capture()
        report = capture_readiness.validate_pose_reprojection(
            manifest,
            index,
            capture_root=self.capture,
            block_px=8,
            min_blocks=40,
        )
        by_id = {frame["frameId"]: frame for frame in report["frames"]}
        self.assertEqual(by_id["transforms.json#0"]["status"], "USABLE")
        self.assertEqual(by_id["transforms.json#1"]["status"], "USABLE")
        self.assertEqual(by_id["transforms.json#2"]["status"], "REJECTED_LOW_SCORE")
        self.assertEqual(by_id["transforms.json#3"]["status"], "REJECTED_COARSE")
        self.assertGreater(by_id["transforms.json#0"]["reprojectionScore"], 0.5)
        # 2 usable of 4 with required = min(20, ceil(0.3*4)) = 2 -> PASS even
        # though the coarse 80% all-or-nothing rule fails this batch
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["checks"]["pointCloudAlignment"], "FAIL")
        self.assertEqual(report["checks"]["reprojectionPerFrame"], "PASS")
        self.assertEqual(report["usableFrames"], ["transforms.json#0", "transforms.json#1"])
        self.assertEqual(report["usableFrameCount"], 2)
        self.assertEqual(report["requiredUsableFrameCount"], 2)
        self.assertEqual(
            {row["frameId"] for row in report["rejectedFrames"]},
            {"transforms.json#2", "transforms.json#3"},
        )

    def test_pose_validation_v1_schema_fields_are_preserved(self):
        manifest, index = self._build_capture()
        report = capture_readiness.validate_pose_reprojection(
            manifest,
            index,
            capture_root=self.capture,
            block_px=8,
            min_blocks=40,
        )
        self.assertEqual(report["schemaVersion"], "1.0")
        self.assertEqual(report["artifactType"], "pose-validation")
        for key in ("format", "coordinateConvention", "imageBindings", "pointCloudAlignment"):
            self.assertIn(key, report["checks"])
        for frame in report["frames"]:
            for key in ("frameId", "center", "aligned", "insideExpandedPointCloudBounds"):
                self.assertIn(key, frame)
        self.assertIn("alignedFrameCount", report)
        self.assertIn("indexFingerprint", report)

    def test_all_frames_below_threshold_fails_closed(self):
        manifest, index = self._build_capture()
        report = capture_readiness.validate_pose_reprojection(
            manifest,
            index,
            capture_root=self.capture,
            block_px=8,
            min_blocks=40,
            min_score=0.999,
        )
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["usableFrames"], [])
        self.assertEqual(report["checks"]["reprojectionPerFrame"], "FAIL")

    def test_colorless_cloud_falls_back_to_geometry_only(self):
        manifest, index = self._build_capture(with_color=False)
        report = capture_readiness.validate_pose_reprojection(
            manifest,
            index,
            capture_root=self.capture,
            block_px=8,
            min_blocks=40,
        )
        by_id = {frame["frameId"]: frame for frame in report["frames"]}
        self.assertEqual(by_id["transforms.json#0"]["status"], "USABLE_GEOMETRY_ONLY")
        self.assertIsNone(by_id["transforms.json#0"]["reprojectionScore"])
        self.assertFalse(report["reprojection"]["hasColor"])

    def test_discovery_not_ready_fails_closed(self):
        report = capture_readiness.validate_pose_reprojection(
            {"poseValidation": {"status": "FAIL"}},
            _EmptyIndex(),
            capture_root=self.capture,
        )
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["usableFrames"], [])
        self.assertIn("POSE_DISCOVERY_NOT_READY", report["errors"])


class _EmptyIndex:
    manifest = {"bounds": {}, "indexFingerprint": "0" * 64, "indexedPointCount": 0}


if __name__ == "__main__":
    unittest.main()
