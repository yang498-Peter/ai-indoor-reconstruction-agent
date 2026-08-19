from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCENE_CORE = ROOT / "scene-core"
QUALITY_MODULE = SCENE_CORE / "quality_report_v2.py"
QUALITY_SCHEMA = SCENE_CORE / "quality-report-v2.schema.json"
SCENE_API_MODULE = SCENE_CORE / "scene_api.py"
LOOP_MODULE = (
    ROOT
    / ".codex"
    / "skills"
    / "reconstruct-indoor-scene"
    / "scripts"
    / "reconstruction_loop.py"
)


def load_module(name: str, path: Path):
    if not path.is_file():
        raise AssertionError(f"required contract implementation is missing: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = load_module("scene_api_contract", SCENE_API_MODULE)


class V2PublishContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.quality = load_module("quality_report_v2_contract", QUALITY_MODULE)
        self.loop = load_module("reconstruction_loop_contract", LOOP_MODULE)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.evidence_path = self.root / "evidence" / "wall-section.bin"
        self.evidence_path.parent.mkdir(parents=True)
        self.evidence_path.write_bytes(b"deterministic-wall-section")
        self.scene = self._make_publishable_scene()
        self.scene_path = self.root / "scene-authority.json"
        self.scene_path.write_text(
            json.dumps(self.scene, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.review_path = self.root / "review-receipt.json"
        self.review_path.write_text(
            json.dumps(self._review_receipt(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _make_publishable_scene(self) -> dict:
        scene = api.new_scene("synthetic-v2", 3.0, 0.0, "author-run")
        level = scene["rootNodeIds"][0]
        walls = [
            ("wall_a", [0.0, 0.0], [4.0, 0.0]),
            ("wall_b", [4.0, 0.0], [4.0, 3.0]),
            ("wall_c", [4.0, 3.0], [0.0, 3.0]),
            ("wall_d", [0.0, 3.0], [0.0, 0.0]),
        ]
        source = {
            "type": "point-measurement",
            "path": "evidence/wall-section.bin",
            "sha256": hashlib.sha256(b"deterministic-wall-section").hexdigest(),
        }
        for node_id, start, end in walls:
            api.op_create_wall(
                scene,
                {
                    "id": node_id,
                    "level": level,
                    "start": start,
                    "end": end,
                    "height": 3.0,
                    "thickness": 0.12,
                },
                "author-run",
            )
            scene["evidence"][node_id] = {
                "status": "accepted-measured",
                "sources": [dict(source)],
                "reviewer": "reviewer-run",
            }
        scene["review"]["topology"]["spaces"] = [
            {"id": "space_main", "boundaryNodeIds": [wall[0] for wall in walls]}
        ]
        api.validate_scene(scene)
        return scene

    def _review_receipt(self) -> dict:
        return {
            "schemaVersion": "2.0",
            "geometryDigest": api.geometry_digest(self.scene),
            "evidenceSetDigest": api.evidence_set_digest(self.scene),
            "artifactSha256": hashlib.sha256(self.scene_path.read_bytes()).hexdigest(),
            "reviewer": {
                "actorId": "reviewer-run",
                "runId": "11111111-1111-4111-8111-111111111111",
                "role": "reviewer",
                "provider": "deterministic-checker",
            },
            "reviewedAt": datetime.now(timezone.utc).isoformat(),
            "p0": [],
            "p1": [],
        }

    def _evaluate(self) -> dict:
        return self.quality.evaluate_files(self.scene_path, self.review_path)

    def test_v2_scene_evaluates_without_legacy_quality_fields(self) -> None:
        report = self._evaluate()
        schema = json.loads(QUALITY_SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(report["schemaVersion"], "2.0")
        self.assertEqual(report["sceneSchemaVersion"], "2.0")
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["checks"]["sceneV2Valid"])
        self.assertNotIn("structures", report["checks"])
        self.assertNotIn("qualityLoops", report["checks"])
        self.assertEqual(set(schema["required"]), set(report))

    def test_legacy_scene_requires_explicit_migration(self) -> None:
        legacy_path = self.root / "legacy-scene.json"
        legacy_path.write_text(
            json.dumps({"structures": [{"id": "Wall01"}]}) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            self.quality.QualityReportError,
            "LEGACY_SCENE_REQUIRES_MIGRATION",
        ):
            self.quality.evaluate_files(legacy_path, self.review_path)

    def test_publish_rejects_legacy_scene_before_reading_quality_fields(self) -> None:
        legacy_path = self.root / "legacy-publish.json"
        legacy_path.write_text(
            json.dumps({"structures": [{"id": "Wall01"}]}) + "\n",
            encoding="utf-8",
        )
        job_path = self.root / "legacy-job.json"
        state_path = self.root / "legacy-state.json"
        job_path.write_text(
            json.dumps(
                {
                    "jobId": "legacy-publish-contract",
                    "captureFingerprint": "b" * 64,
                    "state": "READY_FULL",
                    "blockedCapabilities": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        state = self.loop.initialize_workflow(job_path, state_path)
        legacy_sha = hashlib.sha256(legacy_path.read_bytes()).hexdigest()
        state["currentSceneSha256"] = legacy_sha
        state["currentScenePath"] = str(legacy_path.resolve())
        for name in state["stageOrder"]:
            if name != "publish":
                state["stages"][name]["status"] = "PASS"
                state["stages"][name]["sceneSha256"] = legacy_sha
                state["stages"][name]["evaluation"] = {
                    "evaluator": state["stages"][name]["evaluator"],
                    "evaluatorCodeSha256": self.loop.sha256_file(LOOP_MODULE),
                    "pipelineContractDigest": self.loop.PIPELINE_CONTRACT_DIGEST,
                    "result": "PASS",
                }
        state["capabilities"]["score-gate"] = {
            "status": "AVAILABLE",
            "reason": "contract fixture",
            "evidence": [],
        }
        self.loop.event(state, "contract-fixture", "prepare-legacy-publish", {})
        self.loop.save_state(state_path, state)
        args = type(
            "Args",
            (),
            {
                "state": state_path,
                "actor": "publish-gate",
                "scene": legacy_path,
                "review": self.review_path,
                "quality_report": self.review_path,
                "score": None,
                "output": self.root / "legacy-published",
                "note": "must fail",
            },
        )()
        with self.assertRaisesRegex(
            self.loop.WorkflowError,
            "LEGACY_SCENE_REQUIRES_MIGRATION",
        ):
            self.loop.command_publish(args)

    def test_publish_recomputes_v2_report_and_writes_v2_bundle(self) -> None:
        report = self._evaluate()
        report_path = self.root / "quality-report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        job_path = self.root / "job.json"
        state_path = self.root / "pipeline-state.json"
        job_path.write_text(
            json.dumps(
                {
                    "jobId": "v2-publish-contract",
                    "captureFingerprint": "a" * 64,
                    "state": "READY_FULL",
                    "blockedCapabilities": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        state = self.loop.initialize_workflow(job_path, state_path)
        scene_artifact_sha = hashlib.sha256(self.scene_path.read_bytes()).hexdigest()
        state["currentSceneSha256"] = scene_artifact_sha
        state["currentScenePath"] = str(self.scene_path.resolve())
        for name in state["stageOrder"]:
            if name != "publish":
                state["stages"][name]["status"] = "PASS"
                state["stages"][name]["sceneSha256"] = scene_artifact_sha
                state["stages"][name]["evaluation"] = {
                    "evaluator": state["stages"][name]["evaluator"],
                    "evaluatorCodeSha256": self.loop.sha256_file(LOOP_MODULE),
                    "pipelineContractDigest": self.loop.PIPELINE_CONTRACT_DIGEST,
                    "result": "PASS",
                }
        state["capabilities"]["score-gate"] = {
            "status": "AVAILABLE",
            "reason": "V2 deterministic evaluator",
            "evidence": [],
        }
        self.loop.event(state, "contract-fixture", "prepare-v2-publish", {})
        self.loop.save_state(state_path, state)

        args = type(
            "Args",
            (),
            {
                "state": state_path,
                "actor": "publish-gate",
                "scene": self.scene_path,
                "review": self.review_path,
                "quality_report": report_path,
                "score": None,
                "output": self.root / "published",
                "note": "V2 contract publish",
            },
        )()
        self.loop.command_publish(args)

        publish_root = self.root / "published" / report["geometryDigest"][:16]
        self.assertTrue((publish_root / "scene-authority.json").is_file())
        self.assertTrue((publish_root / "quality-report.json").is_file())
        self.assertFalse((publish_root / "scene-score.json").exists())
        manifest = json.loads(
            (publish_root / "publish-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schemaVersion"], "2.0")
        self.assertEqual(manifest["geometryDigest"], report["geometryDigest"])


if __name__ == "__main__":
    unittest.main()
