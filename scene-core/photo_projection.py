#!/usr/bin/env python3
"""Shared projection math for posed capture photos and panoramas.

Coordinate conventions (verified numerically against the RTK-house capture
``2026-03-05_10-58-54-rtk-house_2`` on 2026-08-20; see the evidence numbers
below rather than re-deriving from documentation):

* **World frame** is the LAS local frame: the capture pipeline converts every
  product (colorized LAS, transforms.json, ImgPose.txt, odom) from ECEF into
  one shared local ENU metre frame (``coordinate_context.json``:
  ``outputBasis: local-with-geo-info``).  ``transforms.json`` frame
  ``transform_matrix`` values are therefore camera-to-world matrices **in LAS
  coordinates** - no unit change, no axis flip.
* **Scene V2 plan coordinates equal LAS x/y exactly.**  Corridor occupancy of
  ``outputs/rtk-house-2/scene.json`` walls against the raw LAS: identity
  mapping puts 42k/46k/20k points inside a 0.15 m corridor of the north/south/
  east cabin walls while shifting by +-(5,5) drops to ~0-4k.  The scene's
  ``meta.displayOffset`` (and the ``display = [x, elevation, -y]`` transform)
  are viewer-display recentering only and must NOT be applied when projecting.
* **Scene elevation zero sits at LAS z = ground_z** (-0.50 m for rtk-house-2,
  the same ``--ground-z`` that built ``cloud.lrpc``): ``las_z = elevation +
  ground_z``.
* **transforms.json cameras use the OpenGL/NeRF convention** (camera looks
  along -Z, +Y up, camera-to-world).  Projecting a LAS sample into undistorted
  frames and comparing block-median colors gives gray correlation 0.44-0.57
  for OpenGL versus ~0.1 for OpenCV across left and right frames, so the
  adapter id ``transforms-json-c2w-opengl-v1`` is confirmed by data.
* ``frames[].file_path`` (``left\\<ts>.jpg``) resolves under ``undistort/`` and
  those images match the top-level ``undistort_camera_model`` (pinhole
  fx=fy=800, cx=cy=800, 1600x1600, 90 deg fov, **zero distortion**).  The
  per-frame ``fl_x/fl_y/cx/cy/k1..k4`` (2912x2912) describe the original
  fisheye images, which are not shipped; when only per-frame intrinsics are
  available we model k1..k4 as the equidistant fisheye theta-polynomial.
* **ImgPose.txt** rows are camera-to-world poses in the same world frame but
  with the **OpenCV** camera convention (+Z forward, +Y down): converting its
  quaternion and right-multiplying ``diag(1,-1,-1)`` reproduces the
  transforms.json rotation to 3.4e-4 and the translation to a few millimetres.
  It is sampled every 0.5 s (~880 rows) versus the ~2 s keyframes in
  transforms.json, so panorama poses interpolate ImgPose, not transforms.
* **Panoramas** (``panorama/<ts>.jpg``, 4096x2048 equirect): the pano centre
  **position** interpolated from ImgPose is reliable, but the pano
  **orientation** is not.  Fitting the camera->pano rotation per panorama by
  maximising LAS->pano color correlation reaches corr ~0.45 on individual
  panoramas, yet the fitted rotation varies by 5-12 degrees between panoramas
  of the same capture and no constant time offset (scanned +-3 s) explains
  the drift - the stitcher appears to bake a per-pano orientation that is not
  recorded in any shipped file.  ``pano_pose_at`` therefore uses ImgPose
  position + nominal ``PANO_FROM_CAMERA`` rotation and pano projections must
  be treated as approximate review visuals only, never as acceptance
  evidence.  Wall dossiers use the pinhole undistorted photos exclusively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import Sequence

import numpy as np


_OPENGL_TO_OPENCV = np.diag([1.0, -1.0, -1.0])


@dataclass(frozen=True)
class CameraModel:
    """Intrinsics for one image; ``model`` selects the distortion math."""

    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    model: str = "pinhole"  # "pinhole" (k ignored/zero) | "fisheye-equidistant"
    k: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def is_valid(self) -> bool:
        numbers = (self.fx, self.fy, self.cx, self.cy)
        return (
            self.width > 0
            and self.height > 0
            and all(math.isfinite(value) for value in numbers)
            and self.fx > 0
            and self.fy > 0
        )


@dataclass(frozen=True)
class Frame:
    """One posed photo: camera-to-world pose (OpenGL convention) + intrinsics."""

    frame_id: str
    file_path: str
    c2w: np.ndarray  # (4, 4) float64, camera-to-world, OpenGL camera axes
    camera: CameraModel
    timestamp: int | None = None
    image_path: Path | None = None
    source_path: str | None = None

    @property
    def center(self) -> np.ndarray:
        return self.c2w[:3, 3]

    def world_to_camera(self, xyz: np.ndarray) -> np.ndarray:
        """World points -> OpenCV camera coordinates (x right, y down, z forward)."""
        pts = np.asarray(xyz, dtype=np.float64)
        single = pts.ndim == 1
        pts = np.atleast_2d(pts)
        rotation = self.c2w[:3, :3]
        # OpenGL camera coords, then flip to OpenCV (y down, z forward).
        cam_gl = (pts - self.c2w[:3, 3]) @ rotation
        cam_cv = cam_gl @ _OPENGL_TO_OPENCV
        return cam_cv[0] if single else cam_cv


def _distort_uv(camera: CameraModel, x: np.ndarray, y: np.ndarray, z: np.ndarray):
    """Normalized camera dirs -> pixel coordinates for the camera's model."""
    with np.errstate(divide="ignore", invalid="ignore"):
        if camera.model == "fisheye-equidistant":
            r = np.hypot(x, y)
            theta = np.arctan2(r, z)
            k1, k2, k3, k4 = camera.k
            theta2 = theta * theta
            theta_d = theta * (1 + theta2 * (k1 + theta2 * (k2 + theta2 * (k3 + theta2 * k4))))
            scale = np.where(r > 1e-12, theta_d / np.where(r > 1e-12, r, 1.0), 0.0)
            u = camera.fx * x * scale + camera.cx
            v = camera.fy * y * scale + camera.cy
        else:
            xn = x / z
            yn = y / z
            u = camera.fx * xn + camera.cx
            v = camera.fy * yn + camera.cy
    return u, v


