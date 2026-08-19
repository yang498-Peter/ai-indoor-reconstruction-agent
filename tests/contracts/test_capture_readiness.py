from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
