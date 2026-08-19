from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import types
import unittest


ROOT = Path(__file__).resolve().parents[2]
LOOP_PATH = ROOT / ".codex" / "skills" / "reconstruct-indoor-scene" / "scripts" / "reconstruction_loop.py"
IDENTITY_PATH = ROOT / "scene-core" / "execution_identity.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


identity_api = load("execution_identity_pipeline_contract", IDENTITY_PATH)
loop = load("reconstruction_loop_pipeline_contract", LOOP_PATH)


def make_identity(actor, run_id, role, policy, reviewer_class=None):
    value = {
        "schemaVersion": "1.0", "actorId": actor, "runId": run_id, "role": role,
        "provider": "contract-test", "model": "fixture", "policyId": policy,
        "toolPolicyHash": identity_api.policy_digest(policy),
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "attestation": {"issuer": "contract-test", "enforcementMode": "application-enforced"},
    }
    if reviewer_class:
        value["reviewerClass"] = reviewer_class
    return value


class PipelineExecutionIdentityContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.job = self.root / "job.json"
        self.state = self.root / "pipeline-state.json"
        self.scene = self.root / "scene-authority.json"
        self.raw = self.root / "raw.bin"
        self.render = self.root / "render.bin"
        self.job.write_text(json.dumps({
            "jobId": "identity-contract", "captureFingerprint": "a" * 64,
            "state": "READY_FULL", "blockedCapabilities": [],
        }), encoding="utf-8")
        self.scene.write_text(json.dumps({"schemaVersion": "2.0"}), encoding="utf-8")
        self.raw.write_bytes(b"raw evidence")
        self.render.write_bytes(b"render evidence")
        loop.initialize_workflow(self.job, self.state)
        self.author = self._identity_file("author.json", make_identity(
            "author-west", "11111111-1111-4111-8111-111111111111", "author", "author-v1",
        ))
        self._open_and_patch()

    def tearDown(self):
        self.temp.cleanup()

    def _identity_file(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _open_and_patch(self):
        loop.command_open_issue(types.SimpleNamespace(
            state=self.state, actor="author-west", execution=self.author,
            area="west", severity="P1", kind="missing-wall", target=[],
            summary="missing wall", evidence=[f"raw={self.raw}"],
        ))
        loop.command_patch(types.SimpleNamespace(
            state=self.state, actor="author-west", execution=self.author,
            issue="I0001", scene=self.scene, checkpoint_dir=None,
            note="patch", strategy_change=None,
        ))

    def _review(self, identity_path, actor):
        return loop.command_review(types.SimpleNamespace(
            state=self.state, actor=actor, execution=identity_path,
            issue="I0001", scene=self.scene, verdict="PASS", score=90,
            evidence=[f"render={self.render}", f"raw={self.raw}"], note="review",
        ))

    def test_same_run_cannot_be_reregistered_as_reviewer(self):
        reviewer = self._identity_file("same-run-reviewer.json", make_identity(
            "reviewer-east", "11111111-1111-4111-8111-111111111111",
            "reviewer", "reviewer-readonly-v1", "regional",
        ))
        with self.assertRaisesRegex(loop.WorkflowError, "already registered"):
            self._review(reviewer, "reviewer-east")

    def test_p1_standard_reviewer_is_rejected(self):
        reviewer = self._identity_file("standard-reviewer.json", make_identity(
            "reviewer-east", "22222222-2222-4222-8222-222222222222",
            "reviewer", "reviewer-readonly-v1", "standard",
        ))
        with self.assertRaisesRegex(loop.WorkflowError, "P0_P1_INDEPENDENT_REVIEW_REQUIRED"):
            self._review(reviewer, "reviewer-east")

    def test_distinct_regional_read_only_reviewer_passes(self):
        reviewer = self._identity_file("regional-reviewer.json", make_identity(
            "reviewer-east", "22222222-2222-4222-8222-222222222222",
            "reviewer", "reviewer-readonly-v1", "regional",
        ))
        self._review(reviewer, "reviewer-east")
        state = loop.read_state(self.state)
        issue = state["issues"][0]
        self.assertEqual(issue["status"], "RESOLVED")
        self.assertEqual(issue["resolvedByRunId"], "22222222-2222-4222-8222-222222222222")


if __name__ == "__main__":
    unittest.main()
