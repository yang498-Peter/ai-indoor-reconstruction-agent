#!/usr/bin/env python3
"""Render a browser-free colour elevation along an Agent-picked source segment."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import cv2
import laspy
import numpy as np


def parse_xy(value: str) -> np.ndarray:
    parts = [float(item.strip()) for item in value.split(",")]
    if len(parts) != 2 or not np.isfinite(parts).all():
        raise argparse.ArgumentTypeError("expected finite x,y")
    return np.asarray(parts, dtype=np.float64)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--las", required=True, type=Path)
    parser.add_argument("--start", required=True, type=parse_xy)
    parser.add_argument("--end", required=True, type=parse_xy)
    parser.add_argument("--floor-z", required=True, type=float)
    parser.add_argument("--ceiling-z", required=True, type=float)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--resolution", type=float, default=0.0125)
    parser.add_argument("--half-band", type=float, default=0.18)
    args = parser.parse_args()

    delta = args.end - args.start
    length = float(np.linalg.norm(delta))
    if length < 0.05 or args.resolution <= 0 or args.half_band <= 0:
        raise SystemExit("invalid segment or raster parameters")
    unit = delta / length
    lower_z = args.floor_z - 0.04
    upper_z = args.ceiling_z + 0.12
    width = int(math.ceil((length + 0.5) / args.resolution)) + 1
    height = int(math.ceil((upper_z - lower_z) / args.resolution)) + 1
    nearest = np.full(width * height, np.inf, dtype=np.float32)
    colors = np.zeros((width * height, 3), dtype=np.uint8)
    offset_edges = np.linspace(-0.8, 0.8, 321, dtype=np.float64)
    offset_histogram = np.zeros(len(offset_edges) - 1, dtype=np.int64)
    point_count = 0

    with laspy.open(args.las) as reader:
        for chunk in reader.chunk_iterator(800_000):
            x = np.asarray(chunk.x, dtype=np.float64)
            y = np.asarray(chunk.y, dtype=np.float64)
            z = np.asarray(chunk.z, dtype=np.float64)
            dx = x - args.start[0]
            dy = y - args.start[1]
            along = dx * unit[0] + dy * unit[1]
            perpendicular = -dx * unit[1] + dy * unit[0]
            structural = (
                (along >= 0) & (along <= length) &
                (np.abs(perpendicular) <= 0.8) &
                (z >= args.floor_z + 0.15) & (z <= args.ceiling_z - 0.08)
            )
            if np.any(structural):
                offset_histogram += np.histogram(perpendicular[structural], bins=offset_edges)[0]
            mask = (
                (along >= -0.25) & (along <= length + 0.25) &
                (np.abs(perpendicular) <= args.half_band) &
                (z >= lower_z) & (z <= upper_z)
            )
            if not np.any(mask):
                continue
            rgb = np.column_stack((np.asarray(chunk.red), np.asarray(chunk.green), np.asarray(chunk.blue)))
            if rgb.max(initial=0) > 255:
                rgb = np.rint(rgb / 257.0)
            rgb = np.clip(rgb, 0, 255).astype(np.uint8)
            along_selected = along[mask] + 0.25
            z_selected = z[mask]
            distance_selected = np.abs(perpendicular[mask]).astype(np.float32)
            col = np.clip((along_selected / args.resolution).astype(np.int32), 0, width - 1)
            row = np.clip(((upper_z - z_selected) / args.resolution).astype(np.int32), 0, height - 1)
            linear = row.astype(np.int64) * width + col
            order = np.lexsort((distance_selected, linear))
            ordered_linear = linear[order]
            first = np.r_[True, ordered_linear[1:] != ordered_linear[:-1]]
            chosen = order[first]
            chosen_linear = linear[chosen]
            closer = distance_selected[chosen] < nearest[chosen_linear]
            chosen = chosen[closer]
            chosen_linear = linear[chosen]
            nearest[chosen_linear] = distance_selected[chosen]
            colors[chosen_linear] = rgb[mask][chosen]
            point_count += int(mask.sum())

    valid = np.isfinite(nearest).reshape(height, width)
    image = np.full((height, width, 3), 13, dtype=np.uint8)
    image[valid] = colors.reshape(height, width, 3)[valid]
    image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    for relative_height, color in [(0.0, (0, 220, 255)), (args.ceiling_z - args.floor_z, (60, 80, 255))]:
        row = int(round((upper_z - (args.floor_z + relative_height)) / args.resolution))
        if 0 <= row < height:
            cv2.line(image, (0, row), (width - 1, row), color, 1, cv2.LINE_AA)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded, buffer = cv2.imencode(args.output.suffix or ".png", image)
    if not encoded:
        raise SystemExit("failed to encode elevation")
    buffer.tofile(str(args.output))

    smooth = np.convolve(offset_histogram, np.ones(7, dtype=np.float64), mode="same")
    best = int(np.argmax(smooth))
    metadata = {
        "sourceStart": args.start.tolist(),
        "sourceEnd": args.end.tolist(),
        "lengthM": round(length, 4),
        "resolutionMPerPixel": args.resolution,
        "halfBandM": args.half_band,
        "pointCount": point_count,
        "strongestPerpendicularOffsetM": round(float((offset_edges[best] + offset_edges[best + 1]) * 0.5), 4),
        "offsetMeaning": "measurement suggestion only; Agent must inspect the unannotated elevation",
    }
    metadata_path = args.metadata or args.output.with_suffix(".json")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
