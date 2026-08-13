from __future__ import annotations

import importlib.util
import json
import math
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
migrate_mod = load("migrate_scene_v1_to_v2")


class SceneApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scene_path = self.root / "scene.json"
        self.scene = api.new_scene("test-dataset", 3.0, 0.0, "author-a")
        self.level = api.default_level_id(self.scene)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_wall(self, wall_id="wall_test01", start=(0, 0), end=(6, 0)):
        return api.op_create_wall(self.scene, {
            "id": wall_id, "start": list(start), "end": list(end),
            "height": 3.0, "thickness": 0.12, "level": self.level,
        }, "author-a")

    def make_evidence_file(self, name="receipt.json") -> str:
        path = self.root / name
        path.write_text(json.dumps({"kind": "test"}), encoding="utf-8")
        return name

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
                "id": "wall_test01", "mode": "measured", "reviewer": "reviewer-b",
            })
        self.assertIn("MEASURED_NEEDS_VERIFIED_SOURCE", str(ctx.exception))
        receipt = self.make_evidence_file()
        api.op_attach_evidence(self.scene, self.scene_path, {
            "id": "wall_test01", "type": "high-structure-slice", "path": receipt,
        })
        entry = api.op_accept(self.scene, self.scene_path, {
            "id": "wall_test01", "mode": "measured", "reviewer": "reviewer-b",
        })
        self.assertEqual(entry["status"], "accepted-measured")

    def test_accept_measured_rejects_stale_hash(self):
        self.make_wall()
        receipt = self.make_evidence_file()
        api.op_attach_evidence(self.scene, self.scene_path, {
            "id": "wall_test01", "type": "elevation", "path": receipt,
        })
        (self.root / receipt).write_text(json.dumps({"kind": "tampered"}), encoding="utf-8")
        with self.assertRaises(api.SceneError) as ctx:
            api.op_accept(self.scene, self.scene_path, {
                "id": "wall_test01", "mode": "measured", "reviewer": "reviewer-b",
            })
        self.assertIn("EVIDENCE_HASH_STALE", str(ctx.exception))

    def test_self_review_forbidden(self):
        self.make_wall()
        receipt = self.make_evidence_file()
        api.op_attach_evidence(self.scene, self.scene_path, {
            "id": "wall_test01", "type": "elevation", "path": receipt,
        })
        with self.assertRaises(api.SceneError) as ctx:
            api.op_accept(self.scene, self.scene_path, {
                "id": "wall_test01", "mode": "measured", "reviewer": "author-a",
            })
        self.assertIn("SELF_REVIEW_FORBIDDEN", str(ctx.exception))

    def test_accept_inferred_needs_two_sources_and_reason(self):
        self.make_wall()
        api.op_attach_evidence(self.scene, self.scene_path, {
            "id": "wall_test01", "type": "inference-basis", "note": "symmetry", "allow_missing": True,
        })
        with self.assertRaises(api.SceneError):
            api.op_accept(self.scene, self.scene_path, {
                "id": "wall_test01", "mode": "inferred", "reviewer": "reviewer-b", "reason": "mirror of east wing",
            })
        api.op_attach_evidence(self.scene, self.scene_path, {
            "id": "wall_test01", "type": "inference-basis", "note": "repeated module", "allow_missing": True,
        })
        entry = api.op_accept(self.scene, self.scene_path, {
            "id": "wall_test01", "mode": "inferred", "reviewer": "reviewer-b", "reason": "mirror of east wing",
        })
        self.assertEqual(entry["status"], "accepted-inferred")

    def test_geometry_update_invalidates_acceptance(self):
        self.make_wall()
        receipt = self.make_evidence_file()
        api.op_attach_evidence(self.scene, self.scene_path, {
            "id": "wall_test01", "type": "elevation", "path": receipt,
        })
        api.op_accept(self.scene, self.scene_path, {
            "id": "wall_test01", "mode": "measured", "reviewer": "reviewer-b",
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
        self.assertEqual(scene["evidence"]["item_table5"]["status"], "accepted-measured")
        wall_entry = scene["evidence"]["wall_wall17"]
        self.assertEqual(wall_entry["status"], "accepted-measured")
        self.assertTrue(wall_entry["sources"])


if __name__ == "__main__":
    unittest.main()
