from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "scene-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

RTK_CAPTURE = Path(
    r"C:\baidunetdiskdownload\2026-03-05_10-58-54-rtk-house_2\2026-03-05_10-58-54-rtk-house_2"
)


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pp = load("photo_projection_under_test", CORE / "photo_projection.py")


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


def make_frame(position, target, *, width=400, height=400, focal=200.0) -> "pp.Frame":
    camera = pp.CameraModel(
        width=width, height=height, fx=focal, fy=focal, cx=width / 2, cy=height / 2
    )
    return pp.Frame(
        frame_id="test#0",
        file_path="test.png",
        c2w=look_at_c2w(position, target),
        camera=camera,
    )


class PinholeProjectionTests(unittest.TestCase):
    def test_point_on_optical_axis_hits_principal_point(self):
        frame = make_frame((0, 0, 0), (0, 5, 0))
        uv, depth, in_front = pp.project_points(frame, np.array([[0.0, 3.0, 0.0]]))
        self.assertTrue(in_front[0])
        self.assertAlmostEqual(depth[0], 3.0, places=9)
        self.assertAlmostEqual(uv[0, 0], 200.0, places=6)
        self.assertAlmostEqual(uv[0, 1], 200.0, places=6)

    def test_known_box_corner_round_trip(self):
        # Camera at origin looking +y; point 1 m right, 1 m up at 2 m depth:
        # normalized (0.5, -0.5) -> pixel (cx + fx*0.5, cy - fy*0.5).
        frame = make_frame((0, 0, 0), (0, 5, 0))
        uv, depth, in_front = pp.project_points(
            frame, np.array([[1.0, 2.0, 1.0], [-1.0, 2.0, -1.0]])
        )
        self.assertTrue(in_front.all())
        np.testing.assert_allclose(uv[0], [300.0, 100.0], atol=1e-6)
        np.testing.assert_allclose(uv[1], [100.0, 300.0], atol=1e-6)

    def test_points_behind_camera_are_flagged(self):
        frame = make_frame((0, 0, 0), (0, 5, 0))
        uv, depth, in_front = pp.project_points(frame, np.array([[0.0, -3.0, 0.0]]))
        self.assertFalse(in_front[0])
        self.assertTrue(np.isnan(uv[0]).all())

    def test_synthetic_box_round_trip_through_arbitrary_pose(self):
        # Project the 8 corners of a box, then invert the pinhole equations
        # per point and confirm the reconstructed rays hit the corners.
        frame = make_frame((4.0, -3.0, 2.0), (0.5, 1.0, 0.5))
        corners = np.array(
            [[x, y, z] for x in (0, 1) for y in (0, 1) for z in (0, 1)], dtype=np.float64
        )
        uv, depth, in_front = pp.project_points(frame, corners)
        self.assertTrue(in_front.all())
        camera = frame.camera
        for pixel, d, corner in zip(uv, depth, corners):
            xn = (pixel[0] - camera.cx) / camera.fx
            yn = (pixel[1] - camera.cy) / camera.fy
            cam_point = np.array([xn * d, yn * d, d])
            # OpenCV -> OpenGL -> world
            gl = cam_point * np.array([1.0, -1.0, -1.0])
            world = frame.c2w[:3, :3] @ gl + frame.c2w[:3, 3]
            np.testing.assert_allclose(world, corner, atol=1e-9)

    def test_in_image_mask(self):
        frame = make_frame((0, 0, 0), (0, 5, 0))
        uv = np.array([[10.0, 10.0], [399.0, 399.0], [-1.0, 50.0], [50.0, 401.0]])
        mask = pp.in_image(frame, uv)
        self.assertEqual(mask.tolist(), [True, True, False, False])


class PolylineClippingTests(unittest.TestCase):
    def test_polyline_fully_in_front_is_single_run(self):
        frame = make_frame((0, 0, 0), (0, 5, 0))
        runs = pp.project_polyline(frame, [[0, 2, 0], [1, 2, 0], [1, 2, 1]])
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(runs[0]), 3)

    def test_segment_crossing_near_plane_is_cut(self):
        frame = make_frame((0, 0, 0), (0, 5, 0))
        runs = pp.project_polyline(frame, [[0, 2, 0], [0, -2, 0]])
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(runs[0]), 2)
        # the cut vertex must be finite (projected at the near plane)
        self.assertTrue(all(math.isfinite(v) for point in runs[0] for v in point))

    def test_fully_behind_polyline_yields_nothing(self):
        frame = make_frame((0, 0, 0), (0, 5, 0))
        self.assertEqual(pp.project_polyline(frame, [[0, -2, 0], [1, -2, 0]]), [])

    def test_closed_loop_wraps(self):
        frame = make_frame((0, 0, 0), (0, 5, 0))
        runs = pp.project_polyline(
            frame, [[-1, 2, -1], [1, 2, -1], [1, 2, 1], [-1, 2, 1]], closed=True
        )
        total_vertices = sum(len(run) for run in runs)
        self.assertGreaterEqual(total_vertices, 5)