def project_points(frame: Frame, xyz: np.ndarray, *, near: float = 0.05):
    """Project world points into the frame.

    Returns ``(uv, depth, in_front)``:
      * ``uv``: (N, 2) float64 pixel coordinates (finite only where in_front),
      * ``depth``: (N,) metres along the optical axis (OpenCV z),
      * ``in_front``: (N,) bool, depth > near.

    ``in_front`` does NOT include the image-rectangle test; combine with
    :func:`in_image` when needed so callers can draw geometry that extends
    past the frame edges.
    """
    cam = frame.world_to_camera(xyz)
    cam = np.atleast_2d(cam)
    depth = cam[:, 2].copy()
    in_front = depth > near
    u, v = _distort_uv(frame.camera, cam[:, 0], cam[:, 1], np.where(in_front, depth, np.nan))
    uv = np.stack([u, v], axis=1)
    uv[~in_front] = np.nan
    return uv, depth, in_front


def in_image(frame: Frame, uv: np.ndarray, margin: float = 0.0) -> np.ndarray:
    uv = np.atleast_2d(np.asarray(uv, dtype=np.float64))
    return (
        np.isfinite(uv).all(axis=1)
        & (uv[:, 0] >= -margin)
        & (uv[:, 0] <= frame.camera.width - 1 + margin)
        & (uv[:, 1] >= -margin)
        & (uv[:, 1] <= frame.camera.height - 1 + margin)
    )


