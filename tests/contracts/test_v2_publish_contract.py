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
identity_api = api.execution_identity_api


def identity(actor, run_id, role, policy, reviewer_class=None):
    value = {
        "schemaVersion": "1.0", "actorId": actor, "runId": run_id, "role": role,
        "provider": "publish-contract", "model": "fixture", "policyId": policy,
        "toolPolicyHash": identity_api.policy_digest(policy),
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "attestation": {"issuer": "publish-contract", "enforcementMode": "application-enforced"},
    }
    if reviewer_class:
        value["reviewerClass"] = reviewer_class
    return value


AUTHOR = identity(
    "author-run", "11111111-1111-4111-8111-111111111111", "author", "author-v1",
)
REVIEWER = identity(
    "reviewer-run", "22222222-2222-4222-8222-222222222222",
    "reviewer", "reviewer-readonly-v1", "adversarial",
)
PUBLISHER = identity(
    "publish-gate", "33333333-3333-4333-8333-333333333333", "publisher", "publisher-v1",
)


class V2PublishContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.quality = load_module("quality_report_v2_contract", QUALITY_MODULE)
        self.loop = load_module("reconstruction_loop_contract", LOOP_MODULE)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.evidence_path = self.root / "evidence" / "wall-section.bin"
        self.evidence_path.parent.mkdir(parents=True)
        self.evidence_path.write_bytes(b"deterministic-wall-section")
        self.publisher_identity_path = self.root / "publisher-identity.json"
        self.publisher_identity_path.write_text(json.dumps(PUBLISHER), encoding="utf-8")
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
        scene = api.new_scene("synthetic-v2", 3.0, 0.0, "author-run", AUTHOR)
        level = scene["rootNodeIds"][0]
        walls = [
            ("wall_a", [0.0, 0.0], [4.0, 0.0]),
            ("wall_b", [4.0, 0.0], [4.0, 3.0]),
            ("wall_c", [4.0, 3.0], [0.0, 3.0]),
            ("wall_d", [0.0, 3.0], [0.0, 0.0]),
        ]
        scene["review"]["topology"]["spaces"] = [
            {"id": "space_main", "boundaryNodeIds": [wall[0] for wall in walls]}
        ]
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
                    "execution": AUTHOR,
                },
                "author-run",
            )
            api.op_attach_evidence(scene, self.root / "scene-authority.json", {
                "id": node_id, "type": "point-measurement",
                "path": "evidence/wall-section.bin", "producer": "publish-contract",
            })
            api.op_accept(scene, self.root / "scene-authority.json", {
                "id": node_id, "mode": "measured", "reviewerIdentity": REVIEWER,
            })
        api.validate_scene(scene)
        return scene

    def _review_receipt(self) -> dict:
        return {
            "schemaVersion": "2.0",
            "geometryDigest": api.geometry_digest(self.scene),
            "evidenceSetDigest": api.evidence_set_digest(self.scene),
            "artifactSha256": hashlib.sha256(self.scene_path.read_bytes()).hexdigest(),
            "reviewer": REVIEWER,
            "reviewedAt": datetime.now(timezone.utc).isoformat(),
            "p0": [],
            "p1": [],
        }

    def _evaluate(self) -> dict:
        return self.quality.evaluate_files(self.scene_path, self.review_path)

    def _bind_stage_identities(self, state: dict) -> None:
        for execution in (AUTHOR, REVIEWER):
            state["executions"][execution["runId"]] = {
                "identity": execution,
                "identityDigest": identity_api.identity_digest(execution),
            }
        state["stages"]["author"]["executionRunId"] = AUTHOR["runId"]
        state["stages"]["author"]["identityDigest"] = identity_api.identity_digest(AUTHOR)
        for name in ("presentation-review", "regional-review", "global-review"):
            state["stages"][name]["executionRunId"] = REVIEWER["runId"]
            state["stages"][name]["identityDigest"] = identity_api.identity_digest(REVIEWER)

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
        self.assertEqual(set(schema["properties"]["checks"]["required"]), set(report["checks"]))
        self.assertEqual(report["checks"]["sharedRootCount"], 0)

    def test_shared_root_derivations_pass_and_are_reported_as_info(self) -> None:
        # Pure geometry capture: every derived artifact descends from one LAS.
        # Shared roots must not FAIL; they surface as sharedRootCount info.
        root_digest = "d" * 64
        level = self.scene["rootNodeIds"][0]
        api.op_create_wall(self.scene, {
            "id": "wall_inferred", "level": level, "start": [1.0, 1.0], "end": [3.0, 1.0],
            "height": 3.0, "thickness": 0.12, "execution": AUTHOR,
        }, "author-run")
        for name, content, params in (
            ("band-low.bin", b"low band bytes", {"band": [0.3, 0.9]}),
            ("band-high.bin", b"high band bytes", {"band": [1.8, 2.4]}),
        ):
            (self.root / name).write_bytes(content)
            receipt_name = f"{name}.provenance.json"
            (self.root / receipt_name).write_text(json.dumps({
                "outputContentSha256": hashlib.sha256(content).hexdigest(),
                "rootContentSha256s": [root_digest],
                "producer": "pointcloud-evidence",
                "generatorParameters": params,
            }), encoding="utf-8")
            api.op_attach_evidence(self.scene, self.scene_path, {
                "id": "wall_inferred", "type": "derived-band", "path": name,
                "provenanceReceipt": receipt_name,
            })
        api.op_accept(self.scene, self.scene_path, {
            "id": "wall_inferred", "mode": "inferred", "reviewerIdentity": REVIEWER,
            "reason": "two height-band derivations of the single capture root",
        })
        self.scene_path.write_text(
            json.dumps(self.scene, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.review_path.write_text(
            json.dumps(self._review_receipt(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report = self._evaluate()
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["checks"]["evidenceLineagesDistinct"])
        self.assertEqual(report["checks"]["sharedRootCount"], 1)

    def test_stale_claim_snapshot_is_an_explicit_quality_failure(self) -> None:
        scene = json.loads(self.scene_path.read_text(encoding="utf-8"))
        scene["nodes"]["wall_a"]["end"] = [4.5, 0.0]
        self.scene_path.write_text(
            json.dumps(scene, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report = self._evaluate()
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["checks"]["claimsCurrent"])
        self.assertIn("ACCEPTED_CLAIMS_STALE", report["errors"])

    def test_presentation_layer_cannot_pass_authority_quality_gate(self) -> None:
        scene = json.loads(self.scene_path.read_text(encoding="utf-8"))
        scene["sceneLayer"] = "presentation"
        self.scene_path.write_text(
            json.dumps(scene, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report = self._evaluate()
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["checks"]["authorityLayerValid"])
        self.assertIn("SCENE_LAYER_NOT_AUTHORITY", report["errors"])

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
        self._bind_stage_identities(state)
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
                "execution": self.publisher_identity_path,
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
        self._bind_stage_identities(state)
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
                "execution": self.publisher_identity_path,
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
