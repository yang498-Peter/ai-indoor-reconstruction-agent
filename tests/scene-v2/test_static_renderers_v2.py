import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StaticRendererV2Tests(unittest.TestCase):
    def setUp(self):
        scripts = ROOT / ".codex" / "skills" / "reconstruct-indoor-scene" / "scripts"
        self.plan = load_module("render_scene_plan", scripts / "render_scene_plan.py")
        self.oblique = load_module("render_scene_oblique", scripts / "render_scene_oblique.py")
        self.scene = {
            "schemaVersion": "2.0",
            "nodes": {
                "level": {"id": "level", "type": "level", "height": 3.0},
                "wall": {"id": "wall", "type": "wall", "wallKind": "solid", "start": [0, 0], "end": [2, 0], "height": 3, "thickness": .1},
                "opening": {"id": "opening", "type": "opening", "parentId": "wall", "hostOffsetM": 1, "width": .8, "height": 2.1, "sillHeight": 0},
                "slab": {"id": "slab", "type": "slab", "polygon": [[0, 0], [2, 0], [2, 2], [0, 2]], "elevation": -.05},
                "item": {"id": "item", "type": "item", "category": "table", "center": [1, 1], "size": [1, .7, .5], "elevation": 0, "yaw": 0},
            },
            "evidence": {key: {"status": "accepted-measured"} for key in ("wall", "slab", "item")},
            "review": {"issues": []},
        }

    def test_v2_compiles_non_empty_review_surface(self):
        self.scene["nodes"]["wall"]["children"] = ["opening"]
        self.scene["evidence"]["opening"] = {"status": "accepted-measured"}
        for module in (self.plan, self.oblique):
            compiled = module.compile_scene_v2_for_review(self.scene)
            self.assertEqual(len(compiled["structures"]), 4)
            self.assertEqual(len(compiled["objects"]), 1)
            self.assertEqual(compiled["structures"][0]["start"], [0, 0, 0])
            self.assertTrue(compiled["objects"][0]["_atomicV2"])

    def test_door_and_window_cut_the_host_wall(self):
        self.scene["nodes"]["wall"]["children"] = ["opening"]
        self.scene["evidence"]["opening"] = {"status": "accepted-measured"}
        for opening_type in ("door", "window"):
            self.scene["nodes"]["opening"]["type"] = opening_type
            for module in (self.plan, self.oblique):
                compiled = module.compile_scene_v2_for_review(self.scene)
                wall_parts = [row for row in compiled["structures"] if row["id"].startswith(("wall-", "opening-"))]
                self.assertGreater(len(wall_parts), 1, f"{module.__name__} ignored {opening_type}")


if __name__ == "__main__":
    unittest.main()
