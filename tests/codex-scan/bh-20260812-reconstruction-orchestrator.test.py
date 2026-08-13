from __future__ import annotations

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
SPEC = importlib.util.spec_from_file_location("reconstruction_loop", SCRIPT)
assert SPEC and SPEC.loader
loop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loop)


class ReconstructionOrchestratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.job = self.root / "job.json"
        self.state_path = self.root / "pipeline-state.json"
        self.scene = self.root / "scene.json"
        self.raw = self.root / "raw.png"
        self.render = self.root / "render.png"
        self.tool = self.root / "tool.py"
        self.job.write_text(
            json.dumps(
                {
                    "jobId": "indoor-test",
                    "captureFingerprint": "a" * 64,
                    "state": "READY_FULL",
                    "blockedCapabilities": [],
                }
            ),
            encoding="utf-8",
        )
        self.scene.write_text('{"structures": [{"id": "Wall01"}]}\n', encoding="utf-8")
        self.raw.write_bytes(b"raw-evidence")
        self.render.write_bytes(b"render-evidence")
        self.tool.write_text("raise SystemExit(0)\n", encoding="utf-8")
        loop.initialize_workflow(self.job, self.state_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def args(self, **kwargs):
        return type("Args", (), kwargs)()

    def bind(self, *names: str) -> None:
        for name in names:
            receipt = self.root / f"{name}-probe.json"
            receipt.write_text(
                json.dumps(
                    {
                        "capability": name,
                        "status": "PASS",
                        "checkedBy": "probe-reviewer",
                        "checkedAt": datetime.now(timezone.utc).isoformat(),
                        "probeCommand": ["python", str(self.tool)],
                        "evidenceSha256s": [loop.sha256_file(self.tool)],
                    }
                ),
                encoding="utf-8",
            )
            loop.command_capability(
                self.args(
                    state=self.state_path,
                    actor="root",
                    name=name,
                    status="AVAILABLE",
                    reason="synthetic deterministic capability",
                    evidence=[str(self.tool)],
                    receipt=receipt,
                )
            )

    def pass_stage(self, name: str, scene: bool = False, actor: str = "root") -> None:
        loop.command_stage(
            self.args(
                state=self.state_path,
                actor=actor,
                name=name,
                status="PASS",
                artifact=[str(self.raw)],
                scene=self.scene if scene or name == "seed" else None,
                note="synthetic pass",
            )
        )

    def open_patch(self, severity: str = "P1") -> str:
        loop.command_open_issue(
            self.args(
                state=self.state_path,
                actor="author-west",
                area="west-wing",
                severity=severity,
                kind="missing-wall",
                target=["Wall01"],
                summary="wall needs correction",
                evidence=[f"raw={self.raw}"],
            )
        )
        state = loop.read_state(self.state_path)
        issue_id = state["issues"][-1]["id"]
        loop.command_patch(
            self.args(
                state=self.state_path,
                actor="author-west",
                issue=issue_id,
                scene=self.scene,
                checkpoint_dir=None,
                note="correct wall",
                strategy_change=None,
            )
        )
        return issue_id

    def review(self, issue_id: str, actor: str, verdict: str, score: int) -> None:
        loop.command_review(
            self.args(
                state=self.state_path,
                actor=actor,
                issue=issue_id,
                scene=self.scene,
                verdict=verdict,
                score=score,
                evidence=[f"render={self.render}", f"raw={self.raw}"],
                note="visual comparison",
            )
        )

    def test_capability_and_prerequisite_gates_fail_closed(self) -> None:
        with self.assertRaises(loop.WorkflowError):
            self.pass_stage("evidence")
        self.bind("point-cloud-sections")
        self.pass_stage("evidence")
        with self.assertRaises(loop.WorkflowError):
            self.pass_stage("author", scene=True)

    def test_capability_rejects_unrelated_noop_probe(self) -> None:
        receipt = self.root / "bad-probe.json"
        receipt.write_text(
            json.dumps(
                {
                    "capability": "point-cloud-sections",
                    "status": "PASS",
                    "checkedBy": "probe-reviewer",
                    "checkedAt": datetime.now(timezone.utc).isoformat(),
                    "probeCommand": ["python", "-c", "raise SystemExit(0)", str(self.tool)],
                    "evidenceSha256s": [loop.sha256_file(self.tool)],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(loop.WorkflowError):
            loop.command_capability(
                self.args(
                    state=self.state_path,
                    actor="root-agent",
                    name="point-cloud-sections",
                    status="AVAILABLE",
                    reason="unrelated no-op",
                    evidence=[str(self.tool)],
                    receipt=receipt,
                )
            )

    def test_state_rejects_modified_job_and_actor_alias(self) -> None:
        state_before = loop.sha256_file(self.state_path)
        self.job.write_text(
            json.dumps(
                {
                    "jobId": "indoor-test",
                    "captureFingerprint": "a" * 64,
                    "state": "READY_GEOMETRY_ONLY",
                    "blockedCapabilities": ["whole-scene-acceptance"],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(loop.WorkflowError):
            loop.initialize_workflow(self.job, self.state_path)
        self.assertEqual(state_before, loop.sha256_file(self.state_path))

    def test_restore_cannot_overwrite_an_unrelated_file(self) -> None:
        issue_id = self.open_patch()
        self.review(issue_id, "reviewer-east", "PASS", 90)
        unrelated = self.root.parent / "unrelated.json"
        unrelated.write_text('{"keep": true}\n', encoding="utf-8")
        original = unrelated.read_bytes()
        with self.assertRaises(loop.WorkflowError):
            loop.command_restore(
                self.args(state=self.state_path, actor="root", scene=unrelated)
            )
        self.assertEqual(original, unrelated.read_bytes())

    def test_checkpoint_directory_must_remain_inside_work(self) -> None:
        loop.command_open_issue(
            self.args(
                state=self.state_path,
                actor="author-west",
                area="west-wing",
                severity="P1",
                kind="missing-wall",
                target=["Wall01"],
                summary="wall needs correction",
                evidence=[f"raw={self.raw}"],
            )
        )
        with self.assertRaises(loop.WorkflowError):
            loop.command_patch(
                self.args(
                    state=self.state_path,
                    actor="author-west",
                    issue="I0001",
                    scene=self.scene,
                    checkpoint_dir=self.root.parent,
                    note="bad destination",
                    strategy_change=None,
                )
            )

    def test_p1_author_cannot_accept_own_patch_and_checkpoint_is_immutable(self) -> None:
        issue_id = self.open_patch()
        state = loop.read_state(self.state_path)
        checkpoint = Path(state["lastCheckpoint"]["path"])
        self.assertTrue(checkpoint.is_file())
        self.assertEqual(loop.sha256_file(checkpoint), state["currentSceneSha256"])
        with self.assertRaises(loop.WorkflowError):
            self.review(issue_id, "author-west", "PASS", 90)
        with self.assertRaises(loop.WorkflowError):
            self.review(issue_id, " AUTHOR-WEST ", "PASS", 90)
        with self.assertRaises(loop.WorkflowError):
            self.review(issue_id, "author-west\u200b", "PASS", 90)
        with self.assertRaises(loop.WorkflowError):
            self.review(issue_id, "author-west\uFE0F", "PASS", 90)
        with self.assertRaises(loop.WorkflowError):
            self.review(issue_id, "author-west\u034F", "PASS", 90)
        self.review(issue_id, "reviewer-east", "PASS", 90)
        self.assertEqual(loop.issue_by_id(loop.read_state(self.state_path), issue_id)["status"], "RESOLVED")

    def test_review_requires_render_and_distinct_source_evidence(self) -> None:
        issue_id = self.open_patch()
        with self.assertRaises(loop.WorkflowError):
            loop.command_review(
                self.args(
                    state=self.state_path,
                    actor="reviewer-east",
                    issue=issue_id,
                    scene=self.scene,
                    verdict="PASS",
                    score=90,
                    evidence=[f"raw={self.raw}"],
                    note="missing render",
                )
            )

    def test_two_non_improving_failures_force_strategy_change(self) -> None:
        issue_id = self.open_patch()
        self.review(issue_id, "reviewer-east", "FAIL", 70)
        self.scene.write_text('{"structures": [{"id": "Wall01", "x": 1}]}\n', encoding="utf-8")
        loop.command_patch(
            self.args(
                state=self.state_path,
                actor="author-west",
                issue=issue_id,
                scene=self.scene,
                checkpoint_dir=None,
                note="second correction",
                strategy_change=None,
            )
        )
        self.review(issue_id, "reviewer-east", "FAIL", 70)
        self.scene.write_text('{"structures": [{"id": "Wall01", "x": 2}]}\n', encoding="utf-8")
        loop.command_patch(
            self.args(
                state=self.state_path,
                actor="author-west",
                issue=issue_id,
                scene=self.scene,
                checkpoint_dir=None,
                note="third correction",
                strategy_change=None,
            )
        )
        self.review(issue_id, "reviewer-east", "FAIL", 69)
        self.scene.write_text('{"structures": [{"id": "Wall01", "x": 3}]}\n', encoding="utf-8")
        with self.assertRaises(loop.WorkflowError):
            loop.command_patch(
                self.args(
                    state=self.state_path,
                    actor="author-west",
                    issue=issue_id,
                    scene=self.scene,
                    checkpoint_dir=None,
                    note="same strategy",
                    strategy_change=None,
                )
            )
        loop.command_patch(
            self.args(
                state=self.state_path,
                actor="author-west",
                issue=issue_id,
                scene=self.scene,
                checkpoint_dir=None,
                note="switch to perpendicular elevation",
                strategy_change="replace plan-only fitting with perpendicular elevation and endpoint intersections",
            )
        )

    def test_scene_change_invalidates_later_reviews(self) -> None:
        self.bind(
            "point-cloud-sections",
            "semantic-scene-compiler",
            "semantic-edit",
            "deterministic-render",
            "visual-inspection",
            "topology-check",
            "overlap-check",
        )
        self.pass_stage("evidence")
        self.pass_stage("seed")
        issue_id = self.open_patch()
        self.review(issue_id, "reviewer-east", "PASS", 90)
        self.pass_stage("author", scene=True)
        self.pass_stage("regional-review", scene=True, actor="regional-reviewer")
        self.pass_stage("global-review", scene=True, actor="global-red-team")
        loop.command_open_issue(
            self.args(
                state=self.state_path,
                actor="red-team",
                area="global",
                severity="P1",
                kind="omission",
                target=[],
                summary="new omission",
                evidence=[f"raw={self.raw}"],
            )
        )
        state = loop.read_state(self.state_path)
        self.assertEqual(state["stages"]["author"]["status"], "REVIEW")
        self.assertEqual(state["stages"]["regional-review"]["status"], "PENDING")
        self.assertEqual(state["stages"]["global-review"]["status"], "PENDING")

    def test_publish_requires_current_passed_artifacts_and_is_immutable(self) -> None:
        self.bind(
            "point-cloud-sections",
            "semantic-scene-compiler",
            "semantic-edit",
            "deterministic-render",
            "visual-inspection",
            "topology-check",
            "overlap-check",
            "score-gate",
        )
        self.pass_stage("evidence")
        self.pass_stage("seed")
        issue_id = self.open_patch()
        self.review(issue_id, "reviewer-east", "PASS", 90)
        self.pass_stage("author", scene=True)
        self.pass_stage("regional-review", scene=True, actor="regional-reviewer")
        self.pass_stage("global-review", scene=True, actor="global-red-team")
        scene_sha = loop.sha256_file(self.scene)
        receipt = self.root / "review.json"
        score = self.root / "score.json"
        receipt.write_text(json.dumps({"sceneSha256": scene_sha}), encoding="utf-8")
        score.write_text(json.dumps({"status": "PASS", "sceneSha256": scene_sha}), encoding="utf-8")
        publish_args = self.args(
            state=self.state_path,
            actor="root-gatekeeper",
            scene=self.scene,
            review=receipt,
            score=score,
            output=self.root / "published",
            note="synthetic immutable publish",
        )
        with self.assertRaises(loop.WorkflowError):
            loop.command_publish(publish_args)


if __name__ == "__main__":
    unittest.main()