class EquirectTests(unittest.TestCase):
    def test_forward_up_and_side_directions(self):
        pose = np.eye(4)  # pano frame == world: x right, y down, z forward
        width, height = 512, 256
        uv, distance, valid = pp.project_points_equirect(
            pose,
            np.array(
                [
                    [0.0, 0.0, 5.0],  # forward -> centre
                    [5.0, 0.0, 0.0],  # right -> u = 3/4 width
                    [0.0, -5.0, 0.0],  # up (y down axis) -> v = 0
                ]
            ),
            width=width,
            height=height,
        )
        self.assertTrue(valid.all())
        np.testing.assert_allclose(uv[0], [width / 2, height / 2], atol=1e-6)
        self.assertAlmostEqual(uv[1][0], width * 0.75, places=6)
        self.assertAlmostEqual(uv[2][1], 0.0, places=6)
        np.testing.assert_allclose(distance, [5.0, 5.0, 5.0])

    def test_points_at_centre_are_invalid(self):
        pose = np.eye(4)
        uv, distance, valid = pp.project_points_equirect(pose, np.array([[0.0, 0.0, 0.05]]))
        self.assertFalse(valid[0])
        self.assertTrue(np.isnan(uv[0]).all())


class PoseTrackTests(unittest.TestCase):
    def test_midpoint_interpolation(self):
        track = pp.PoseTrack(
            timestamps=np.array([0.0, 1.0]),
            positions=np.array([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0]]),
            quaternions=np.array([[0.0, 0.0, 0.0, 1.0], [0.0, 0.0, 0.0, 1.0]]),
        )
        pose = track.pose_at(0.5)
        np.testing.assert_allclose(pose[:3, 3], [1.0, 0.0, 0.0])
        np.testing.assert_allclose(pose[:3, :3], np.eye(3), atol=1e-12)

    def test_covers_gates_extrapolation(self):
        track = pp.PoseTrack(
            timestamps=np.array([10.0, 11.0]),
            positions=np.zeros((2, 3)),
            quaternions=np.array([[0.0, 0.0, 0.0, 1.0]] * 2),
        )
        self.assertTrue(track.covers(10.5))
        self.assertFalse(track.covers(20.0))

    def test_load_imgpose_parses_and_sorts(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ImgPose.txt"
            path.write_text(
                "index x y z roll pitch yaw qx qy qz qw timestamp\n"
                "left/b.jpg 1 0 0 0 0 0 0 0 0 1 2.0\n"
                "right/x.jpg 9 9 9 0 0 0 0 0 0 1 1.5\n"
                "left/a.jpg 0 0 0 0 0 0 0 0 0 1 1.0\n",
                encoding="utf-8",
            )
            track = pp.load_imgpose(path)
            self.assertEqual(len(track.timestamps), 2)
            self.assertEqual(track.timestamps.tolist(), [1.0, 2.0])
            np.testing.assert_allclose(track.pose_at(1.5)[:3, 3], [0.5, 0.0, 0.0])


class WallWireframeTests(unittest.TestCase):
    def test_outline_and_opening_rects(self):
        wall = {
            "start": [0.0, 0.0],
            "end": [4.0, 0.0],
            "height": 3.0,
            "baseHeight": 0.5,
        }
        child = {
            "id": "win",
            "type": "window",
            "hostOffsetM": 1.0,
            "width": 1.0,
            "sillHeight": 1.0,
            "height": 1.2,
        }
        wire = pp.wall_wireframe(wall, -0.5, [child])
        self.assertAlmostEqual(wire["lengthM"], 4.0)
        # base at 0.5 elevation + ground_z(-0.5) = LAS z 0.0; top LAS z 3.0
        self.assertEqual(wire["outline"][0], [0.0, 0.0, 0.0])
        self.assertEqual(wire["outline"][2], [4.0, 0.0, 3.0])
        rect = wire["openings"][0]["rect"]
        # hostOffsetM=1.0 is the opening CENTER (scene_api convention):
        # width 1.0 spans along-wall 0.5..1.5. Sill 1.0 + ground_z(-0.5).
        self.assertEqual(rect[0], [0.5, 0.0, 0.5])
        self.assertEqual(rect[2], [1.5, 0.0, 1.7])


class LoadFramesTests(unittest.TestCase):
    def test_loads_undistorted_frames_with_shared_camera(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "undistort" / "left").mkdir(parents=True)
            from PIL import Image

            Image.new("RGB", (8, 8)).save(root / "undistort" / "left" / "1.jpg")
            value = {
                "undistort_camera_model": {
                    "width": 8,
                    "height": 8,
                    "intrinsic": [[4, 0, 4], [0, 4, 4], [0, 0, 1]],
                },
                "frames": [
                    {
                        "file_path": "left\\1.jpg",
                        "timestamp": 123,
                        "fl_x": 999.0,
                        "fl_y": 999.0,
                        "cx": 1.0,
                        "cy": 1.0,
                        "w": 100,
                        "h": 100,
                        "k1": 0.1,
                        "transform_matrix": np.eye(4).tolist(),
                    }
                ],
            }
            (root / "transforms.json").write_text(json.dumps(value), encoding="utf-8")
            frames = pp.load_frames(root / "transforms.json")
            self.assertEqual(len(frames), 1)
            frame = frames[0]
            self.assertIsNotNone(frame.image_path)
            # undistorted image found -> shared pinhole model, not per-frame fisheye
            self.assertEqual(frame.camera.model, "pinhole")
            self.assertEqual(frame.camera.fx, 4.0)
            self.assertEqual(frame.timestamp, 123)


@unittest.skipUnless(RTK_CAPTURE.is_dir(), "real RTK capture not present on this machine")
class RealCaptureCoordinateConventionTests(unittest.TestCase):
    """Regression for the verified conventions: LAS-frame c2w, OpenGL camera."""

    def test_las_sample_projects_into_frames(self):
        import laspy

        frames = pp.load_frames(RTK_CAPTURE / "transforms.json")
        self.assertEqual(len(frames), 156)
        with laspy.open(RTK_CAPTURE / "2026-03-05_10-58-54-RTK-HOUSE_colorized.las") as reader:
            chunk = next(reader.chunk_iterator(2_000_000))[::40]
            pts = np.stack(
                [np.asarray(chunk.x), np.asarray(chunk.y), np.asarray(chunk.z)], axis=1
            )
        checked = 0
        correct_fractions = []
        flipped_fractions = []
        for frame in frames[:: len(frames) // 5]:
            distance = np.linalg.norm(pts - frame.center, axis=1)
            near = pts[distance < 30.0]
            if len(near) < 1000:
                continue
            uv, depth, in_front = pp.project_points(frame, near)
            correct_fractions.append(
                float((in_front & pp.in_image(frame, uv)).sum()) / len(near)
            )
            # same pose interpreted with the WRONG (OpenCV) camera convention:
            # skip the OpenGL->OpenCV axis flip by pre-flipping the rotation
            wrong = pp.Frame(
                frame_id=frame.frame_id + "#wrong",
                file_path=frame.file_path,
                c2w=frame.c2w @ np.diag([1.0, -1.0, -1.0, 1.0]),
                camera=frame.camera,
            )
            uv_w, _, in_front_w = pp.project_points(wrong, near)
            flipped_fractions.append(
                float((in_front_w & pp.in_image(wrong, uv_w)).sum()) / len(near)
            )
            checked += 1
        self.assertGreaterEqual(checked, 4)
        mean_correct = float(np.mean(correct_fractions))
        mean_flipped = float(np.mean(flipped_fractions))
        # the verified OpenGL convention must show far more of the nearby
        # cloud than the flipped interpretation (which looks backwards)
        self.assertGreater(mean_correct, 0.05)
        self.assertGreater(mean_correct, 1.5 * mean_flipped)

    def test_imgpose_matches_transforms_convention(self):
        track = pp.load_imgpose(RTK_CAPTURE / "ImgPose.txt")
        frames = pp.load_frames(RTK_CAPTURE / "transforms.json")
        frame = frames[0]
        c2w_cv = track.pose_at(frame.timestamp / 1e9)
        # ImgPose is OpenCV c2w; flipping y/z columns must reproduce the
        # transforms.json OpenGL rotation
        flipped = c2w_cv[:3, :3] @ np.diag([1.0, -1.0, -1.0])
        self.assertLess(float(np.abs(flipped - frame.c2w[:3, :3]).max()), 5e-3)
        self.assertLess(float(np.linalg.norm(c2w_cv[:3, 3] - frame.center)), 0.02)


if __name__ == "__main__":
    unittest.main()
