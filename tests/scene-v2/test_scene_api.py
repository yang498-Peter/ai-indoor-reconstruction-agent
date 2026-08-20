from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest

CORE = Path(__file__).resolve().parents[2] / "scene-core"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, CORE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


api = load("scene_api")
identity_api = load("execution_identity")
migrate_mod = load("migrate_scene_v1_to_v2")


def identity(actor, run_id, role, policy, reviewer_class=None):
    value = {
        "schemaVersion": "1.0", "actorId": actor, "runId": run_id, "role": role,
        "provider": "scene-api-test", "model": "fixture", "policyId": policy,
        "toolPolicyHash": identity_api.policy_digest(policy),
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "attestation": {"issuer": "scene-api-test", "enforcementMode": "application-enforced"},
    }
    if reviewer_class:
        value["reviewerClass"] = reviewer_class
    return value


AUTHOR = identity("author-a", "11111111-1111-4111-8111-111111111111", "author", "author-v1")
REVIEWER = identity(
    "reviewer-b", "22222222-2222-4222-8222-222222222222",
    "reviewer", "reviewer-readonly-v1", "regional",
)
SELF_REVIEWER = identity(
    "author-a", "33333333-3333-4333-8333-333333333333",
    "reviewer", "reviewer-readonly-v1", "regional",
)


class SceneApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scene_path = self.root / "scene.json"
        self.scene = api.new_scene("test-dataset", 3.0, 0.0, "author-a", AUTHOR)
        self.level = api.default_level_id(self.scene)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_wall(self, wall_id="wall_test01", start=(0, 0), end=(6, 0)):
        return api.op_create_wall(self.scene, {
            "id": wall_id, "start": list(start), "end": list(end),
            "height": 3.0, "thickness": 0.12, "level": self.level, "execution": AUTHOR,
        }, "author-a")

    def make_evidence_file(self, name="receipt.json", kind="test") -> str:
        path = self.root / name
        path.write_text(json.dumps({"kind": kind}), encoding="utf-8")
        return name

    def test_summary_excludes_level_container_from_evidence_statuses(self):
        summary = api.op_summary(self.scene)
        self.assertEqual(summary["nodeCounts"], {"level": 1})
        self.assertEqual(summary["evidenceStatuses"], {})

        self.make_wall()
        summary = api.op_summary(self.scene)
        self.assertEqual(summary["evidenceStatuses"], {"candidate": 1})

    def test_issue_transition_requires_independent_hash_bound_receipt(self):
        api.op_open_issue(self.scene, {
            "id": "I-test", "severity": "P1", "kind": "reconciliation",
            "summary": "needs reconciliation", "openedBy": "author-a", "execution": AUTHOR,
        })
        receipt = self.make_evidence_file("issue-receipt.json")

        with self.assertRaises(api.SceneError) as ctx:
            api.op_transition_issue(self.scene, self.scene_path, {
                "id": "I-test", "expectedStatus": "OPEN", "status": "RESOLVED",
                "reviewerIdentity": SELF_REVIEWER, "reason": "self review", "receiptPath": receipt,
            })
        self.assertIn("SELF_REVIEW_FORBIDDEN", str(ctx.exception))
        self.assertEqual(self.scene["review"]["issues"][0]["status"], "OPEN")

        result = api.op_transition_issue(self.scene, self.scene_path, {
            "id": "I-test", "expectedStatus": "OPEN", "status": "RESOLVED",
            "reviewerIdentity": REVIEWER, "reason": "receipt reconciles every named family",
            "receiptPath": receipt,
        })
        self.assertEqual(result["status"], "RESOLVED")
        self.assertEqual(result["resolution"]["previousStatus"], "OPEN")
        self.assertEqual(len(result["resolution"]["receipt"]["sha256"]), 64)

    def test_open_issue_refuses_duplicate_or_missing_target(self):
        result = api.op_open_issue(self.scene, {
            "id": "I-withheld", "severity": "P2", "kind": "withheld-scope",
            "summary": "unobserved member remains withheld", "openedBy": "author-a", "execution": AUTHOR,
        })
        self.assertEqual(result["status"], "OPEN")
        with self.assertRaises(api.SceneError) as ctx:
            api.op_open_issue(self.scene, {
                "id": "I-withheld", "severity": "P2", "kind": "withheld-scope",
                "summary": "duplicate", "openedBy": "author-a", "execution": AUTHOR,
            })
        self.assertIn("ISSUE_EXISTS", str(ctx.exception))
        with self.assertRaises(api.SceneError) as ctx:
            api.op_open_issue(self.scene, {
                "id": "I-missing-target", "severity": "P1", "kind": "withheld-scope",
                "summary": "bad target", "openedBy": "author-a", "targetNodeIds": ["item_missing"],
                "execution": AUTHOR,
            })
        self.assertIn("ISSUE_TARGET_MISSING", str(ctx.exception))

    def test_wall_and_hosted_door_validate(self):
        self.make_wall()
        api.op_add_opening(self.scene, "door", {
            "id": "door_a", "wall": "wall_test01", "offset": 2.0, "width": 0.9, "height": 2.05,
        }, "author-a")
        api.validate_scene(self.scene)
        wall = self.scene["nodes"]["wall_test01"]
        self.assertIn("door_a", wall["children"])
        self.assertEqual(self.scene["nodes"]["door_a"]["parentId"], "wall_test01")

    def test_opening_outside_wall_fails(self):
        self.make_wall()
        api.op_add_opening(self.scene, "door", {
            "id": "door_far", "wall": "wall_test01", "offset": 5.9, "width": 1.0, "height": 2.0,
        }, "author-a")
        with self.assertRaises(api.SceneError) as ctx:
            api.validate_scene(self.scene)
        self.assertIn("OPENING_OUTSIDE_WALL", str(ctx.exception))

    def test_opening_overlap_fails(self):
        self.make_wall()
        for door_id, offset in (("door_a", 2.0), ("door_b", 2.5)):
            api.op_add_opening(self.scene, "door", {
                "id": door_id, "wall": "wall_test01", "offset": offset, "width": 1.0, "height": 2.0,
            }, "author-a")
        with self.assertRaises(api.SceneError) as ctx:
            api.validate_scene(self.scene)
        self.assertIn("OPENING_OVERLAP", str(ctx.exception))

    def test_vertically_stacked_openings_are_legal(self):
        # Transom over a door / hatch under a window: same plan interval,
        # disjoint height bands - real construction, must validate.
        self.make_wall()
        api.op_add_opening(self.scene, "window", {
            "id": "window_up", "wall": "wall_test01", "offset": 3.0, "width": 1.0,
            "height": 0.9, "sill": 1.6,
        }, "author-a")
        api.op_add_opening(self.scene, "opening", {
            "id": "hatch_low", "wall": "wall_test01", "offset": 3.1, "width": 1.1,
            "height": 0.4, "sill": 0.1,
        }, "author-a")
        api.validate_scene(self.scene)

    def test_opening_taller_than_wall_fails(self):
        self.make_wall()
        api.op_add_opening(self.scene, "window", {
            "id": "window_tall", "wall": "wall_test01", "offset": 3.0, "width": 1.0,
            "height": 2.5, "sill": 0.9,
        }, "author-a")
        with self.assertRaises(api.SceneError) as ctx:
            api.validate_scene(self.scene)
        self.assertIn("OPENING_TALLER_THAN_WALL", str(ctx.exception))

    def test_accept_measured_requires_verified_file(self):
        self.make_wall()
        with self.assertRaises(api.SceneError) as ctx:
            api.op_accept(self.scene, self.scene_path, {
                "id": "wall_test01", "mode": "measured", "reviewerIdentity": REVIEWER,
            })
        self.assertIn("MEASURED_NEEDS_VERIFIED_SOURCE", str(ctx.exception))
        receipt = self.make_evidence_file()
        api.op_attach_evidence(self.scene, self.scene_path, {
            "id": "wall_test01", "type": "high-structure-slice", "path": receipt,
            "producer": "scene-api-test",
        })
        entry = api.op_accept(self.scene, self.scene_path, {
            "id": "wall_test01", "mode": "measured", "reviewerIdentity": REVIEWER,
        })
        self.assertEqual(entry["status"], "accepted-measured")

    def test_accept_measured_rejects_stale_hash(self):
        self.make_wall()
        receipt = self.make_evidence_file()
        api.op_attach_evidence(self.scene, self.scene_path, {
            "id": "wall_test01", "type": "elevation", "path": receipt,
            "producer": "scene-api-test",
        })
        (self.root / receipt).write_text(json.dumps({"kind": "tampered"}), encoding="utf-8")
        with self.assertRaises(api.SceneError) as ctx:
            api.op_accept(self.scene, self.scene_path, {
                "id": "wall_test01", "mode": "measured", "reviewerIdentity": REVIEWER,
            })
        self.assertIn("EVIDENCE_HASH_STALE", str(ctx.exception))

    def test_self_review_forbidden(self):
        self.make_wall()
        receipt = self.make_evidence_file()
        api.op_attach_evidence(self.scene, self.scene_path, {
            "id": "wall_test01", "type": "elevation", "path": receipt,
            "producer": "scene-api-test",
        })
        with self.assertRaises(api.SceneError) as ctx:
            api.op_accept(self.scene, self.scene_path, {
                "id": "wall_test01", "mode": "measured", "reviewerIdentity": SELF_REVIEWER,
            })
        self.assertIn("SELF_REVIEW_FORBIDDEN", str(ctx.exception))

    def test_accept_inferred_needs_two_sources_and_reason(self):
        self.make_wall()
        api.op_attach_evidence(self.scene, self.scene_path, {
            "id": "wall_test01", "type": "inference-basis", "note": "symmetry", "allow_missing": True,
        })
        with self.assertRaises(api.SceneError):
            api.op_accept(self.scene, self.scene_path, {
                "id": "wall_test01", "mode": "inferred", "reviewerIdentity": REVIEWER, "reason": "mirror of east wing",
            })
        elevation = self.make_evidence_file("inferred-elevation.json", "elevation")
        photo = self.make_evidence_file("inferred-photo.json", "photo")
        for evidence_type, path in (("elevation", elevation), ("photo", photo)):
            api.op_attach_evidence(self.scene, self.scene_path, {
                "id": "wall_test01", "type": evidence_type, "path": path,
                "producer": "scene-api-test",
            })
        entry = api.op_accept(self.scene, self.scene_path, {
            "id": "wall_test01", "mode": "inferred", "reviewerIdentity": REVIEWER, "reason": "mirror of east wing",
        })
        self.assertEqual(entry["status"], "accepted-inferred")

    def _attach_with_receipt(self, wall_id, name, content, roots, producer, parameters=None):
        (self.root / name).write_bytes(content)
        receipt_name = f"{name}.provenance.json"
        receipt = {
            "outputContentSha256": hashlib.sha256(content).hexdigest(),
            "rootContentSha256s": roots,
            "producer": producer,
        }
        if parameters is not None:
            receipt["generatorParameters"] = parameters
        (self.root / receipt_name).write_text(json.dumps(receipt), encoding="utf-8")
        return api.op_attach_evidence(self.scene, self.scene_path, {
            "id": wall_id, "type": "derived", "path": name,
            "provenanceReceipt": receipt_name,
        })

    def test_accept_inferred_from_two_derivations_of_one_root(self):
        # New lineage rule: the derivation pipeline identity (producer +
        # generator parameters), not root disjointness, defines independence.
        self.make_wall()
        root = "e" * 64
        self._attach_with_receipt(
            "wall_test01", "band-a.bin", b"band a", [root],
            "pointcloud-evidence", {"band": [0.3, 0.9]})
        self._attach_with_receipt(
            "wall_test01", "band-b.bin", b"band b", [root],
            "pointcloud-evidence", {"band": [1.8, 2.4]})
        sources = self.scene["evidence"]["wall_test01"]["sources"]
        self.assertNotEqual(sources[0]["lineageId"], sources[1]["lineageId"])
        entry = api.op_accept(self.scene, self.scene_path, {
            "id": "wall_test01", "mode": "inferred", "reviewerIdentity": REVIEWER,
            "reason": "two band derivations of one capture",
        })
        self.assertEqual(entry["status"], "accepted-inferred")
        api.validate_scene(self.scene)

    def test_accept_inferred_rejects_same_parameter_rerun(self):
        self.make_wall()
        root = "e" * 64
        self._attach_with_receipt(
            "wall_test01", "run-a.bin", b"run a bytes", [root],
            "pointcloud-evidence", {"band": [0.3, 0.9]})
        self._attach_with_receipt(
            "wall_test01", "run-b.bin", b"run b bytes", [root],
            "pointcloud-evidence", {"band": [0.3, 0.9]})
        sources = self.scene["evidence"]["wall_test01"]["sources"]
        self.assertEqual(sources[0]["lineageId"], sources[1]["lineageId"])
        with self.assertRaises(api.SceneError) as ctx:
            api.op_accept(self.scene, self.scene_path, {
                "id": "wall_test01", "mode": "inferred", "reviewerIdentity": REVIEWER,
                "reason": "same pipeline twice",
            })
        self.assertIn("INFERRED_NEEDS_TWO_DISTINCT_SOURCES", str(ctx.exception))

    def test_geometry_update_invalidates_acceptance(self):
        self.make_wall()
        receipt = self.make_evidence_file()
        api.op_attach_evidence(self.scene, self.scene_path, {
            "id": "wall_test01", "type": "elevation", "path": receipt,
            "producer": "scene-api-test",
        })
        api.op_accept(self.scene, self.scene_path, {
            "id": "wall_test01", "mode": "measured", "reviewerIdentity": REVIEWER,
        })
        api.op_update_node(self.scene, {"id": "wall_test01", "updates": {"height": 2.8}})
        self.assertEqual(self.scene["evidence"]["wall_test01"]["status"], "candidate")

    def test_protected_fields_rejected(self):
        self.make_wall()
        with self.assertRaises(api.SceneError):
            api.op_update_node(self.scene, {"id": "wall_test01", "updates": {"parentId": "level_x"}})

    def test_delete_cascades_and_syncs_parent(self):
        self.make_wall()
        api.op_add_opening(self.scene, "door", {
            "id": "door_a", "wall": "wall_test01", "offset": 2.0, "width": 0.9, "height": 2.05,
        }, "author-a")
        removed = api.op_delete_node(self.scene, "wall_test01")
        self.assertIn("door_a", removed)
        self.assertNotIn("wall_test01", self.scene["nodes"])
        self.assertNotIn("wall_test01", self.scene["nodes"][self.level]["children"])
        api.validate_scene(self.scene)

    def test_save_undo_roundtrip(self):
        self.make_wall()
        api.save_scene(self.scene_path, self.scene, "author-a")
        loaded = api.load_scene(self.scene_path)
        api.op_create_wall(loaded, {
            "id": "wall_second", "start": [0, 2], "end": [4, 2],
            "height": 3.0, "thickness": 0.12, "level": self.level,
        }, "author-a")
        api.save_scene(self.scene_path, loaded, "author-a")
        self.assertIn("wall_second", api.load_scene(self.scene_path)["nodes"])
        api.undo(self.scene_path)
        self.assertNotIn("wall_second", api.load_scene(self.scene_path)["nodes"])

    def test_apply_patch_is_atomic(self):
        api.save_scene(self.scene_path, self.scene, "author-a")
        loaded = api.load_scene(self.scene_path)
        ops = [
            {"op": "create_wall", "id": "wall_ok", "start": [0, 0], "end": [5, 0], "height": 3.0, "thickness": 0.12},
            {"op": "add_door", "id": "door_bad", "wall": "wall_ok", "offset": 9.0, "width": 1.0, "height": 2.0},
        ]
        api.apply_ops(loaded, self.scene_path, ops, "author-a")
        with self.assertRaises(api.SceneError):
            api.save_scene(self.scene_path, loaded, "author-a")
        # Disk copy untouched by the failed batch.
        self.assertNotIn("wall_ok", api.load_scene(self.scene_path)["nodes"])

    def test_measure_wall_and_distance(self):
        self.make_wall()
        self.make_wall("wall_other", (0, 4), (6, 4))
        self.assertAlmostEqual(api.op_measure(self.scene, {"id": "wall_test01"})["lengthM"], 6.0)
        result = api.op_measure(self.scene, {"id": None, "from": "wall_test01", "to": "wall_other"})
        self.assertAlmostEqual(result["planDistanceM"], 4.0)


