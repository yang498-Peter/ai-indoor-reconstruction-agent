from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "scene-core"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, CORE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


topology = load("candidate_topology")


def face(face_id: str, start, end, *, support: int = 500, residual: float = 0.02) -> dict:
    return {
        "id": face_id,
        "start": list(start),
        "end": list(end),
        "lengthM": math.dist(start, end),
        "angleDeg": math.degrees(math.atan2(end[1] - start[1], end[0] - start[0])) % 180.0,
        "supportPointCount": support,
        "residualP50M": residual * 0.5,
        "residualP90M": residual,
    }


def wall(
    wall_id: str,
    start,
    end,
    *,
    support: int = 800,
    residual: float = 0.02,
    wall_mode: str = "paired-faces",
    status: str = "candidate",
) -> dict:
    return {
        "id": wall_id,
        "status": status,
        "wallMode": wall_mode,
        "sourceFaceIds": [f"{wall_id}-a", f"{wall_id}-b"] if wall_mode == "paired-faces" else [f"{wall_id}-face"],
        "rawCenterline": {"start": list(start), "end": list(end)},
        "suggestedCenterline": {"start": list(start), "end": list(end)},
        "thicknessM": 0.12,
        "lengthM": math.dist(start, end),
        "supportPointCount": support,
        "fitResidualP50M": residual * 0.5,
        "fitResidualP90M": residual,
        "confidence": 0.9 if support >= 500 else 0.3,
    }


def two_room_document(*, translate=(0.0, 0.0), reverse: bool = False) -> dict:
    dx, dy = translate

    def p(x, y):
        return [x + dx, y + dy]

    rows = [
        wall("south", p(0, 0), p(8, 0)),
        wall("north", p(0, 4), p(8, 4)),
        wall("west", p(0, 0), p(0, 4)),
        wall("east", p(8, 0), p(8, 4)),
        wall("divider", p(4, 0), p(4, 4)),
        wall("south-duplicate", p(0, 0.025), p(8, 0.025), support=120, residual=0.07),
    ]
    if reverse:
        rows.reverse()
    return {
        "schemaVersion": 2,
        "kind": "structural-proposals",
        "status": "CANDIDATES_ONLY",
        "indexFingerprint": "f" * 64,
        "indexManifestSha256": "e" * 64,
        "faceObservations": [],
        "wallCandidates": rows,
    }


