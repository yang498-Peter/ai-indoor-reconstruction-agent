"""P1-6/P1-7 contract tests: recomputed artifact checks and area-scoped re-review.

Self-reported booleans that the contract classifies as recomputed must be
independently recomputed by the stage evaluators; a claim the recomputation
contradicts fails with SELF_REPORTED_CHECK_MISMATCH, and missing recompute
inputs fail closed. Regional review keeps a per-area ledger so an area-scoped
patch only reopens the affected areas.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
LOOP_PATH = (
    ROOT / ".codex" / "skills" / "reconstruct-indoor-scene" / "scripts" / "reconstruction_loop.py"
)
SCENE_API_PATH = ROOT / "scene-core" / "scene_api.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


loop = load("reconstruction_loop_check_recompute", LOOP_PATH)
api = load("scene_api_check_recompute", SCENE_API_PATH)
identity_api = loop.execution_identity_api


def make_identity(actor, run_id, role, policy, reviewer_class=None):
    value = {
        "schemaVersion": "1.0", "actorId": actor, "runId": run_id, "role": role,
        "provider": "check-recompute-test", "model": "fixture", "policyId": policy,
        "toolPolicyHash": identity_api.policy_digest(policy),
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "attestation": {
            "issuer": "check-recompute-test",
            "enforcementMode": "application-enforced",
        },
    }
    if reviewer_class:
        value["reviewerClass"] = reviewer_class
    return value


AUTHOR = make_identity(
    "author-west", "11111111-1111-4111-8111-111111111111", "author", "author-v1",
)
REVIEWER_STUB = {
    "actorId": "reviewer-east",
    "runId": "22222222-2222-4222-8222-222222222222",
}

RECT_WALLS = [
    ("wall_a", [0.0, 0.0], [4.0, 0.0]),
    ("wall_b", [4.0, 0.0], [4.0, 3.0]),
    ("wall_c", [4.0, 3.0], [0.0, 3.0]),
    ("wall_d", [0.0, 3.0], [0.0, 0.0]),
]

ELIGIBLE_CANDIDATE = {
    "id": "cand-1",
    "lengthM": 3.0,
    "confidence": 0.8,
    "fitResidualP90M": 0.03,
    "supportPointCount": 5000,
    "wallMode": "single-face",
    "suggestedCenterline": {"start": [0.0, 10.0], "end": [3.0, 10.0]},
}


class CheckRecomputeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.job = self.root / "job.json"
        self.state_path = self.root / "pipeline-state.json"
        self.scene_path = self.root / "scene-authority.json"
        self.proposals = self.root / "proposals.json"
        self.raw = self.root / "raw.png"
        self.render = self.root / "render.png"
        self.job.write_text(
            json.dumps({
                "jobId": "check-recompute",
                "captureFingerprint": "a" * 64,
                "state": "READY_FULL",
                "blockedCapabilities": [],
            }),
            encoding="utf-8",
        )
        self.proposals.write_text(
            json.dumps({"wallCandidates": []}) + "\n", encoding="utf-8"
        )
        self.raw.write_bytes(b"raw-evidence")
        self.render.write_bytes(b"render-evidence")
        loop.initialize_workflow(self.job, self.state_path)
        state = loop.read_state(self.state_path)
        for name in (
            "point-cloud-sections", "semantic-scene-compiler", "semantic-edit",
            "deterministic-render", "visual-inspection", "topology-check",
            "overlap-check",
        ):
            state["capabilities"][name] = {
                "status": "AVAILABLE",
                "reason": "check-recompute fixture",
                "evidence": [],
            }
        loop.event(state, "check-recompute-fixture", "enable-capabilities", {})
        loop.save_state(self.state_path, state)
        self.executions = {
            "author-west": self.write_identity(AUTHOR),
            "reviewer-east": self.write_identity(make_identity(
                "reviewer-east", REVIEWER_STUB["runId"],
                "reviewer", "reviewer-readonly-v1", "regional",
            )),
            "presentation-reviewer": self.write_identity(make_identity(
                "presentation-reviewer", "33333333-3333-4333-8333-333333333333",
                "reviewer", "reviewer-readonly-v1", "standard",
            )),
            "regional-reviewer": self.write_identity(make_identity(
                "regional-reviewer", "44444444-4444-4444-8444-444444444444",
                "reviewer", "reviewer-readonly-v1", "regional",
            )),
            "global-red-team": self.write_identity(make_identity(
                "global-red-team", "55555555-5555-4555-8555-555555555555",
                "reviewer", "reviewer-readonly-v1", "adversarial",
            )),
        }
        self.write_scene(self.build_scene())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def args(self, **kwargs):
        return type("Args", (), kwargs)()

    def write_identity(self, value: dict) -> Path:
        path = self.root / f"execution-{value['actorId']}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def build_scene(
        self,
        *,
        crossing: bool = False,
        candidate_boundary: bool = False,
        measured: bool = False,
    ) -> dict:
        scene = api.new_scene("check-recompute", 3.0, 0.0, "author-west", AUTHOR)
        level = scene["rootNodeIds"][0]
        for node_id, start, end in RECT_WALLS:
            api.op_create_wall(scene, {
                "id": node_id, "level": level, "start": start, "end": end,
                "height": 3.0, "thickness": 0.12, "execution": AUTHOR,
            }, "author-west")
        if crossing:
            # Crosses wall_a at (2, 0) and wall_c at (2, 3) through both
            # interiors: an X-collision the compile recompute must catch.
            api.op_create_wall(scene, {
                "id": "wall_x", "level": level,
                "start": [2.0, -1.0], "end": [2.0, 4.0],
                "height": 3.0, "thickness": 0.12, "execution": AUTHOR,
            }, "author-west")
        scene["review"]["topology"]["spaces"] = [
            {"id": "space_main", "boundaryNodeIds": [wall[0] for wall in RECT_WALLS]}
        ]
        accepted = [wall[0] for wall in RECT_WALLS] + (["wall_x"] if crossing else [])
        if candidate_boundary:
            # wall_d stays candidate while the declared space still names it.
            accepted.remove("wall_d")
        for node_id in accepted:
            status = (
                "accepted-measured" if measured and node_id == "wall_a"
                else "accepted-inferred"
            )
            scene["evidence"][node_id] = {
                "status": status,
                "reason": "fixture acceptance",
                "sources": [{"type": "overview"}],
                "claimHash": "a" * 64,
                "acceptedSourceDigest": "b" * 64,
                "reviewer": dict(REVIEWER_STUB),
                "claimSnapshot": api.claim_payload(scene, node_id),
            }
        return scene

    def write_scene(self, scene: dict) -> str:
        self.scene_path.write_text(
            json.dumps(scene, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return loop.sha256_file(self.scene_path)

    def default_payload_extra(self) -> dict[str, dict]:
        scene_sha = loop.sha256_file(self.scene_path)
        areas = [{"id": "west-wing", "score": 92}, {"id": "east-wing", "score": 90}]
        omission = {
            "recompute": {"proposalsPath": str(self.proposals)},
            "dispositions": {},
        }
        return {
            "presentation-review-receipt": {"sceneSha256": scene_sha},
            "regional-review-receipt": {"areas": areas},
            "omission-audit": dict(omission),
            "global-review-receipt": {"areas": areas, "score": 92, **omission},
        }

    def stage_actor(self, name: str) -> str:
        return {
            "presentation-review": "presentation-reviewer",
            "regional-review": "regional-reviewer",
            "global-review": "global-red-team",
        }.get(name, "author-west")

    def pass_stage(self, name: str, payload_extra: dict | None = None) -> None:
        spec = loop.STAGE_SPECS[name]
        scene_path = self.scene_path if spec["sceneBinding"] == "authority" else None
        scene_sha = loop.sha256_file(self.scene_path) if scene_path else None
        extras = self.default_payload_extra()
        extras.update(payload_extra or {})
        artifacts = []
        for artifact_type in spec["requiredArtifacts"]:
            if artifact_type == "scene-authority":
                continue
            payload = {
                "status": "PASS",
                "checks": {
                    check: True
                    for check in spec["requiredArtifactChecks"][artifact_type]
                },
                "fixture": artifact_type,
            }
            payload.update(extras.get(artifact_type, {}))
            payload_digest = hashlib.sha256(
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            value = {
                "schemaVersion": "1.0",
                "artifactType": artifact_type,
                "jobId": "check-recompute",
                "captureFingerprint": "a" * 64,
                "payloadDigest": payload_digest,
                "producer": {
                    "name": "check-recompute-test",
                    "version": "1.0",
                    "gitSha": "b" * 40,
                    "command": ["fixture"],
                    "configDigest": "c" * 64,
                    "environmentDigest": "d" * 64,
                    "randomSeed": 0,
                },
                "inputs": loop.required_upstream_input_bindings(
                    loop.read_state(self.state_path), name
                ),
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            }
            if scene_sha:
                scene_input = {
                    "artifactType": "scene-authority",
                    "artifactSha256": scene_sha,
                }
                if scene_input not in value["inputs"]:
                    value["inputs"].append(scene_input)
            path = self.root / f"{name}-{artifact_type}.json"
            path.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            artifacts.append(f"{artifact_type}={path}")
        actor = self.stage_actor(name)
        loop.command_evaluate_stage(
            self.args(
                state=self.state_path,
                actor=actor,
                execution=self.executions[actor],
                name=name,
                artifact=artifacts,
                scene=scene_path,
                note="check-recompute fixture",
            )
        )

    def advance_through(self, last: str) -> None:
        for name in ("evidence", "macro-hypothesis", "seed", "author",
                     "presentation-review", "regional-review", "global-review"):
            self.pass_stage(name)
            if name == last:
                return

    # --- SELF_REPORTED_CHECK_MISMATCH and fail-closed inputs -----------------

    def test_presentation_scene_sha_mismatch_fails(self) -> None:
        self.advance_through("author")
        with self.assertRaises(loop.WorkflowError) as caught:
            self.pass_stage(
                "presentation-review",
                {"presentation-review-receipt": {"sceneSha256": "f" * 64}},
            )
        self.assertEqual(caught.exception.code, "SELF_REPORTED_CHECK_MISMATCH")

    def test_presentation_receipt_without_scene_sha_fails_closed(self) -> None:
        self.advance_through("author")
        with self.assertRaises(loop.WorkflowError) as caught:
            self.pass_stage(
                "presentation-review",
                {"presentation-review-receipt": {"sceneSha256": None}},
            )
        self.assertEqual(caught.exception.code, "CHECK_RECOMPUTE_INPUT_MISSING")

    def test_omission_undisposed_candidate_contradicts_claim(self) -> None:
        self.proposals.write_text(
            json.dumps({"wallCandidates": [ELIGIBLE_CANDIDATE]}) + "\n",
            encoding="utf-8",
        )
        self.advance_through("presentation-review")
        with self.assertRaises(loop.WorkflowError) as caught:
            self.pass_stage("regional-review")
        self.assertEqual(caught.exception.code, "SELF_REPORTED_CHECK_MISMATCH")
        self.assertIn("allEligibleProposalsDisposed", str(caught.exception))
        # An explicit disposition for the same candidate satisfies the gate.
        self.pass_stage(
            "regional-review",
            {"omission-audit": {
                "recompute": {"proposalsPath": str(self.proposals)},
                "dispositions": {"cand-1": "WITHHOLD_NOT_A_WALL"},
            }},
        )
        state = loop.read_state(self.state_path)
        self.assertEqual(state["stages"]["regional-review"]["status"], "PASS")

    def test_omission_audit_without_proposals_fails_closed(self) -> None:
        self.advance_through("presentation-review")
        with self.assertRaises(loop.WorkflowError) as caught:
            self.pass_stage("regional-review", {"omission-audit": {"recompute": {}}})
        self.assertEqual(caught.exception.code, "CHECK_RECOMPUTE_INPUT_MISSING")

    def test_global_review_detects_crossing_walls(self) -> None:
        self.write_scene(self.build_scene(crossing=True))
        self.advance_through("regional-review")
        with self.assertRaises(loop.WorkflowError) as caught:
            self.pass_stage("global-review")
        self.assertEqual(caught.exception.code, "SELF_REPORTED_CHECK_MISMATCH")
        self.assertIn("collisions", str(caught.exception))

    def test_global_review_detects_topology_break(self) -> None:
        self.write_scene(self.build_scene(candidate_boundary=True))
        self.advance_through("regional-review")
        with self.assertRaises(loop.WorkflowError) as caught:
            self.pass_stage("global-review")
        self.assertEqual(caught.exception.code, "SELF_REPORTED_CHECK_MISMATCH")
        self.assertIn("topology", str(caught.exception))

    def test_measured_wall_without_index_fails_closed(self) -> None:
        self.write_scene(self.build_scene(measured=True))
        self.advance_through("regional-review")
        with self.assertRaises(loop.WorkflowError) as caught:
            self.pass_stage("global-review")
        self.assertEqual(caught.exception.code, "CHECK_RECOMPUTE_INPUT_MISSING")
        self.assertIn("indexPath", str(caught.exception))

    def test_clean_scene_passes_global_review_with_recomputed_checks(self) -> None:
        self.advance_through("global-review")
        state = loop.read_state(self.state_path)
        stage = state["stages"]["global-review"]
        self.assertEqual(stage["status"], "PASS")
        recomputed = stage["evaluation"]["checks"]["recomputedChecks"]
        self.assertTrue(recomputed["global-review-receipt"]["topology"])
        self.assertTrue(recomputed["global-review-receipt"]["collisions"])
        self.assertTrue(recomputed["global-review-receipt"]["scoreGate"])

    def test_score_gate_recompute_rejects_low_area_score(self) -> None:
        self.advance_through("regional-review")
        with self.assertRaises(loop.WorkflowError) as caught:
            self.pass_stage(
                "global-review",
                {"global-review-receipt": {
                    "areas": [{"id": "west-wing", "score": 84}],
                    "score": 95,
                    "recompute": {"proposalsPath": str(self.proposals)},
                    "dispositions": {},
                }},
            )
        self.assertEqual(caught.exception.code, "SELF_REPORTED_CHECK_MISMATCH")
        self.assertIn("scoreGate", str(caught.exception))

    # --- P1-7: area-scoped invalidation and partial re-review ----------------

    def open_issue(self, area: str) -> str:
        loop.command_open_issue(
            self.args(
                state=self.state_path,
                actor="author-west",
                execution=self.executions["author-west"],
                area=area,
                severity="P1",
                kind="missing-wall",
                target=["wall_a"],
                summary="area-scoped correction",
                evidence=[f"raw={self.raw}"],
            )
        )
        return loop.read_state(self.state_path)["issues"][-1]["id"]

    def patch_issue(self, issue_id: str, affected_areas: str | None) -> None:
        scene = json.loads(self.scene_path.read_text(encoding="utf-8"))
        scene.setdefault("meta", {})["patchNote"] = f"{issue_id}-{affected_areas}"
        self.write_scene(scene)
        loop.command_patch(
            self.args(
                state=self.state_path,
                actor="author-west",
                execution=self.executions["author-west"],
                issue=issue_id,
                scene=self.scene_path,
                checkpoint_dir=None,
                note="scoped patch",
                strategy_change=None,
                affected_areas=affected_areas,
            )
        )

    def resolve_issue(self, issue_id: str) -> None:
        loop.command_review(
            self.args(
                state=self.state_path,
                actor="reviewer-east",
                execution=self.executions["reviewer-east"],
                issue=issue_id,
                scene=self.scene_path,
                verdict="PASS",
                score=90,
                evidence=[f"render={self.render}", f"raw={self.raw}"],
                note="scoped patch accepted",
            )
        )

    def area_statuses(self) -> dict[str, str]:
        state = loop.read_state(self.state_path)
        return {
            area_id: record["status"]
            for area_id, record in state["stages"]["regional-review"]["areaReview"].items()
        }

    def test_area_scoped_patch_reopens_only_affected_area(self) -> None:
        self.advance_through("regional-review")
        self.assertEqual(
            self.area_statuses(),
            {"west-wing": "PASS", "east-wing": "PASS"},
        )
        issue_id = self.open_issue("west-wing")
        self.assertEqual(
            self.area_statuses(),
            {"west-wing": "PENDING", "east-wing": "PASS"},
        )
        self.patch_issue(issue_id, "west-wing")
        self.assertEqual(
            self.area_statuses(),
            {"west-wing": "PENDING", "east-wing": "PASS"},
        )
        state = loop.read_state(self.state_path)
        self.assertEqual(state["stages"]["regional-review"]["status"], "PENDING")
        self.resolve_issue(issue_id)
        self.pass_stage("author")
        self.pass_stage("presentation-review")
        # A receipt that skips the invalidated area cannot pass...
        with self.assertRaises(loop.WorkflowError) as caught:
            self.pass_stage(
                "regional-review",
                {"regional-review-receipt": {
                    "areas": [{"id": "east-wing", "score": 91}],
                }},
            )
        self.assertEqual(
            caught.exception.code, "REGIONAL_REVIEW_AREA_COVERAGE_INCOMPLETE"
        )
        # ...while covering only the invalidated area is sufficient.
        self.pass_stage(
            "regional-review",
            {"regional-review-receipt": {
                "areas": [{"id": "west-wing", "score": 93}],
            }},
        )
        self.assertEqual(
            self.area_statuses(),
            {"west-wing": "PASS", "east-wing": "PASS"},
        )

    def test_unrecorded_issue_area_reopens_every_area(self) -> None:
        self.advance_through("regional-review")
        self.open_issue("global")
        self.assertEqual(
            self.area_statuses(),
            {"west-wing": "PENDING", "east-wing": "PENDING"},
        )

    def test_patch_without_affected_areas_reopens_every_area(self) -> None:
        self.advance_through("regional-review")
        issue_id = self.open_issue("west-wing")
        self.patch_issue(issue_id, None)
        self.assertEqual(
            self.area_statuses(),
            {"west-wing": "PENDING", "east-wing": "PENDING"},
        )


if __name__ == "__main__":
    unittest.main()