def project_polyline(
    frame: Frame,
    points: Sequence[Sequence[float]],
    *,
    near: float = 0.05,
    closed: bool = False,
) -> list[list[tuple[float, float]]]:
    """3D polyline -> list of 2D pixel polylines, clipped at the near plane.

    Each returned polyline is a run of consecutive vertices in front of the
    camera; segments crossing the near plane are cut at the intersection so
    wireframes never wrap around behind the camera.  Pixel coordinates may lie
    outside the image rectangle (PIL clips at draw time).
    """
    pts = np.asarray(points, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] < 2 or pts.shape[1] != 3:
        raise ValueError("project_polyline expects an (N>=2, 3) point list")
    if closed:
        pts = np.vstack([pts, pts[:1]])
    cam = frame.world_to_camera(pts)
    result: list[list[tuple[float, float]]] = []
    run: list[tuple[float, float]] = []

    def pixel(cam_point: np.ndarray) -> tuple[float, float]:
        u, v = _distort_uv(
            frame.camera,
            np.asarray([cam_point[0]]),
            np.asarray([cam_point[1]]),
            np.asarray([cam_point[2]]),
        )
        return float(u[0]), float(v[0])

    for index in range(len(cam) - 1):
        a, b = cam[index], cam[index + 1]
        a_in, b_in = a[2] > near, b[2] > near
        if not a_in and not b_in:
            if run:
                result.append(run)
                run = []
            continue
        if a_in and b_in:
            if not run:
                run = [pixel(a)]
            run.append(pixel(b))
            continue
        # crosses the near plane: cut at z == near
        t = (near - a[2]) / (b[2] - a[2])
        cut = a + t * (b - a)
        cut[2] = near
        if a_in:
            if not run:
                run = [pixel(a)]
            run.append(pixel(cut))
            result.append(run)
            run = []
        else:
            run = [pixel(cut), pixel(b)]
    if run:
        result.append(run)
    return [polyline for polyline in result if len(polyline) >= 2]


def _camera_from_value(value: dict, *, model: str, k_keys=("k1", "k2", "k3", "k4")) -> CameraModel:
    intrinsic = value.get("intrinsic")
    if isinstance(intrinsic, list):
        matrix = np.asarray(intrinsic, dtype=np.float64)
        return CameraModel(
            width=int(value["width"]),
            height=int(value["height"]),
            fx=float(matrix[0, 0]),
            fy=float(matrix[1, 1]),
            cx=float(matrix[0, 2]),
            cy=float(matrix[1, 2]),
            model=model,
        )
    return CameraModel(
        width=int(value.get("width") or value.get("w")),
        height=int(value.get("height") or value.get("h")),
        fx=float(value["fl_x"]),
        fy=float(value["fl_y"]),
        cx=float(value["cx"]),
        cy=float(value["cy"]),
        model=model,
        k=tuple(float(value.get(key, 0.0)) for key in k_keys),
    )


def load_frames(transforms_path: Path, *, image_root: Path | None = None) -> list[Frame]:
    """Load ``transforms.json`` into :class:`Frame` objects.

    Image resolution prefers the undistorted copies (``<root>/undistort/<rel>``
    then ``<root>/<rel>``).  When the resolved image lives under ``undistort/``
    (or no per-frame intrinsics exist) the shared ``undistort_camera_model``
    pinhole is used with zero distortion; otherwise the per-frame fisheye
    intrinsics apply.  Missing images leave ``image_path`` as ``None`` so
    validation callers can decide how to treat unbound frames.
    """
    transforms_path = Path(transforms_path)
    value = json.loads(transforms_path.read_text(encoding="utf-8"))
    root = Path(image_root) if image_root is not None else transforms_path.parent
    undistort_model = value.get("undistort_camera_model")
    undistort_camera = (
        _camera_from_value(undistort_model, model="pinhole")
        if isinstance(undistort_model, dict)
        else None
    )
    frames: list[Frame] = []
    for index, item in enumerate(value.get("frames", [])):
        matrix = np.asarray(item["transform_matrix"], dtype=np.float64)
        if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
            raise ValueError(f"frame {index} transform_matrix is not a finite 4x4")
        relative = str(item["file_path"]).replace("\\", "/").strip()
        image_path = None
        used_undistorted = False
        for candidate, is_undistorted in (
            (root / "undistort" / relative, True),
            (root / relative, False),
        ):
            if candidate.is_file():
                image_path = candidate
                used_undistorted = is_undistorted
                break
        if (used_undistorted or image_path is None) and undistort_camera is not None:
            camera = undistort_camera
        elif "fl_x" in item:
            camera = _camera_from_value(item, model="fisheye-equidistant")
        elif undistort_camera is not None:
            camera = undistort_camera
        else:
            raise ValueError(f"frame {index} has no usable camera model")
        timestamp = item.get("timestamp")
        frames.append(
            Frame(
                frame_id=f"{transforms_path.name}#{index}",
                file_path=relative,
                c2w=matrix,
                camera=camera,
                timestamp=int(timestamp) if timestamp is not None else None,
                image_path=image_path,
                source_path=str(transforms_path),
            )
        )
    return frames


