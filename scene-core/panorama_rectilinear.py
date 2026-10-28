#!/usr/bin/env python3
"""Render rectilinear evidence views from equirectangular panoramas.

This is deliberately scene-agnostic: it only changes the projection used for
human review and does not infer geometry or semantic labels.
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path

import cv2
import numpy as np


def render_view(
    image: np.ndarray,
    *,
    yaw_degrees: float,
    pitch_degrees: float = 0.0,
    horizontal_fov_degrees: float = 95.0,
    width: int = 960,
    height: int = 720,
) -> np.ndarray:
    """Project a panorama: yaw 0 faces its centre, positive pitch looks up."""
    if image.ndim not in (2, 3) or min(image.shape[:2], default=0) < 2:
        raise ValueError("panorama must contain at least 2 by 2 pixels")
    if not all(math.isfinite(value) for value in (yaw_degrees, pitch_degrees, horizontal_fov_degrees)):
        raise ValueError("yaw, pitch and field of view must be finite")
    if not -90.0 <= pitch_degrees <= 90.0:
        raise ValueError("pitch must be between -90 and 90 degrees")
    if not 1.0 < horizontal_fov_degrees < 179.0:
        raise ValueError("horizontal field of view must be between 1 and 179 degrees")
    if width < 2 or height < 2:
        raise ValueError("output dimensions must be at least 2 by 2 pixels")

    source_height, source_width = image.shape[:2]
    focal = (width / 2.0) / math.tan(math.radians(horizontal_fov_degrees) / 2.0)
    x = (np.arange(width, dtype=np.float32) - (width - 1.0) / 2.0) / focal
    y = ((height - 1.0) / 2.0 - np.arange(height, dtype=np.float32)) / focal
    xx, yy = np.meshgrid(x, y)
    zz = np.ones_like(xx)
    rays = np.stack((xx, yy, zz), axis=-1)
    rays /= np.linalg.norm(rays, axis=-1, keepdims=True)

    yaw = math.radians(yaw_degrees)
    pitch = math.radians(pitch_degrees)
    yaw_rotation = np.array(
        [[math.cos(yaw), 0.0, math.sin(yaw)], [0.0, 1.0, 0.0], [-math.sin(yaw), 0.0, math.cos(yaw)]],
        dtype=np.float32,
    )
    pitch_rotation = np.array(
        [[1.0, 0.0, 0.0], [0.0, math.cos(pitch), math.sin(pitch)], [0.0, -math.sin(pitch), math.cos(pitch)]],
        dtype=np.float32,
    )
    rays = rays @ (yaw_rotation @ pitch_rotation).T

    longitude = np.arctan2(rays[..., 0], rays[..., 2])
    latitude = np.arcsin(np.clip(rays[..., 1], -1.0, 1.0))
    map_x = ((longitude / (2.0 * np.pi)) + 0.5) * source_width
    map_y = (0.5 - latitude / np.pi) * source_height
    map_x = np.mod(map_x, source_width).astype(np.float32)
    map_y = np.clip(map_y, 0.0, source_height - 1.0).astype(np.float32)
    # Horizontal longitude wraps, but interpolation at the poles must never
    # wrap into the opposite pole. Four replicated rows cover Lanczos taps.
    padded = cv2.copyMakeBorder(image, 4, 4, 0, 0, cv2.BORDER_REPLICATE)
    return cv2.remap(padded, map_x, map_y + 4, cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_WRAP)


def make_contact_sheet(
    image_path: Path,
    output_path: Path,
    *,
    yaws: list[float],
    pitch: float,
    fov: float,
    width: int,
    height: int,
) -> None:
    if not yaws:
        raise ValueError("at least one yaw is required")
    # Python file I/O supports Unicode Windows paths that OpenCV's filename
    # functions may not handle consistently.
    image = cv2.imdecode(np.frombuffer(image_path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"could not read panorama: {image_path}")
    views: list[np.ndarray] = []
    for yaw in yaws:
        view = render_view(
            image,
            yaw_degrees=yaw,
            pitch_degrees=pitch,
            horizontal_fov_degrees=fov,
            width=width,
            height=height,
        )
        cv2.rectangle(view, (0, 0), (260, 38), (20, 20, 20), thickness=-1)
        cv2.putText(
            view,
            f"yaw {yaw:g} deg",
            (12, 27),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.72,
            (245, 245, 245),
            2,
            cv2.LINE_AA,
        )
        views.append(view)
    rows = [np.hstack(views[index : index + 2]) for index in range(0, len(views), 2)]
    if len(views) > 1 and len(views) % 2:
        rows[-1] = np.hstack((rows[-1], np.zeros_like(views[0])))
    sheet = np.vstack(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise OSError(f"could not write contact sheet: {output_path}")
    # Never overwrite an existing image, even if another process races preflight.
    with output_path.open("xb") as stream:
        stream.write(encoded.tobytes())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="equirectangular panorama files")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--yaws", default="0,90,180,270")
    parser.add_argument("--pitch", type=float, default=0.0, help="degrees; positive looks up")
    parser.add_argument("--fov", type=float, default=95.0)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=720)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    yaws = [float(value.strip()) for value in args.yaws.split(",") if value.strip()]
    if not yaws:
        raise ValueError("at least one yaw is required")
    outputs = [args.output_dir / f"{path.stem}-rectilinear.jpg" for path in args.inputs]
    key = lambda path: os.path.normcase(str(path.resolve()))
    output_keys = [key(path) for path in outputs]
    if len(set(output_keys)) != len(output_keys):
        raise ValueError("inputs would produce duplicate output names")
    if set(output_keys) & {key(path) for path in args.inputs}:
        raise ValueError("an output would overwrite an input")
    if any(path.exists() for path in outputs):
        raise FileExistsError("an output already exists; choose a fresh output directory")
    for image_path, output_path in zip(args.inputs, outputs):
        make_contact_sheet(
            image_path,
            output_path,
            yaws=yaws,
            pitch=args.pitch,
            fov=args.fov,
            width=args.width,
            height=args.height,
        )
        print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
