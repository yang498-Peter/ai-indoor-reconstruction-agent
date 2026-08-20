from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "scene-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pp = load("photo_projection", CORE / "photo_projection.py")
rpo = load("render_photo_overlay", CORE / "render_photo_overlay.py")
wd = load("wall_dossier", CORE / "wall_dossier.py")


def look_at_c2w(position, target, up=(0.0, 0.0, 1.0)) -> np.ndarray:
    position = np.asarray(position, dtype=np.float64)
    forward = np.asarray(target, dtype=np.float64) - position
    forward = forward / np.linalg.norm(forward)
    z_axis = -forward
    x_axis = np.cross(forward, np.asarray(up, dtype=np.float64))
    x_axis = x_axis / np.linalg.norm(x_axis)
    y_axis = np.cross(z_axis, x_axis)
    c2w = np.eye(4)
    c2w[:3, 0] = x_axis
    c2w[:3, 1] = y_axis
    c2w[:3, 2] = z_axis
    c2w[:3, 3] = position
    return c2w


def build_capture(root: Path, cameras: list[tuple]) -> Path:
    """Synthetic transforms.json + images: 400x400 pinhole, 90 deg fov."""
    (root / "undistort").mkdir(parents=True, exist_ok=True)
    frames = []
    for index, (position, target) in enumerate(cameras):
        name = f"img{index}.png"
        image = Image.new("RGB", (400, 400), (90 + 20 * index, 90, 90))
        image.save(root / "undistort" / name)
        frames.append(
            {
                "file_path": name,
                "timestamp": 1000 + index,
                "transform_matrix": look_at_c2w(position, target).tolist(),
            }
        )
    value = {
        "undistort_camera_model": {
            "width": 400,
            "height": 400,
            "intrinsic": [[200, 0, 200], [0, 200, 200], [0, 0, 1]],
        },
        "frames": frames,
    }
    path = root / "transforms.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def build_scene(root: Path) -> Path:
    scene = {
        "schemaVersion": "2.0",
        "dataset": "synthetic-dossier",
        "coordinateFrame": {"authority": "source-plan-meters-z-up"},
        "nodes": {
            "level": {"id": "level", "type": "level", "children": ["wall_test"]},
            "wall_test": {
                "id": "wall_test",
                "type": "wall",
                "parentId": "level",
                "start": [0.0, 0.0],
                "end": [4.0, 0.0],
                "height": 3.0,
                "thickness": 0.2,
                "baseHeight": 0.0,
                "children": ["win_test"],
                "material": {"color": "#888888", "description": "test"},
            },
            "win_test": {
                "id": "win_test",
                "type": "window",
                "parentId": "wall_test",
                "hostOffsetM": 1.0,
                "width": 1.0,
                "sillHeight": 1.0,
                "height": 1.2,
                "children": [],
            },
        },
        "rootNodeIds": ["level"],
        "evidence": {
            "wall_test": {
                "status": "accepted-measured",
                "sources": [
                    {"type": "elevation", "path": "sections/missing.png", "note": "n/a"}
                ],
            }
        },
        "review": {"issues": []},
        "meta": {"displayOffset": [0.0, 0.0]},
        "revision": 1,
    }
    (root / "evidence").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (100, 100), (30, 30, 30)).save(root / "evidence" / "band-walls.png")
    (root / "evidence" / "evidence-manifest.json").write_text(
        json.dumps(
            {
                "grid": {
                    "originX": -5.0,
                    "originY": 5.0,
                    "cellSizeM": 0.1,
                    "widthPx": 100,
                    "heightPx": 100,
                }
            }
        ),
        encoding="utf-8",
    )
    path = root / "scene.json"
    path.write_text(json.dumps(scene), encoding="utf-8")
    return path


FRONTAL = ((2.0, -4.0, 1.5), (2.0, 0.0, 1.5))
OBLIQUE = ((7.0, -1.2, 1.5), (2.0, 0.0, 1.5))
BEHIND = ((2.0, 4.0, 1.5), (2.0, 8.0, 1.5))  # looks away from the wall


class ScoreFramesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.transforms = build_capture(self.root, [FRONTAL, OBLIQUE, BEHIND])
        self.frames = pp.load_frames(self.transforms)
        self.wire = pp.wall_wireframe(
            {"start": [0.0, 0.0], "end": [4.0, 0.0], "height": 3.0, "baseHeight": 0.0},
            0.0,
            [],
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_frontal_view_outranks_oblique_and_excludes_backward(self):
        picked = rpo.score_frames(self.frames, self.wire, top_n=3, min_coverage=0.3)
        self.assertGreaterEqual(len(picked), 1)
        self.assertEqual(picked[0][0].file_path, "img0.png")
        picked_paths = [frame.file_path for frame, _ in picked]
        self.assertNotIn("img2.png", picked_paths)

    def test_selection_stats_are_reported(self):
        picked = rpo.score_frames(self.frames, self.wire, top_n=1, min_coverage=0.3)
        _, stats = picked[0]
        self.assertIn("coverage", stats)
        self.assertIn("distanceM", stats)
        self.assertGreater(stats["coverage"], 0.9)


class RenderOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.transforms = build_capture(self.root, [FRONTAL, OBLIQUE])
        self.scene = build_scene(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_overlays_are_rendered_with_lineage(self):
        output = self.root / "overlays"
        manifest = rpo.render_element_overlays(
            self.scene,
            self.transforms,
            ["wall_test"],
            output,
            ground_z=0.0,
            top_n=2,
            min_coverage=0.3,
        )
        self.assertEqual(len(manifest["elements"]), 1)
        overlays = manifest["elements"][0]["overlays"]
        self.assertGreaterEqual(len(overlays), 1)
        for overlay in overlays:
            path = output / overlay["output"]
            self.assertTrue(path.is_file())
            self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", overlay["lineageId"]))
            # the overlay must differ from the flat source image (lines drawn)
            rendered = np.asarray(Image.open(path).convert("RGB"))
            self.assertTrue((rendered == [0, 255, 255]).all(axis=2).any())
        self.assertTrue((output / "photo-overlay-manifest.json").is_file())

    def test_openings_are_drawn_in_magenta(self):
        output = self.root / "overlays"
        manifest = rpo.render_element_overlays(
            self.scene,
            self.transforms,
            ["wall_test"],
            output,
            ground_z=0.0,
            top_n=1,
            min_coverage=0.3,
        )
        overlay = manifest["elements"][0]["overlays"][0]
        rendered = np.asarray(Image.open(output / overlay["output"]).convert("RGB"))
        self.assertTrue((rendered == [255, 0, 255]).all(axis=2).any())

    def test_unknown_element_raises(self):
        with self.assertRaises(KeyError):
            rpo.render_element_overlays(
                self.scene,
                self.transforms,
                ["nope"],
                self.root / "overlays",
                ground_z=0.0,
            )


class WallDossierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.transforms = build_capture(self.root, [FRONTAL, OBLIQUE])
        self.scene = build_scene(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_dossier_page_is_composed_with_manifest(self):
        output = self.root / "dossiers" / "wall_test.png"
        manifest = wd.build_dossier(
            self.scene,
            self.transforms,
            "wall_test",
            output,
            ground_z=0.0,
            top_n=2,
            min_coverage=0.3,
        )
        self.assertTrue(output.is_file())
        with Image.open(output) as page:
            self.assertGreater(page.width, 800)
            self.assertGreater(page.height, 600)
        self.assertGreaterEqual(len(manifest["photos"]), 1)
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", manifest["lineageId"]))
        self.assertEqual(manifest["elementId"], "wall_test")
        self.assertIsNone(manifest["section"])  # no section available, no index
        sidecar = output.with_suffix(".json")
        self.assertTrue(sidecar.is_file())
        stored = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(stored["lineageId"], manifest["lineageId"])
        self.assertEqual(stored["planCrop"]["source"], "evidence/band-walls.png")

    def test_dossier_rejects_non_wall_elements(self):
        with self.assertRaises(ValueError):
            wd.build_dossier(
                self.scene,
                self.transforms,
                "win_test",
                self.root / "out.png",
                ground_z=0.0,
            )

    def test_dossier_rejects_unknown_element(self):
        with self.assertRaises(KeyError):
            wd.build_dossier(
                self.scene,
                self.transforms,
                "missing",
                self.root / "out.png",
                ground_z=0.0,
            )


if __name__ == "__main__":
    unittest.main()