class CandidateTopologyTest(unittest.TestCase):
    def test_single_face_keeps_observation_and_two_centerline_alternatives(self):
        document = {
            "faceObservations": [face("face-001", (0.0, 0.0), (5.0, 0.0))],
            "wallCandidates": [wall("single", (0.0, 0.0), (5.0, 0.0), wall_mode="single-face")],
        }
        report = topology.optimize_topology(document)
        hypothesis = next(item for item in report["wallHypotheses"] if item["sourceCandidateId"] == "single")
        self.assertEqual(hypothesis["wallMode"], "single-face")
        self.assertEqual(hypothesis["status"], "candidate")
        self.assertEqual(len(hypothesis["alternatives"]), 2)
        offsets = sorted(round(item["centerline"]["start"][1], 3) for item in hypothesis["alternatives"])
        self.assertEqual(offsets, [-0.06, 0.06])
        self.assertEqual(hypothesis["authorityEligibility"], "inferred-only-until-corroborated")
        self.assertIn("face-001", report["observationsById"])

    def test_continuity_segmentation_bridges_small_gap_but_not_large_gap(self):
        document = {
            "faceObservations": [
                face("a", (0.0, 0.0), (2.0, 0.0)),
                face("b", (2.12, 0.01), (4.0, 0.01)),
                face("c", (5.0, 0.0), (7.0, 0.0)),
            ],
            "wallCandidates": [],
        }
        report = topology.optimize_topology(document)
        segments = report["continuitySegments"]
        self.assertEqual(len(segments), 2)
        merged = next(item for item in segments if item["sourceObservationIds"] == ["a", "b"])
        self.assertAlmostEqual(merged["supportIntervalsM"][0][0], 0.0, places=2)
        self.assertEqual(len(merged["supportIntervalsM"]), 2)
        self.assertAlmostEqual(merged["lengthM"], 4.0, places=2)
        self.assertAlmostEqual(merged["bridgedGapsM"][0], 0.12, places=2)
        self.assertLessEqual(merged["maxBridgedGapM"], 0.20)

    def test_zone_aware_axes_preserve_two_non_manhattan_regions(self):
        rows = [
            wall("z1-a", (0, 0), (5, 0)),
            wall("z1-b", (0, 0), (0, 4)),
            wall("z2-a", (30, 0), (34.330127, 2.5)),
            wall("z2-b", (30, 0), (28, 3.464102)),
        ]
        report = topology.optimize_topology({"faceObservations": [], "wallCandidates": rows})
        self.assertEqual(len(report["zones"]), 2)
        axes = [sorted(round(item["angleDeg"]) for item in zone["axisFamilies"]) for zone in report["zones"]]
        self.assertIn([0, 90], axes)
        self.assertIn([30, 120], axes)
        self.assertFalse(report["profile"]["manhattanHardConstraint"])

    def test_rotated_non_manhattan_room_closes_without_axis_forcing(self):
        angle = math.radians(30.0)
        u = (math.cos(angle), math.sin(angle))
        v = (-math.sin(angle), math.cos(angle))

        def p(a, b):
            return [a * u[0] + b * v[0], a * u[1] + b * v[1]]

        rows = [
            wall("a", p(0, 0), p(6, 0)),
            wall("b", p(6, 0), p(6, 4)),
            wall("c", p(6, 4), p(0, 4)),
            wall("d", p(0, 4), p(0, 0)),
        ]
        report = topology.optimize_topology({"faceObservations": [], "wallCandidates": rows})
        self.assertEqual(report["topology"]["roomCount"], 1)
        angles = sorted(round(item["angleDeg"]) for item in report["zones"][0]["axisFamilies"])
        self.assertEqual(angles, [30, 120])

    def test_supported_cross_junction_is_not_rejected_as_illegal_crossing(self):
        rows = [
            wall("horizontal", (-4, 0), (4, 0)),
            wall("vertical", (0, -4), (0, 4)),
        ]
        report = topology.optimize_topology({"faceObservations": [], "wallCandidates": rows})
        self.assertEqual(
            set(report["selection"]["selectedSourceCandidateIds"]),
            {"horizontal", "vertical"},
        )

    def test_global_selection_recovers_two_rooms_and_rejects_duplicate(self):
        report = topology.optimize_topology(two_room_document())
        selected = set(report["selection"]["selectedSourceCandidateIds"])
        self.assertEqual(selected, {"south", "north", "west", "east", "divider"})
        self.assertIn("south-duplicate", report["selection"]["rejectedSourceCandidateIds"])
        self.assertEqual(report["topology"]["roomCount"], 2)
        self.assertEqual(report["topology"]["adjacencyCount"], 1)
        self.assertEqual(report["status"], "CANDIDATE_SELECTION_PROPOSED")
        self.assertEqual(report["authorityRule"], "proposal-only-agent-review-required")

    def test_model_complexity_rejects_weak_isolated_fragment(self):
        document = two_room_document()
        document["wallCandidates"].append(
            wall("weak-fragment", (20, 20), (20.7, 20), support=40, residual=0.12)
        )
        report = topology.optimize_topology(document)
        self.assertNotIn("weak-fragment", report["selection"]["selectedSourceCandidateIds"])
        self.assertIn("weak-fragment", report["selection"]["rejectedSourceCandidateIds"])

    def test_floor_and_scan_boundaries_never_become_wall_hypotheses(self):
        boundaries = {
            "scanCoverageBoundary": [[0, 0], [8, 0], [8, 4], [0, 4]],
            "floorSupportPolygon": [[0.2, 0.2], [7.8, 0.2], [7.8, 3.8], [0.2, 3.8]],
            "inferredRoomBoundary": [[0, 0], [8, 0], [8, 4], [0, 4]],
        }
        report = topology.optimize_topology(
            {"faceObservations": [], "wallCandidates": []}, boundary_semantics=boundaries
        )
        self.assertEqual(report["boundarySemantics"], boundaries)
        self.assertEqual(report["wallHypotheses"], [])
        self.assertEqual(report["selection"]["selectedHypothesisIds"], [])
        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertIn("TOPOLOGY_NO_ELIGIBLE_HYPOTHESES", report["blockingCodes"])

    def test_rejected_candidates_cannot_reenter_selection(self):
        rejected = wall("rejected", (0, 0), (5, 0), status="rejected")
        report = topology.optimize_topology({"faceObservations": [], "wallCandidates": [rejected]})
        self.assertEqual(report["selection"]["selectedHypothesisIds"], [])
        self.assertIn("TOPOLOGY_NO_ELIGIBLE_HYPOTHESES", report["blockingCodes"])

    def test_open_wall_set_stays_review_required(self):
        report = topology.optimize_topology(
            {"faceObservations": [], "wallCandidates": [wall("open", (0, 0), (5, 0))]}
        )
        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertIn("TOPOLOGY_NO_CLOSED_ROOM_CANDIDATE", report["blockingCodes"])

    def test_malformed_single_face_alternative_fails_with_stable_code(self):
        bad = {
            "hypothesisId": "bad",
            "sourceCandidateId": "bad",
            "status": "candidate",
            "wallMode": "single-face",
            "thicknessM": 0.12,
            "supportPointCount": 500,
            "fitResidualP90M": 0.02,
            "centerlineAlternatives": [
                {"id": "only-one", "centerline": {"start": [0, 0], "end": [5, 0]}}
            ],
        }
        with self.assertRaisesRegex(
            topology.CandidateTopologyError, "TOPOLOGY_SINGLE_FACE_ALTERNATIVES_REQUIRED"
        ):
            topology.optimize_topology({"faceObservations": [], "wallHypotheses": [bad]})

    def test_order_and_large_translation_preserve_selection_and_topology(self):
        baseline = topology.optimize_topology(two_room_document())
        changed = topology.optimize_topology(two_room_document(translate=(1_000_000.0, -2_000_000.0), reverse=True))
        self.assertEqual(
            set(baseline["selection"]["selectedSourceCandidateIds"]),
            set(changed["selection"]["selectedSourceCandidateIds"]),
        )
        self.assertEqual(baseline["topology"]["roomCount"], changed["topology"]["roomCount"])
        self.assertEqual(baseline["topology"]["adjacencyCount"], changed["topology"]["adjacencyCount"])

    def test_small_shared_vertex_noise_preserves_two_room_topology(self):
        offsets = {
            (0, 0): (0.008, -0.006),
            (8, 0): (-0.007, 0.004),
            (0, 4): (0.006, -0.005),
            (8, 4): (-0.004, 0.007),
            (4, 0): (0.005, 0.003),
            (4, 4): (-0.006, -0.004),
        }

        def p(point):
            dx, dy = offsets[point]
            return [point[0] + dx, point[1] + dy]

        rows = [
            wall("south", p((0, 0)), p((8, 0))),
            wall("north", p((0, 4)), p((8, 4))),
            wall("west", p((0, 0)), p((0, 4))),
            wall("east", p((8, 0)), p((8, 4))),
            wall("divider", p((4, 0)), p((4, 4))),
        ]
        report = topology.optimize_topology({"faceObservations": [], "wallCandidates": rows})
        self.assertEqual(report["topology"]["roomCount"], 2)
        self.assertEqual(report["topology"]["adjacencyCount"], 1)

    def test_cleanroom_profile_is_explicit_and_not_the_default(self):
        default = topology.load_profile(None)
        cleanroom = topology.load_profile(ROOT / "profiles" / "cleanroom-v1.json")
        self.assertEqual(default["id"], "default-office-v1")
        self.assertEqual(cleanroom["id"], "cleanroom-v1")
        self.assertNotIn("cleanroom", json.dumps(default).casefold())
        self.assertTrue(cleanroom["explicitOptInRequired"])

    def test_contract_schemas_and_profiles_parse(self):
        artifact_schema = json.loads(
            (ROOT / "schemas" / "candidate-topology-v1.schema.json").read_text(encoding="utf-8")
        )
        profile_schema = json.loads(
            (ROOT / "schemas" / "candidate-topology-profile-v1.schema.json").read_text(encoding="utf-8")
        )
        self.assertIn("authorityRule", artifact_schema["required"])
        self.assertEqual(profile_schema["properties"]["manhattanHardConstraint"]["const"], False)
        for name in ("default-office-v1.json", "cleanroom-v1.json"):
            profile = json.loads((ROOT / "profiles" / name).read_text(encoding="utf-8"))
            self.assertTrue(set(profile_schema["required"]).issubset(profile))

    def test_profile_rejects_manhattan_hard_constraint(self):
        profile = topology.load_profile(None)
        profile["manhattanHardConstraint"] = True
        with self.assertRaisesRegex(topology.CandidateTopologyError, "TOPOLOGY_PROFILE_INVALID"):
            topology.optimize_topology(two_room_document(), profile=profile)

    def test_cli_writes_hash_bound_atomic_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "structural-proposals.json"
            output = root / "candidate-topology.json"
            source.write_text(json.dumps(two_room_document()), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CORE / "candidate_topology.py"),
                    f"--proposals={source}",
                    f"--output={output}",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["schemaVersion"], "1.0")
            self.assertEqual(report["kind"], "candidate-topology-optimization")
            self.assertEqual(len(report["inputHashes"]["proposalsSha256"]), 64)
            self.assertEqual(len(report["configDigest"]), 64)
            self.assertEqual(len(report["producer"]["codeSha256"]), 64)
            self.assertFalse(any(root.glob(f".{output.name}.*")))


if __name__ == "__main__":
    unittest.main()
