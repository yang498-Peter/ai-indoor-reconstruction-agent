from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import types
import unittest


ROOT = Path(__file__).resolve().parents[2]
LOOP_PATH = (
    ROOT
    / ".codex"
    / "skills"
    / "reconstruct-indoor-scene"
    / "scripts"
    / "reconstruction_loop.py"
)


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


loop = load("reconstruction_loop_intake_revalidation", LOOP_PATH)


class IntakeRevalidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.job = self.root / "job.json"
        self.state_path = self.root / "pipeline-state.json"
        self.job.write_text(
            json.dumps(
                {
                    "jobId": "revalidation-test",
                    "captureFingerprint": "a" * 64,
                    "state": "READY_GEOMETRY_ONLY",
                    "blockedCapabilities": ["posed-photo-association"],
                }
            ),
            encoding="utf-8",
        )
        loop.initialize_workflow(self.job, self.state_path)
        identity = {
            "schemaVersion": "1.0",
            "actorId": "author-agent",
            "runId": "11111111-1111-4111-8111-111111111111",
            "role": "author",
            "provider": "revalidation-test",
            "model": "fixture",
            "policyId": "author-v1",
            "toolPolicyHash": loop.execution_identity_api.policy_digest("author-v1"),
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "attestation": {
                "issuer": "revalidation-test",
                "enforcementMode": "application-enforced",
            },
        }
        self.identity_path = self.root / "author-identity.json"
        self.identity_path.write_text(json.dumps(identity), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def pose_report(
        self,
        status: str = "PASS",
        alignment: str = "PASS",
        fingerprint: str = "a" * 64,
    ) -> Path:
        value = {
            "schemaVersion": "1.0",
            "artifactType": "pose-validation",
            "status": status,
            "sourceSetDigest": "e" * 64,
            "checks": {
                "format": "PASS",
                "coordinateConvention": "PASS",
                "imageBindings": "PASS",
                "pointCloudAlignment": alignment,
            },
            "frames": [],
            "errors": [],
            "inputs": [
                {"artifactType": "capture-manifest", "payloadDigest": fingerprint},
                {"artifactType": "capture-index", "payloadDigest": "f" * 64},
            ],
        }
        path = self.root / "pose-validation.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def revalidate(self, report_path: Path) -> None:
        loop.command_revalidate_intake(
            types.SimpleNamespace(
                state=self.state_path,
                actor="author-agent",
                execution=self.identity_path,
                pose_validation=report_path,
            )
        )

    def test_pass_artifact_unblocks_job_and_rebinds_state(self) -> None:
        report_path = self.pose_report()
        report_sha = loop.sha256_file(report_path)
        self.revalidate(report_path)
        job = json.loads(self.job.read_text(encoding="utf-8"))
        self.assertEqual(job["state"], "READY_FULL")
        self.assertEqual(job["blockedCapabilities"], [])
        self.assertEqual(
            job["intakeRevalidations"][0]["poseValidationSha256"], report_sha
        )
        # read_state re-verifies the job hash binding, so a torn upgrade would fail here.
        state = loop.read_state(self.state_path)
        self.assertEqual(state["jobSha256"], loop.sha256_file(self.job))
        capability = state["capabilities"]["posed-photo-association"]
        self.assertEqual(capability["status"], "AVAILABLE")
        self.assertEqual(capability["upgrade"]["artifactSha256"], report_sha)
        self.assertEqual(
            state["stages"]["intake"]["artifacts"][0]["sha256"],
            loop.sha256_file(self.job),
        )
        events = [
            item for item in state["events"] if item["action"] == "revalidate-intake"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["details"]["poseValidationSha256"], report_sha)
        # Downstream stages must not become stale after the upgrade.
        loop.require_prerequisites(state, "evidence")

    def test_alignment_required_artifact_is_rejected(self) -> None:
        report_path = self.pose_report(status="ALIGNMENT_REQUIRED", alignment="NOT_RUN")
        with self.assertRaises(loop.WorkflowError) as caught:
            self.revalidate(report_path)
        self.assertEqual(caught.exception.code, "INTAKE_REVALIDATION_ARTIFACT_INVALID")
        job = json.loads(self.job.read_text(encoding="utf-8"))
        self.assertEqual(job["blockedCapabilities"], ["posed-photo-association"])
        self.assertEqual(job["state"], "READY_GEOMETRY_ONLY")

    def test_artifact_bound_to_another_capture_is_rejected(self) -> None:
        report_path = self.pose_report(fingerprint="b" * 64)
        with self.assertRaises(loop.WorkflowError) as caught:
            self.revalidate(report_path)
        self.assertEqual(caught.exception.code, "INTAKE_REVALIDATION_ARTIFACT_UNBOUND")

    def test_revalidation_without_blocked_capability_is_rejected(self) -> None:
        report_path = self.pose_report()
        self.revalidate(report_path)
        with self.assertRaises(loop.WorkflowError) as caught:
            self.revalidate(report_path)
        self.assertEqual(caught.exception.code, "INTAKE_REVALIDATION_NOT_REQUIRED")


if __name__ == "__main__":
    unittest.main()
