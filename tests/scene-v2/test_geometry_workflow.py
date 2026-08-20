from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "scene-core"
SKILL_SCRIPTS = ROOT / ".codex" / "skills" / "reconstruct-indoor-scene" / "scripts"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import discover_capture


def load_support():
    path = Path(__file__).with_name("test_geometry_pipeline.py")
    spec = importlib.util.spec_from_file_location("geometry_workflow_test_support", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


support = load_support()
workflow = support.load("geometry_workflow")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GeometryWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.capture_temp = tempfile.TemporaryDirectory()
        self.derivative_temp = tempfile.TemporaryDirectory()
        self.capture_root = Path(self.capture_temp.name)
        self.derivative_root = Path(self.derivative_temp.name)
        self.las = self.capture_root / "room.las"
        self.origin_x, self.origin_y = support.synthetic_room(self.las)
        self.source_hash = sha256(self.las)
        self.workspace = self.derivative_root / "geometry-workspace"

    def tearDown(self) -> None:
        self.derivative_temp.cleanup()
        self.capture_temp.cleanup()

    def test_prepare_then_evaluate_is_traceable_and_fail_closed(self):
        prepared = workflow.prepare_workspace(
            self.las,
            self.workspace,
            floor_z=0.0,
            ceiling_z=2.8,
            tile_size_m=2.0,
            overview_cell_m=0.10,
            proposal_cell_m=0.04,
            proposal_max_points=100_000,
        )
        self.assertEqual(sha256(self.las), self.source_hash)
        self.assertEqual(prepared["status"], "READY_FOR_AGENT_REVIEW")
        self.assertEqual(prepared["gates"]["structuralProposals"], "CANDIDATES_ONLY")
        self.assertIn(
            prepared["gates"]["candidateTopology"],
            {"CANDIDATE_SELECTION_PROPOSED", "REVIEW_REQUIRED"},
        )
        self.assertEqual(prepared["gates"]["publication"], "BLOCKED")
        self.assertTrue((self.workspace / "capture-index" / "capture-index.json").is_file())
        self.assertTrue((self.workspace / "evidence" / "evidence-manifest.json").is_file())
        self.assertTrue((self.workspace / "structural-proposals.json").is_file())
        self.assertTrue((self.workspace / "candidate-topology.json").is_file())

        proposals = json.loads((self.workspace / "structural-proposals.json").read_text(encoding="utf-8"))
        self.assertEqual(proposals["schemaVersion"], 2)
        self.assertTrue(all(item["status"] == "candidate" for item in proposals["wallCandidates"]))
        self.assertTrue(all(item["status"] == "candidate" for item in proposals["wallHypotheses"]))
        for hypothesis in proposals["wallHypotheses"]:
            expected = 2 if hypothesis["wallMode"] == "single-face" else 1
            self.assertEqual(len(hypothesis["centerlineAlternatives"]), expected)
        self.assertTrue(os.path.samefile(proposals["index"], self.workspace / "capture-index"))
        candidate_topology = json.loads((self.workspace / "candidate-topology.json").read_text(encoding="utf-8"))
        self.assertEqual(candidate_topology["authorityRule"], "proposal-only-agent-review-required")
        self.assertEqual(candidate_topology["profile"]["id"], "default-office-v1")
        self.assertEqual(candidate_topology["inputHashes"]["proposalsSha256"], sha256(self.workspace / "structural-proposals.json"))

        scene = support.scene_api.new_scene("workflow-room", 2.8, 0.0, "reviewer-a")
        level = support.scene_api.default_level_id(scene)
        walls = {
            "wall_south": ([self.origin_x, self.origin_y], [self.origin_x + 6.0, self.origin_y]),
            "wall_north": ([self.origin_x, self.origin_y + 4.0], [self.origin_x + 6.0, self.origin_y + 4.0]),
            "wall_west": ([self.origin_x, self.origin_y], [self.origin_x, self.origin_y + 4.0]),
            "wall_east": ([self.origin_x + 6.0, self.origin_y], [self.origin_x + 6.0, self.origin_y + 4.0]),
        }
        for wall_id, (start, end) in walls.items():
            support.scene_api.op_create_wall(
                scene,
                {"id": wall_id, "start": start, "end": end, "height": 2.8, "thickness": 0.2, "level": level},
                "reviewer-a",
            )
            scene["evidence"][wall_id]["status"] = "accepted-measured"
        scene_path = self.derivative_root / "scene-authority.json"
        scene_path.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
        report_path = self.derivative_root / "pointcloud-scene-metrics.json"
        report = workflow.evaluate_authority_scene(scene_path, self.workspace, report_path)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["indexFingerprint"], prepared["indexFingerprint"])
        self.assertEqual(report["sceneSha256"], sha256(scene_path))
        self.assertTrue(report_path.is_file())
        self.assertEqual(report["schemaVersion"], "2.0")
        self.assertFalse(any(report_path.parent.glob(f".{report_path.name}.tmp-*")))

    def test_evaluate_does_not_upgrade_a_scene_without_measured_walls(self):
        workflow.prepare_workspace(
            self.las,
            self.workspace,
            floor_z=0.0,
            ceiling_z=2.8,
            tile_size_m=2.0,
            overview_cell_m=0.10,
            proposal_cell_m=0.04,
            proposal_max_points=100_000,
        )
        scene = support.scene_api.new_scene("empty", 2.8, 0.0, "reviewer-a")
        scene_path = self.derivative_root / "empty-scene.json"
        scene_path.write_text(json.dumps(scene), encoding="utf-8")
        report = workflow.evaluate_authority_scene(
            scene_path,
            self.workspace,
            self.derivative_root / "empty-metrics.json",
        )
        self.assertEqual(report["status"], "NOT_RUN")
        self.assertIn("no-accepted-measured-walls", report["hardGateFailures"])

    def test_prepare_writes_a_hash_bound_pose_alignment_artifact(self):
        image = self.capture_root / "frame.jpg"
        image.write_bytes(b"jpeg-fixture")
        transforms = {
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
                    "file_path": "frame.jpg",
                    "transform_matrix": [
                        [1.0, 0.0, 0.0, self.origin_x + 1.0],
                        [0.0, 1.0, 0.0, self.origin_y + 1.0],
                        [0.0, 0.0, 1.0, 1.0],
                        [0.0, 0.0, 0.0, 1.0],
                    ],
                }
            ],
        }
        (self.capture_root / "transforms.json").write_text(json.dumps(transforms), encoding="utf-8")
        manifest = discover_capture.build_manifest(
            self.capture_root,
            coordinate_frame={"lengthUnit": "metre", "upAxis": "Z", "reference": "local-test"},
        )
        manifest_path = self.derivative_root / "capture-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        prepared = workflow.prepare_workspace(
            self.las,
            self.workspace,
            floor_z=0.0,
            ceiling_z=2.8,
            capture_manifest=manifest_path,
            tile_size_m=2.0,
            overview_cell_m=0.10,
            proposal_cell_m=0.04,
            proposal_max_points=100_000,
        )
        pose_path = self.workspace / "pose-validation.json"
        pose = json.loads(pose_path.read_text(encoding="utf-8"))
        self.assertEqual(prepared["gates"]["photoPoseValidation"], "PASS")
        self.assertEqual(pose["checks"]["pointCloudAlignment"], "PASS")
        self.assertEqual(pose["inputs"][1]["payloadDigest"], prepared["indexFingerprint"])
        self.assertEqual(prepared["lineage"]["sourceSetDigest"], manifest["sourceSetDigest"])


if __name__ == "__main__":
    unittest.main()
