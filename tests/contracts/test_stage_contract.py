from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".codex"
    / "skills"
    / "reconstruct-indoor-scene"
    / "scripts"
    / "reconstruction_loop.py"
)
SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas"
SPEC = importlib.util.spec_from_file_location("reconstruction_loop_stage_contract", SCRIPT)
assert SPEC and SPEC.loader
loop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loop)


EXPECTED_STAGES = [
    "intake",
    "evidence",
    "macro-hypothesis",
    "seed",
    "author",
    "presentation-review",
    "regional-review",
    "global-review",
    "publish",
]


def canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PipelineStageContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.job = self.root / "job.json"
        self.state_path = self.root / "pipeline-state.json"
        self.scene = self.root / "scene-authority.json"
        self.job.write_text(
            json.dumps(
                {
                    "jobId": "contract-test",
                    "captureFingerprint": "a" * 64,
                    "state": "READY_FULL",
                    "blockedCapabilities": [],
                }
            ),
            encoding="utf-8",
        )
        self.scene.write_text(
            json.dumps(
                {
                    "schemaVersion": "2.0",
                    "job": {"id": "contract-test"},
                    "nodes": [],
                    "evidence": [],
                    "review": {"issues": []},
                }
            ),
            encoding="utf-8",
        )
        loop.initialize_workflow(self.job, self.state_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def args(**kwargs):
        return type("Args", (), kwargs)()

    def artifact(
        self,
        artifact_type: str,
        scene_sha: str | None = None,
        failed_check: str | None = None,
    ) -> Path:
        required_checks = next(
            (
                stage["requiredArtifactChecks"][artifact_type]
                for stage in loop.PIPELINE_CONTRACT["stages"]
                if artifact_type in stage["requiredArtifactChecks"]
            ),
            [],
        )
        payload = {
            "status": "PASS",
            "checks": {
                name: name != failed_check
                for name in required_checks
            },
            "fixture": artifact_type,
        }
        value = {
            "schemaVersion": "1.0",
            "artifactType": artifact_type,
            "jobId": "contract-test",
            "captureFingerprint": "a" * 64,
            "payloadDigest": canonical_digest(payload),
            "producer": {
                "name": "stage-contract-test",
                "version": "1.0",
                "gitSha": "b" * 40,
                "command": ["fixture"],
                "configDigest": "c" * 64,
                "environmentDigest": "d" * 64,
                "randomSeed": 0,
            },
            "inputs": [],
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        if scene_sha:
            value["inputs"].append(
                {
                    "artifactType": "scene-authority",
                    "artifactSha256": scene_sha,
                }
            )
        path = self.root / f"{artifact_type}.json"
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return path

    def test_machine_contract_is_the_only_nine_stage_source(self) -> None:
        self.assertEqual(list(loop.STAGE_ORDER), EXPECTED_STAGES)
        self.assertEqual(
            [stage["name"] for stage in loop.PIPELINE_CONTRACT["stages"]],
            EXPECTED_STAGES,
        )
        state = loop.read_state(self.state_path)
        self.assertEqual(state["schemaVersion"], 2)
        self.assertEqual(state["stageOrder"], EXPECTED_STAGES)
        self.assertEqual(state["pipelineContractDigest"], loop.PIPELINE_CONTRACT_DIGEST)
        for name in (
            "pipeline-contract-v2.schema.json",
            "pipeline-artifact-v1.schema.json",
            "pipeline-state-v2.schema.json",
        ):
            document = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
            self.assertEqual(document["$schema"], "https://json-schema.org/draft/2020-12/schema")
        for stage in loop.PIPELINE_CONTRACT["stages"]:
            self.assertEqual(
                set(stage["requiredArtifactChecks"]),
                set(stage["requiredArtifacts"]),
            )

    def test_generic_stage_command_cannot_write_pass(self) -> None:
        with self.assertRaises(loop.WorkflowError) as caught:
            loop.command_stage(
                self.args(
                    state=self.state_path,
                    actor="author-agent",
                    name="evidence",
                    status="PASS",
                    artifact=[],
                    scene=None,
                    note="must be rejected",
                )
            )
        self.assertEqual(caught.exception.code, "GENERIC_STAGE_PASS_FORBIDDEN")
        self.assertIn("evaluate-stage", caught.exception.next_actions[0])

    def test_dedicated_evaluator_requires_current_typed_artifact(self) -> None:
        with self.assertRaises(loop.WorkflowError) as caught:
            loop.command_evaluate_stage(
                self.args(
                    state=self.state_path,
                    actor="evidence-agent",
                    name="evidence",
                    artifact=[],
                    scene=None,
                    note="missing evidence bundle",
                )
            )
        self.assertEqual(caught.exception.code, "STAGE_ARTIFACT_MISSING")

        wrong = self.artifact("macro-hypothesis")
        with self.assertRaises(loop.WorkflowError) as wrong_caught:
            loop.command_evaluate_stage(
                self.args(
                    state=self.state_path,
                    actor="evidence-agent",
                    name="evidence",
                    artifact=[f"macro-hypothesis={wrong}"],
                    scene=None,
                    note="wrong artifact type",
                )
            )
        self.assertEqual(wrong_caught.exception.code, "STAGE_ARTIFACT_MISSING")

    def test_dedicated_evaluator_rejects_failed_artifact_check(self) -> None:
        state = loop.read_state(self.state_path)
        state["capabilities"]["point-cloud-sections"]["status"] = "AVAILABLE"
        loop.event(state, "contract-fixture", "enable-capability", {})
        loop.save_state(self.state_path, state)
        failed = self.artifact("evidence-bundle", failed_check="indexChecksumsValid")
        with self.assertRaises(loop.WorkflowError) as caught:
            loop.command_evaluate_stage(
                self.args(
                    state=self.state_path,
                    actor="evidence-agent",
                    name="evidence",
                    artifact=[f"evidence-bundle={failed}"],
                    scene=None,
                    note="must fail",
                )
            )
        self.assertEqual(caught.exception.code, "STAGE_ARTIFACT_CHECK_FAILED")

    def test_authority_and_presentation_changes_follow_invalidation_dag(self) -> None:
        state = loop.read_state(self.state_path)
        for name in EXPECTED_STAGES:
            state["stages"][name]["status"] = "PASS"

        loop.apply_change_invalidation(state, "authority", "authority changed")
        self.assertEqual(state["stages"]["evidence"]["status"], "PASS")
        self.assertEqual(state["stages"]["macro-hypothesis"]["status"], "PASS")
        self.assertEqual(state["stages"]["author"]["status"], "REVIEW")
        for name in (
            "presentation-review",
            "regional-review",
            "global-review",
            "publish",
        ):
            self.assertEqual(state["stages"][name]["status"], "PENDING")

        for name in EXPECTED_STAGES:
            state["stages"][name]["status"] = "PASS"
        loop.apply_change_invalidation(state, "presentation", "presentation changed")
        for name in ("evidence", "macro-hypothesis", "seed", "author"):
            self.assertEqual(state["stages"][name]["status"], "PASS")
        for name in (
            "presentation-review",
            "regional-review",
            "global-review",
            "publish",
        ):
            self.assertEqual(state["stages"][name]["status"], "PENDING")

    def test_state_compare_and_swap_rejects_stale_writer(self) -> None:
        first = loop.read_state(self.state_path)
        stale = loop.read_state(self.state_path)
        loop.event(first, "writer-one", "fixture", {})
        loop.save_state(self.state_path, first)
        loop.event(stale, "writer-two", "fixture", {})
        with self.assertRaises(loop.WorkflowError) as caught:
            loop.save_state(self.state_path, stale)
        self.assertEqual(caught.exception.code, "STATE_REVISION_CONFLICT")

    def test_evaluator_code_change_makes_prerequisite_stale(self) -> None:
        state = loop.read_state(self.state_path)
        state["stages"]["intake"]["evaluation"]["evaluatorCodeSha256"] = "0" * 64
        state["capabilities"]["point-cloud-sections"]["status"] = "AVAILABLE"
        loop.event(state, "contract-fixture", "tamper-evaluator-binding", {})
        loop.save_state(self.state_path, state)
        evidence = self.artifact("evidence-bundle")
        with self.assertRaises(loop.WorkflowError) as caught:
            loop.command_evaluate_stage(
                self.args(
                    state=self.state_path,
                    actor="evidence-agent",
                    name="evidence",
                    artifact=[f"evidence-bundle={evidence}"],
                    scene=None,
                    note="stale prerequisite must fail",
                )
            )
        self.assertEqual(
            caught.exception.code,
            "STAGE_PREREQUISITE_INCOMPLETE_OR_STALE",
        )

    def test_v1_state_requires_explicit_fail_closed_migration(self) -> None:
        legacy = json.loads(self.state_path.read_text(encoding="utf-8"))
        legacy["schemaVersion"] = 1
        legacy.pop("pipelineContractDigest", None)
        legacy["stageOrder"] = [
            "intake",
            "evidence",
            "seed",
            "author",
            "regional-review",
            "global-review",
            "publish",
        ]
        legacy["stages"].pop("macro-hypothesis")
        legacy["stages"].pop("presentation-review")
        self.state_path.write_text(
            json.dumps(legacy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        with self.assertRaises(loop.WorkflowError) as caught:
            loop.read_state(self.state_path)
        self.assertEqual(caught.exception.code, "PIPELINE_STATE_MIGRATION_REQUIRED")

        loop.command_migrate_state(
            self.args(state=self.state_path, actor="migration-agent")
        )
        migrated = loop.read_state(self.state_path)
        self.assertEqual(migrated["schemaVersion"], 2)
        self.assertEqual(migrated["stageOrder"], EXPECTED_STAGES)
        self.assertEqual(migrated["stages"]["evidence"]["status"], "PENDING")
        self.assertEqual(migrated["stages"]["macro-hypothesis"]["status"], "PENDING")
        self.assertEqual(migrated["stages"]["seed"]["status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