# --------------------------------------------------------------------------
# Panorama support (equirectangular projections posed from ImgPose.txt)
# --------------------------------------------------------------------------

# Nominal rotation from the ImgPose camera frame (OpenCV axes) to the panorama
# frame (pano x right, y down, z toward the centre column).  This is the best
# axis-aligned candidate on the RTK-house capture, but per-pano SO(3) fits
# showed the true stitched orientation drifts 5-12 degrees between panoramas
# (see module docstring), so equirect projections are approximate review
# visuals only.
PANO_FROM_CAMERA = np.array(
    [
        [0.0, 0.0, -1.0],
        [-1.0, 0.0, 0.0],
        [0.0, -1.0, 0.0],
    ]
)


@dataclass
class PoseTrack:
    """Dense camera trajectory from ImgPose.txt (c2w, OpenCV camera axes)."""

    timestamps: np.ndarray  # (N,) float64 seconds
    positions: np.ndarray  # (N, 3)
    quaternions: np.ndarray  # (N, 4) xyzw
    source_path: str | None = None

    @property
    def t_min(self) -> float:
        return float(self.timestamps[0])

    @property
    def t_max(self) -> float:
        return float(self.timestamps[-1])

    def covers(self, t: float, *, tolerance: float = 1.0) -> bool:
        return self.t_min - tolerance <= t <= self.t_max + tolerance

    def pose_at(self, t: float) -> np.ndarray:
        """Interpolated c2w (OpenCV camera convention) at time ``t`` seconds.

        Position lerp + quaternion nlerp between the two bracketing samples
        (0.5 s spacing makes nlerp vs slerp differences negligible).  Clamps
        outside the covered span; call :meth:`covers` first to gate that.
        """
        ts = self.timestamps
        index = int(np.clip(np.searchsorted(ts, t), 1, len(ts) - 1))
        t0, t1 = ts[index - 1], ts[index]
        alpha = 0.0 if t1 == t0 else float(np.clip((t - t0) / (t1 - t0), 0.0, 1.0))
        position = self.positions[index - 1] * (1 - alpha) + self.positions[index] * alpha
        q0 = self.quaternions[index - 1].copy()
        q1 = self.quaternions[index].copy()
        if float(np.dot(q0, q1)) < 0:
            q1 = -q1
        quaternion = q0 * (1 - alpha) + q1 * alpha
        c2w = np.eye(4)
        c2w[:3, :3] = quaternion_to_matrix(quaternion)
        c2w[:3, 3] = position
        return c2w


def quaternion_to_matrix(q: Sequence[float]) -> np.ndarray:
    """xyzw quaternion -> rotation matrix."""
    quaternion = np.asarray(q, dtype=np.float64)
    quaternion = quaternion / np.linalg.norm(quaternion)
    x, y, z, w = quaternion
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def load_imgpose(path: Path, *, prefix: str = "left/") -> PoseTrack:
    """Parse ImgPose.txt (``index x y z roll pitch yaw qx qy qz qw timestamp``).

    Only rows whose image name starts with ``prefix`` are kept (left and right
    rows interleave; either subset forms a consistent 0.5 s track).  The
    quaternion is camera-to-world with OpenCV camera axes - verified against
    transforms.json: ``R_quat @ diag(1,-1,-1)`` matches the OpenGL c2w rotation
    to 3.4e-4 on the RTK-house capture.
    """
    path = Path(path)
    timestamps: list[float] = []
    positions: list[list[float]] = []
    quaternions: list[list[float]] = []
    for line in path.read_text(encoding="utf-8").strip().splitlines()[1:]:
        parts = line.split()
        if len(parts) != 12 or not parts[0].replace("\\", "/").startswith(prefix):
            continue
        numbers = [float(value) for value in parts[1:]]
        timestamps.append(numbers[-1])
        positions.append(numbers[0:3])
        quaternions.append(numbers[6:10])
    if len(timestamps) < 2:
        raise ValueError(f"ImgPose track has fewer than 2 '{prefix}' rows: {path}")
    order = np.argsort(np.asarray(timestamps))
    return PoseTrack(
        timestamps=np.asarray(timestamps)[order],
        positions=np.asarray(positions)[order],
        quaternions=np.asarray(quaternions)[order],
        source_path=str(path),
    )


