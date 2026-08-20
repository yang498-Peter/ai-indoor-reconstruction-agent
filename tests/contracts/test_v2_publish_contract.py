from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys
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
PUBLISH_MODULE = SCENE_CORE / "publish_bundle_v2.py"
PUBLISH_SCHEMA = ROOT / "schemas" / "publish-manifest-v2.schema.json"


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
        self.publisher = load_module("publish_bundle_v2_contract", PUBLISH_MODULE)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.evidence_path = self.root / "evidence" / "wall-section.bin"
        self.evidence_path.parent.mkdir(parents=True)
        self.evidence_path.write_bytes(b"deterministic-wall-section")
        self.render_path = self.root / "renders" / "global-review.png"
        self.render_path.parent.mkdir(parents=True)
        self.render_path.write_bytes(b"deterministic-render")
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
            "areas": [
                {"id": "main-room", "score": 92},
                {"id": "perimeter-envelope", "score": 90},
            ],
            "score": 91,
        }

    def _evaluate(self) -> dict:
        return self.quality.evaluate_files(self.scene_path, self.review_path)

    def _bundle_fixture(self, output: Path, extras: list[tuple[str, Path]] | None = None):
        report = self._evaluate()
        report_path = self.root / f"quality-{output.name}.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        state = {
            "jobId": "bundle-contract",
            "captureFingerprint": "a" * 64,
            "pipelineContractDigest": "b" * 64,
            "currentSceneSha256": hashlib.sha256(self.scene_path.read_bytes()).hexdigest(),
            "stageOrder": [],
            "stages": {},
            "capabilities": {},
            "executions": {},
            "issues": [],
        }
        return self.publisher.create_bundle(
            output,
            self.scene_path,
            self.review_path,
            report_path,
            state,
            PUBLISHER,
            extras if extras is not None else [("render", self.render_path)],
            "full",
            [],
            [],
            published_at="2026-08-21T00:00:00+00:00",
        )

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
        self.assertTrue(report["blockingChecks"])
        self.assertTrue(all(item["status"] == "PASS" for item in report["blockingChecks"]))
        self.assertEqual(
            len({item["id"] for item in report["blockingChecks"]}),
            len(report["blockingChecks"]),
        )

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

    def _write_review(self, **overrides) -> None:
        receipt = self._review_receipt()
        receipt.update(overrides)
        self.review_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_review_area_score_below_85_fails_score_gate(self) -> None:
        self._write_review(
            areas=[{"id": "main-room", "score": 84}, {"id": "west-wing", "score": 95}],
            score=93,
        )
        report = self._evaluate()
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["checks"]["reviewScoreGate"])
        self.assertEqual(report["checks"]["reviewMinAreaScore"], 84)
        self.assertTrue(
            any(error.startswith("REVIEW_SCORE_BELOW_GATE") for error in report["errors"])
        )
        score_check = next(item for item in report["blockingChecks"] if item["id"] == "review-score")
        self.assertEqual(score_check["status"], "FAIL")
        self.assertEqual(score_check["failureCode"], "REVIEW_SCORE_MISSING_OR_BELOW_GATE")

    def test_review_total_score_below_90_fails_score_gate(self) -> None:
        self._write_review(
            areas=[{"id": "main-room", "score": 88}, {"id": "west-wing", "score": 89}],
            score=89,
        )
        report = self._evaluate()
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["checks"]["reviewScoreGate"])
        self.assertTrue(
            any(error.startswith("REVIEW_SCORE_BELOW_GATE") for error in report["errors"])
        )

    def test_review_scores_at_86_and_91_pass_score_gate(self) -> None:
        self._write_review(
            areas=[{"id": "main-room", "score": 86}, {"id": "west-wing", "score": 92}],
            score=91,
        )
        report = self._evaluate()
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["checks"]["reviewScoreGate"])
        self.assertEqual(report["checks"]["reviewMinAreaScore"], 86)
        self.assertEqual(report["checks"]["reviewTotalScore"], 91)

    def test_review_without_scores_fails_closed(self) -> None:
        receipt = self._review_receipt()
        receipt.pop("areas")
        receipt.pop("score")
        self.review_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report = self._evaluate()
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("REVIEW_SCORE_MISSING", report["errors"])
        self.assertEqual(report["checks"]["reviewAreaCount"], 0)
        self.assertIsNone(report["checks"]["reviewMinAreaScore"])

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

    def test_quality_rejects_evidence_path_escape(self) -> None:
        scene = json.loads(self.scene_path.read_text(encoding="utf-8"))
        scene["evidence"]["wall_a"]["sources"][0]["path"] = "../outside.bin"
        self.scene_path.write_text(
            json.dumps(scene, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.review_path.write_text(
            json.dumps(self._review_receipt(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report = self._evaluate()
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("EVIDENCE_PATH_ESCAPE:wall_a:0", report["errors"])
        evidence_check = next(
            item for item in report["blockingChecks"] if item["id"] == "evidence-files"
        )
        self.assertEqual(evidence_check["status"], "FAIL")

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
                    "evaluatorVersion": self.loop.EVALUATOR_VERSIONS[
                        state["stages"][name]["evaluator"]
                    ],
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
                    "evaluatorVersion": self.loop.EVALUATOR_VERSIONS[
                        state["stages"][name]["evaluator"]
                    ],
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
                "bundle_file": [f"render={self.render_path}"],
                "note": "V2 contract publish",
            },
        )()
        self.loop.command_publish(args)

        published = list((self.root / "published").iterdir())
        self.assertEqual(len(published), 1)
        publish_root = published[0]
        self.assertEqual(len(publish_root.name), 64)
        self.assertTrue((publish_root / "scene-authority.json").is_file())
        self.assertTrue((publish_root / "quality-report.json").is_file())
        self.assertFalse((publish_root / "scene-score.json").exists())
        manifest = json.loads(
            (publish_root / "publish-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schemaVersion"], "2.0")
        self.assertEqual(manifest["geometryDigest"], report["geometryDigest"])
        self.assertEqual(manifest["bundleDigest"], publish_root.name)
        self.assertEqual(
            {item["role"] for item in manifest["files"]},
            {"authority", "review", "quality", "evidence", "render", "provenance"},
        )
        self.assertTrue(all(not Path(item["path"]).is_absolute() for item in manifest["files"]))
        self.assertTrue(all((publish_root / item["path"]).is_file() for item in manifest["files"]))
        for json_path in publish_root.rglob("*.json"):
            self.assertNotIn(str(self.root.resolve()), json_path.read_text(encoding="utf-8"))

        schema = json.loads(PUBLISH_SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(set(schema["required"]), set(manifest))
        self.assertEqual(self.publisher.verify_bundle(publish_root)["bundleDigest"], publish_root.name)
        published_state = json.loads(state_path.read_text(encoding="utf-8"))
        publish_evaluation = published_state["stages"]["publish"]["evaluation"]
        self.assertEqual(publish_evaluation["bundleDigest"], publish_root.name)
        self.assertEqual(publish_evaluation["postPublishRevalidation"], "PASS")

    def test_bundle_survives_relocation_and_detects_render_tampering(self) -> None:
        publish_root, _ = self._bundle_fixture(self.root / "publish-relocation")
        relocated = self.root / "relocated" / publish_root.name
        shutil.copytree(publish_root, relocated)
        self.assertEqual(self.publisher.verify_bundle(relocated)["bundleDigest"], relocated.name)
        completed = subprocess.run(
            [sys.executable, str(PUBLISH_MODULE), "--bundle", str(relocated)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertTrue(json.loads(completed.stdout)["ok"])

        manifest = json.loads((relocated / "publish-manifest.json").read_text(encoding="utf-8"))
        render = next(item for item in manifest["files"] if item["role"] == "render")
        render_path = relocated / render["path"]
        os.chmod(render_path, 0o666)
        render_path.write_bytes(b"tampered-render")
        with self.assertRaisesRegex(
            self.publisher.PublishBundleError,
            "PUBLISH_BUNDLE_REVALIDATION_FAILED",
        ):
            self.publisher.verify_bundle(relocated)

    def test_manifest_path_escape_is_rejected(self) -> None:
        publish_root, _ = self._bundle_fixture(self.root / "publish-path-escape")
        manifest_path = publish_root / "publish-manifest.json"
        os.chmod(manifest_path, 0o666)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = "../outside.bin"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(
            self.publisher.PublishBundleError,
            "PUBLISH_MANIFEST_PATH_ESCAPE",
        ):
            self.publisher.verify_bundle(publish_root)

    def test_manifest_scope_tampering_breaks_content_address(self) -> None:
        publish_root, _ = self._bundle_fixture(self.root / "publish-scope-tamper")
        manifest_path = publish_root / "publish-manifest.json"
        os.chmod(manifest_path, 0o666)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["publishScope"] = "geometry-only"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(
            self.publisher.PublishBundleError,
            "PUBLISH_BUNDLE_DIGEST_MISMATCH",
        ):
            self.publisher.verify_bundle(publish_root)

    def test_missing_render_fails_before_creating_publish_directory(self) -> None:
        output = self.root / "publish-missing-render"
        with self.assertRaisesRegex(
            self.publisher.PublishBundleError,
            "PUBLISH_RENDER_REQUIRED",
        ):
            self._bundle_fixture(output, extras=[])
        self.assertFalse(output.exists())

    def test_render_bytes_participate_in_content_address_and_existing_bundle_is_immutable(self) -> None:
        first, _ = self._bundle_fixture(self.root / "publish-address-a")
        with self.assertRaisesRegex(
            self.publisher.PublishBundleError,
            "PUBLISH_IMMUTABLE_DESTINATION_EXISTS",
        ):
            self._bundle_fixture(self.root / "publish-address-a")

        self.render_path.write_bytes(b"different-deterministic-render")
        second, _ = self._bundle_fixture(self.root / "publish-address-b")
        self.assertNotEqual(first.name, second.name)


if __name__ == "__main__":
    unittest.main()
