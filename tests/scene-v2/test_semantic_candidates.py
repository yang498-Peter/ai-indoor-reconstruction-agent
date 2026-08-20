"""Tests for the semantic-observation grounding channel (P2-4).

The permission model under test: a semantic observer only supplies pixel boxes
plus labels; every coordinate in the output must come from ray casting against
geometry the scene already owns, stay in candidate status, and keep a low
confidence unless a geometry report corroborates it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "scene-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

RTK_CAPTURE = Path(
    r"C:\baidunetdiskdownload\2026-03-05_10-58-54-rtk-house_2\2026-03-05_10-58-54-rtk-house_2"
)
RTK_SCENE = ROOT / "outputs" / "rtk-house-2" / "scene.json"
SCHEMA_PATH = ROOT / "schemas" / "semantic-observations-v1.schema.json"


def load(name: str, path: Path):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sc = load("semantic_candidates_under_test", CORE / "semantic_candidates.py")
pp = sc.pp


def look_at_c2w(position, target, up=(0.0, 0.0, 1.0)) -> np.ndarray:
    """OpenGL camera-to-world: camera looks along -Z, +Y is up."""
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


def make_frame(position, target, *, width=400, height=400, focal=300.0) -> "pp.Frame":
    camera = pp.CameraModel(
        width=width, height=height, fx=focal, fy=focal, cx=width / 2, cy=height / 2
    )
    return pp.Frame(
        frame_id="fixture#0",
        file_path="left/fixture.jpg",
        c2w=look_at_c2w(position, target),
        camera=camera,
    )


def bbox_of(frame: "pp.Frame", points: np.ndarray) -> list[float]:
    """Axis-aligned pixel hull of projected world points (the observation)."""
    uv, _, in_front = pp.project_points(frame, points)
    assert in_front.all(), "fixture points must be in front of the camera"
    return [
        float(uv[:, 0].min()), float(uv[:, 1].min()),
        float(uv[:, 0].max()), float(uv[:, 1].max()),
    ]


def payload(*observations: dict) -> dict:
    return {
        "schemaVersion": "1.0",
        "captureFingerprint": "fixture-capture",
        "observations": list(observations),
    }


def observation(frame_id: str, bbox: list[float], label: str,
                confidence: float = 0.9, observer: str = "fixture-vlm") -> dict:
    return {
        "frameId": frame_id, "bbox": bbox, "label": label,
        "labelConfidence": confidence, "observer": observer,
    }


# One wall along y = 2 (camera-facing face at y = 1.95) with a known door,
# ground_z = 0 so scene elevation equals LAS z.
GROUND_Z = 0.0
WALL = {
    "id": "wall_fixture", "type": "wall", "start": [0.0, 2.0], "end": [6.0, 2.0],
    "thickness": 0.1, "height": 2.7, "baseHeight": 0.0, "children": [],
}
SCENE = {"nodes": {"wall_fixture": WALL}}
DOOR = {"center": 2.0, "width": 0.9, "sill": 0.0, "head": 2.05}
FACE_Y = 2.0 - WALL["thickness"] / 2.0  # camera at y < 2 sees this face

FRAME = make_frame((3.0, -3.0, 1.3), (3.0, 2.0, 1.3))


def door_face_corners() -> np.ndarray:
    x0 = DOOR["center"] - DOOR["width"] / 2.0
    x1 = DOOR["center"] + DOOR["width"] / 2.0
    return np.array([
        [x0, FACE_Y, DOOR["sill"]], [x1, FACE_Y, DOOR["sill"]],
        [x1, FACE_Y, DOOR["head"]], [x0, FACE_Y, DOOR["head"]],
    ])


class SchemaFileTest(unittest.TestCase):
    def test_schema_file_matches_the_hand_validator_contract(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schemaVersion"]["const"], sc.SCHEMA_VERSION)
        self.assertEqual(
            set(schema["required"]), {"schemaVersion", "captureFingerprint", "observations"}
        )
        item = schema["properties"]["observations"]["items"]
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(
            set(item["required"]), {"frameId", "bbox", "label", "labelConfidence", "observer"}
        )
        bbox = item["properties"]["bbox"]
        self.assertEqual((bbox["minItems"], bbox["maxItems"]), (4, 4))


class PayloadValidationTest(unittest.TestCase):
    def assert_rejected(self, bad_payload, fragment: str):
        with self.assertRaises(sc.ObservationError) as ctx:
            sc.validate_observations_payload(bad_payload)
        self.assertIn("OBSERVATIONS_INVALID", str(ctx.exception))
        self.assertIn(fragment, str(ctx.exception))

    def test_rejects_bad_inputs(self):
        good = observation("fixture#0", [10, 10, 20, 20], "door")
        self.assert_rejected([], "object")
        self.assert_rejected({"schemaVersion": "1.0"}, "captureFingerprint")
        self.assert_rejected(
            {"schemaVersion": "2.0", "captureFingerprint": "x", "observations": [good]},
            "schemaVersion",
        )
        self.assert_rejected(payload(), "non-empty array")
        self.assert_rejected(payload({**good, "bbox": [10, 10, 20]}), "bbox")
        self.assert_rejected(payload({**good, "bbox": [30, 10, 20, 20]}), "x0 < x1")
        self.assert_rejected(payload({**good, "labelConfidence": 1.5}), "labelConfidence")
        self.assert_rejected(payload({**good, "observer": ""}), "observer")
        # A world coordinate smuggled into the observation is an unknown key:
        # the channel only accepts pixel boxes.
        self.assert_rejected(payload({**good, "worldXYZ": [1, 2, 3]}), "unknown keys")
        self.assert_rejected(payload(good, {**good, "frameId": ""}), "observations[1]")

    def test_accepts_and_normalizes_a_good_payload(self):
        rows = sc.validate_observations_payload(
            payload(observation("fixture#0", [10, 10, 20, 20], "door", 0.75))
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "door")
        self.assertAlmostEqual(rows[0]["labelConfidence"], 0.75)


class OpeningGroundingTest(unittest.TestCase):
    def grounded_door(self):
        bbox = bbox_of(FRAME, door_face_corners())
        report = sc.ground_observations(
            SCENE, [FRAME], payload(observation("fixture#0", bbox, "door")),
            ground_z=GROUND_Z,
        )
        self.assertEqual(report["counts"]["unresolved"], 0, report["unresolved"])
        self.assertEqual(len(report["candidates"]), 1)
        return report["candidates"][0]

    def test_door_bbox_lands_on_the_wall_within_15cm(self):
        candidate = self.grounded_door()
        self.assertEqual(candidate["kind"], "opening")
        self.assertEqual(candidate["type"], "door")
        self.assertEqual(candidate["hostWallId"], "wall_fixture")
        self.assertLess(abs(candidate["hostOffsetM"] - DOOR["center"]), 0.15)
        self.assertLess(abs(candidate["widthM"] - DOOR["width"]), 0.15)
        self.assertLess(abs(candidate["sillM"] - DOOR["sill"]), 0.15)
        self.assertLess(abs(candidate["headM"] - DOOR["head"]), 0.15)

    def test_semantic_only_candidate_keeps_the_limited_permissions(self):
        candidate = self.grounded_door()
        self.assertEqual(candidate["status"], "candidate")
        self.assertEqual(candidate["coordinateSource"], "ray-cast-estimate")
        self.assertTrue(candidate["requiresGeometryConfirmation"])
        self.assertEqual(candidate["corroboration"], "semantic-only")
        self.assertLessEqual(candidate["confidence"], sc.SEMANTIC_ONLY_CONFIDENCE_CAP)
        self.assertEqual(candidate["observation"]["observer"], "fixture-vlm")

    def test_opening_bbox_off_every_wall_is_unresolved(self):
        # A "window" box in the sky above the wall: no wall face hit.
        report = sc.ground_observations(
            SCENE, [FRAME],
            payload(observation("fixture#0", [180.0, 5.0, 220.0, 30.0], "window")),
            ground_z=GROUND_Z,
        )
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["unresolved"][0]["reason"], "NO_WALL_HIT")

    def test_unknown_frame_is_unresolved_not_fatal(self):
        report = sc.ground_observations(
            SCENE, [FRAME],
            payload(observation("nope#9", [10, 10, 20, 20], "door")),
            ground_z=GROUND_Z,
        )
        self.assertEqual(report["unresolved"][0]["reason"], "FRAME_NOT_FOUND")


class ItemGroundingTest(unittest.TestCase):
    def test_furniture_bbox_grounds_on_the_floor_within_20cm(self):
        # A chair-sized box centred at plan (4.0, 0.5); its visual bbox bottom
        # is the front floor contact at y = 0.35.
        corners = np.array([
            [x, y, z]
            for x in (3.7, 4.3) for y in (0.35, 0.65) for z in (0.0, 0.85)
        ])
        bbox = bbox_of(FRAME, corners)
        report = sc.ground_observations(
            SCENE, [FRAME], payload(observation("fixture#0", bbox, "chair", 0.8)),
            ground_z=GROUND_Z,
        )
        self.assertEqual(report["counts"]["unresolved"], 0, report["unresolved"])
        candidate = report["candidates"][0]
        self.assertEqual(candidate["kind"], "item")
        error = float(np.hypot(candidate["planPosition"][0] - 4.0,
                               candidate["planPosition"][1] - 0.5))
        self.assertLess(error, 0.20)
        self.assertTrue(candidate["requiresGeometryConfirmation"])
        self.assertLessEqual(candidate["confidence"], sc.SEMANTIC_ONLY_CONFIDENCE_CAP)
        # Coarse size hints stay in the right ballpark (bbox spans 0.6 x 0.85).
        self.assertLess(abs(candidate["sizeEstimateM"]["height"] - 0.85), 0.3)

    def test_material_label_on_the_wall_becomes_a_wall_surface_candidate(self):
        patch = np.array([
            [2.8, FACE_Y, 1.0], [3.2, FACE_Y, 1.0], [3.2, FACE_Y, 1.6], [2.8, FACE_Y, 1.6],
        ])
        bbox = bbox_of(FRAME, patch)
        report = sc.ground_observations(
            SCENE, [FRAME], payload(observation("fixture#0", bbox, "material", 0.7)),
            ground_z=GROUND_Z,
        )
        candidate = report["candidates"][0]
        self.assertEqual(candidate["kind"], "wall-surface")
        self.assertEqual(candidate["hostWallId"], "wall_fixture")
        self.assertLess(abs(candidate["hostOffsetM"] - 3.0), 0.15)


class CorroborationTest(unittest.TestCase):
    def geometry_report(self, host_offset: float) -> dict:
        return {
            "lineageId": "lineage-fixture",
            "parametersFingerprint": "params-fixture",
            "candidates": [{
                "type": "door", "status": "candidate",
                "hostOffsetM": host_offset, "widthM": 0.88,
                "sillM": 0.0, "headM": 2.02, "confidence": 0.62,
            }],
        }

    def run_with_report(self, report: dict) -> dict:
        bbox = bbox_of(FRAME, door_face_corners())
        result = sc.ground_observations(
            SCENE, [FRAME], payload(observation("fixture#0", bbox, "door")),
            ground_z=GROUND_Z,
            geometry_reports={"wall_fixture": report},
        )
        return result

    def test_matching_geometry_candidate_merges_and_boosts_confidence(self):
        result = self.run_with_report(self.geometry_report(2.07))
        self.assertEqual(result["counts"]["corroborated"], 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["corroboration"], "geometry+semantic")
        # Geometry dimensions win over the ray-cast estimate.
        self.assertEqual(candidate["hostOffsetM"], 2.07)
        self.assertEqual(candidate["widthM"], 0.88)
        self.assertFalse(candidate["requiresGeometryConfirmation"])
        self.assertGreater(candidate["confidence"], sc.SEMANTIC_ONLY_CONFIDENCE_CAP)
        self.assertLess(candidate["geometry"]["hostOffsetDeltaM"], sc.DEFAULT_MERGE_TOLERANCE_M)
        self.assertEqual(candidate["geometry"]["reportLineageId"], "lineage-fixture")
        # The semantic pixel observation stays attached for audit.
        self.assertEqual(candidate["observation"]["label"], "door")

    def test_distant_geometry_candidate_does_not_merge(self):
        result = self.run_with_report(self.geometry_report(4.5))
        self.assertEqual(result["counts"]["corroborated"], 0)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["corroboration"], "semantic-only")
        self.assertTrue(candidate["requiresGeometryConfirmation"])
        self.assertLessEqual(candidate["confidence"], sc.SEMANTIC_ONLY_CONFIDENCE_CAP)


@unittest.skipUnless(
    RTK_CAPTURE.is_dir() and RTK_SCENE.is_file(),
    "real RTK capture / scene not present on this machine",
)
class RealCaptureRoundTripTest(unittest.TestCase):
    """Project a known accepted window into a real frame, hand the resulting
    pixel box to the channel as a 'perfect observation', and require the ray
    cast to land back on the window (round trip < 0.2 m)."""

    GROUND_Z = -0.5
    WALL_ID = "wall_cabin_north"
    WINDOW_ID = "window_north_0"

    def test_window_observation_round_trips_within_20cm(self):
        scene = json.loads(RTK_SCENE.read_text(encoding="utf-8"))
        wall = scene["nodes"][self.WALL_ID]
        window = scene["nodes"][self.WINDOW_ID]

        start = np.asarray(wall["start"], dtype=np.float64)
        end = np.asarray(wall["end"], dtype=np.float64)
        unit = (end - start) / np.linalg.norm(end - start)
        normal = np.array([-unit[1], unit[0], 0.0])

        # World rect of the window on the wall centerline, using the scene_api
        # convention (hostOffsetM = distance from wall start to opening CENTER;
        # photo_projection.wall_wireframe reads it as the left edge instead, so
        # it cannot serve as the reference here).
        along0 = float(window["hostOffsetM"]) - float(window["width"]) / 2.0
        along1 = float(window["hostOffsetM"]) + float(window["width"]) / 2.0
        sill_z = float(window["sillHeight"]) + self.GROUND_Z
        head_z = sill_z + float(window["height"])
        a = start + unit * along0
        b = start + unit * along1
        rect = np.array([
            [a[0], a[1], sill_z], [b[0], b[1], sill_z],
            [b[0], b[1], head_z], [a[0], a[1], head_z],
        ])
        rect_center = rect.mean(axis=0)

        frames = pp.load_frames(RTK_CAPTURE / "transforms.json")
        planes = sc.wall_planes(scene, self.GROUND_Z)
        best = None
        for frame in frames:
            uv, depth, in_front = pp.project_points(frame, rect)
            if not in_front.all() or not pp.in_image(frame, uv, margin=-40).all():
                continue
            if not 1.5 <= float(depth.mean()) <= 12.0:
                continue
            view = rect_center - frame.center
            view = view / np.linalg.norm(view)
            # Unoccluded view only: the first wall the sight line meets must be
            # the host wall itself, not e.g. the opposite side of the cabin.
            first_hit = sc._host_wall_hit(frame.center, view, planes)
            if first_hit is None or first_hit[3]["id"] != self.WALL_ID:
                continue
            frontal = abs(float(np.dot(view, normal)))
            if best is None or frontal > best[0]:
                best = (frontal, frame, uv)
        self.assertIsNotNone(best, "no frame sees the whole window frontally")
        frontal, frame, uv = best
        self.assertGreater(frontal, 0.6)

        bbox = [
            float(uv[:, 0].min()), float(uv[:, 1].min()),
            float(uv[:, 0].max()), float(uv[:, 1].max()),
        ]
        report = sc.ground_observations(
            scene, frames,
            payload(observation(frame.frame_id, bbox, "window", 0.9, "round-trip-test")),
            ground_z=self.GROUND_Z,
        )
        self.assertEqual(report["counts"]["unresolved"], 0, report["unresolved"])
        candidate = report["candidates"][0]
        self.assertEqual(candidate["hostWallId"], self.WALL_ID)
        errors = {
            "hostOffsetM": abs(candidate["hostOffsetM"] - float(window["hostOffsetM"])),
            "widthM": abs(candidate["widthM"] - float(window["width"])),
            "sillM": abs(candidate["sillM"] - float(window["sillHeight"])),
            "headM": abs(candidate["headM"] - (float(window["sillHeight"]) + float(window["height"]))),
        }
        print(f"round-trip frame={frame.frame_id} frontal={frontal:.3f} errors={errors}")
        for name, value in errors.items():
            self.assertLess(value, 0.2, f"{name} round-trip error {value:.3f} m")


if __name__ == "__main__":
    unittest.main()