class MigrationTest(unittest.TestCase):
    def test_v1_scene_migrates_and_hosts_doors(self):
        v1 = {
            "dataset": "legacy",
            "levels": [{"height": 3.05}],
            "structures": [
                {
                    "id": "Wall17", "category": "wall", "geometryType": "segment",
                    "start": [0, 0, 0], "end": [8, 0, 0], "height": 3.05, "thickness": 0.12,
                    "decision": {"status": "accepted", "reason": "high return"},
                    "evidence": {"sourceLayer": "overview", "state": "measured", "reviewer": "agent-x"},
                },
                {
                    # Door lying on the wall centerline (display z=0 -> source y=0).
                    "id": "Door03", "category": "door", "geometryType": "segment",
                    "start": [2.0, 0, 0], "end": [2.9, 0, 0], "height": 2.05,
                    "decision": {"status": "accepted"},
                    "evidence": {"sourceLayer": "photo", "state": "photo-confirmed"},
                },
                {
                    "id": "Floor1", "category": "floor-zone", "geometryType": "polygon",
                    "points": [[0, 0, 0], [8, 0, 0], [8, 0, -6], [0, 0, -6]], "height": 0.05,
                    "decision": {"status": "accepted"}, "evidence": {"state": "measured"},
                },
            ],
            "structureCandidates": [
                {
                    "id": "Cand1", "category": "wall", "geometryType": "segment",
                    "start": [0, 0, -3], "end": [2, 0, -3], "height": 3.0, "thickness": 0.12,
                },
            ],
            "objects": [
                {
                    "id": "Table5", "category": "table", "center": [4, 0, -3], "yaw": 0.3,
                    "size": [1.6, 0.75, 0.8], "color": "#9c7f5c",
                    "deliveryValidation": {"status": "PASS"},
                },
            ],
        }
        scene, report = migrate_mod.migrate(v1, "migration-test")
        api.validate_scene(scene)
        self.assertEqual(report["hostedOpenings"], 1)
        self.assertEqual(report["freeOpenings"], 0)
        door = scene["nodes"]["door_door03"]
        self.assertEqual(scene["nodes"][door["parentId"]]["type"], "wall")
        self.assertAlmostEqual(door["hostOffsetM"], 2.45, places=3)
        self.assertAlmostEqual(door["width"], 0.9, places=3)
        # Display [8,0,-6] -> source [8, 6].
        slab = scene["nodes"]["slab_floor1"]
        self.assertIn([8.0, 6.0], slab["polygon"])
        self.assertEqual(scene["evidence"]["wall_cand1"]["status"], "candidate")
        self.assertEqual(scene["evidence"]["item_table5"]["status"], "candidate")
        self.assertEqual(scene["evidence"]["item_table5"]["legacyDisposition"], "accepted-measured")
        wall_entry = scene["evidence"]["wall_wall17"]
        self.assertEqual(wall_entry["status"], "candidate")
        self.assertEqual(wall_entry["legacyDisposition"], "accepted-measured")
        self.assertTrue(wall_entry["sources"])


if __name__ == "__main__":
    unittest.main()