def pano_pose_at(track: PoseTrack, timestamp_ns: int) -> np.ndarray:
    """Panorama pose (pano-frame-to-world 4x4) at a panorama file timestamp."""
    c2w = track.pose_at(float(timestamp_ns) / 1e9)
    pose = np.eye(4)
    pose[:3, :3] = c2w[:3, :3] @ PANO_FROM_CAMERA
    pose[:3, 3] = c2w[:3, 3]
    return pose


def project_points_equirect(
    pano_pose: np.ndarray,
    xyz: np.ndarray,
    *,
    width: int = 4096,
    height: int = 2048,
    min_range: float = 0.2,
):
    """Project world points into an equirectangular panorama.

    ``pano_pose`` is the 4x4 pano-frame-to-world matrix from
    :func:`pano_pose_at`.  Mapping: ``u = (0.5 + atan2(x, z) / 2pi) * width``
    (z toward the centre column), ``v = (0.5 + asin(y / r) / pi) * height``
    (y down).  Returns ``(uv, range, valid)`` where ``valid`` masks points
    closer than ``min_range`` to the pano centre.
    """
    pts = np.atleast_2d(np.asarray(xyz, dtype=np.float64))
    rotation = np.asarray(pano_pose)[:3, :3]
    center = np.asarray(pano_pose)[:3, 3]
    direction = (pts - center) @ rotation
    distance = np.linalg.norm(direction, axis=1)
    valid = distance > min_range
    with np.errstate(divide="ignore", invalid="ignore"):
        unit = direction / np.where(distance > 0, distance, 1.0)[:, None]
        u = (0.5 + np.arctan2(unit[:, 0], unit[:, 2]) / (2 * np.pi)) * width
        v = (0.5 + np.arcsin(np.clip(unit[:, 1], -1.0, 1.0)) / np.pi) * height
    uv = np.stack([np.mod(u, width), np.clip(v, 0, height - 1)], axis=1)
    uv[~valid] = np.nan
    return uv, distance, valid


# --------------------------------------------------------------------------
# Scene V2 element wireframes (LAS/world coordinates)
# --------------------------------------------------------------------------


def wall_wireframe(wall: dict, ground_z: float, children: list[dict] | None = None) -> dict:
    """Authoritative wall wireframe in world (LAS) coordinates.

    Returns ``{"outline": [...4 loops...], "openings": [rect...]}`` where the
    outline is the wall centreline rectangle (base edge, top edge, two ends)
    and each opening (window/door child) is a rectangle on the same plane.
    ``ground_z`` is the LAS z of scene elevation zero.
    """
    start = np.asarray(wall["start"], dtype=np.float64)
    end = np.asarray(wall["end"], dtype=np.float64)
    base = float(wall.get("baseHeight", 0.0)) + ground_z
    top = base + float(wall["height"])
    p0 = np.array([start[0], start[1], base])
    p1 = np.array([end[0], end[1], base])
    p2 = np.array([end[0], end[1], top])
    p3 = np.array([start[0], start[1], top])
    outline = [p0.tolist(), p1.tolist(), p2.tolist(), p3.tolist()]

    direction = end - start
    length = float(np.linalg.norm(direction))
    unit = direction / length if length > 0 else np.array([1.0, 0.0])
    openings = []
    for child in children or []:
        if child.get("type") not in {"window", "door", "opening"}:
            continue
        offset = float(child.get("hostOffsetM", 0.0))
        width = float(child.get("width", 0.0))
        sill = float(child.get("sillHeight", 0.0)) + ground_z
        head = sill + float(child.get("height", 0.0))
        a = start + unit * offset
        b = start + unit * (offset + width)
        openings.append(
            {
                "id": child.get("id"),
                "type": child.get("type"),
                "rect": [
                    [float(a[0]), float(a[1]), sill],
                    [float(b[0]), float(b[1]), sill],
                    [float(b[0]), float(b[1]), head],
                    [float(a[0]), float(a[1]), head],
                ],
            }
        )
    return {"outline": outline, "openings": openings, "lengthM": length}
