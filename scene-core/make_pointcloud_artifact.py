#!/usr/bin/env python3
"""Convert a LAS/LAZ into the viewer's LRPC point-cloud artifact.

LRPC layout (little endian): magic 'LRPC', uint32 version=1, uint32 count,
uint32 stride=16, then per point float32 x,y,z (DISPLAY frame) + uint8 r,g,b
+ 1 pad byte.

Display frame must match the compiled scene:
  display = [x_src - offsetX, z_las - groundZ, -y_src - offsetZ]
Pass the SAME --offset the scene pins in meta.displayOffset and the same
--ground-z the assembly used as elevation zero, or the cloud will not
register with the model.

  python scene-core/make_pointcloud_artifact.py --las cloud.las \
      --output outputs/<cap>/cloud.lrpc "--offset=5,5" "--ground-z=-0.5" \
      --target-points 2500000 [--crop x0,y0,x1,y1]
"""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

import numpy as np

try:
    import laspy
except ImportError as error:  # pragma: no cover
    raise SystemExit(f"laspy is required: {error}")

CHUNK = 2_000_000


def parse_pair(text: str) -> tuple[float, float]:
    a, b = (float(v) for v in text.split(","))
    return a, b


def parse_crop(text: str) -> tuple[float, float, float, float]:
    a, b, c, d = (float(v) for v in text.split(","))
    return a, b, c, d


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--las", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--offset", required=True, type=parse_pair,
                        help="displayOffset ox,oz pinned in scene meta")
    parser.add_argument("--ground-z", required=True, type=float,
                        help="LAS z that the scene treats as elevation 0")
    parser.add_argument("--target-points", type=int, default=2_500_000)
    parser.add_argument("--crop", type=parse_crop, help="source-plan minX,minY,maxX,maxY")
    args = parser.parse_args()

    with laspy.open(args.las) as reader:
        total = reader.header.point_count
    every = max(1, total // args.target_points)

    offset_x, offset_z = args.offset
    rows = []
    kept = 0
    with laspy.open(args.las) as reader:
        for points in reader.chunk_iterator(CHUNK):
            points = points[::every]
            x = np.asarray(points.x)
            y = np.asarray(points.y)
            z = np.asarray(points.z, dtype=np.float64)
            keep = np.ones(len(x), dtype=bool)
            if args.crop:
                x0, y0, x1, y1 = args.crop
                keep = (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)
            if not keep.any():
                continue
            has_rgb = "red" in points.point_format.dimension_names
            if has_rgb:
                red = np.asarray(points.red)[keep]
                shift = 8 if red.max(initial=0) > 255 else 0
                rgb = np.stack([
                    red >> shift,
                    np.asarray(points.green)[keep] >> shift,
                    np.asarray(points.blue)[keep] >> shift,
                ], axis=1).astype(np.uint8)
            else:
                rgb = np.full((int(keep.sum()), 3), 200, dtype=np.uint8)
            display = np.stack([
                x[keep] - offset_x,
                z[keep] - args.ground_z,
                -y[keep] - offset_z,
            ], axis=1).astype(np.float32)
            record = np.zeros((display.shape[0], 16), dtype=np.uint8)
            record[:, :12] = display.view(np.uint8).reshape(display.shape[0], 12)
            record[:, 12:15] = rgb
            rows.append(record)
            kept += display.shape[0]

    payload = np.concatenate(rows, axis=0) if rows else np.zeros((0, 16), dtype=np.uint8)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as sink:
        sink.write(b"LRPC")
        sink.write(struct.pack("<III", 1, kept, 16))
        sink.write(payload.tobytes())
    print(json.dumps({"ok": True, "points": kept, "every": every,
                      "bytes": args.output.stat().st_size, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
