#!/usr/bin/env python3
"""Tree candidate detector for outdoor captures.

Builds a canopy height model (CHM) from ground-relative returns, finds local
maxima as tree-top candidates, then verifies each candidate against trunk
returns near the ground. Output is CANDIDATES ONLY - positions, heights and
canopy radii with per-candidate evidence counts. The agent reviews them
against the orthophoto before creating scene items; nothing is auto-accepted.

  python scene-core/detect_trees.py --las cloud.las --output trees.json \
      [--min-height 4] [--min-separation 3] [--exclude x0,y0,x1,y1]

--exclude carves out plan rectangles (e.g. the building footprint) so roof
points do not become trees.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

try:
    import laspy
except ImportError as error:  # pragma: no cover
    raise SystemExit(f"laspy is required: {error}")

CHUNK = 2_000_000
CELL = 0.4
GROUND_CELL = 2.0


def sliding_max(array: np.ndarray, radius: int) -> np.ndarray:
    result = array.copy()
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx == 0 and dy == 0:
                continue
            shifted = np.roll(np.roll(array, dy, axis=0), dx, axis=1)
            # Edges wrap with roll; suppress wrapped values.
            if dy > 0:
                shifted[:dy, :] = -np.inf
            elif dy < 0:
                shifted[dy:, :] = -np.inf
            if dx > 0:
                shifted[:, :dx] = -np.inf
            elif dx < 0:
                shifted[:, dx:] = -np.inf
            result = np.maximum(result, shifted)
    return result


def smooth3(array: np.ndarray) -> np.ndarray:
    padded = np.pad(array, 1, mode="edge")
    total = np.zeros_like(array)
    for dy in range(3):
        for dx in range(3):
            total += padded[dy:dy + array.shape[0], dx:dx + array.shape[1]]
    return total / 9.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--las", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--min-height", type=float, default=4.0)
    parser.add_argument("--min-separation", type=float, default=3.0)
    parser.add_argument("--max-trees", type=int, default=80)
    parser.add_argument("--every", type=int, default=1)
    parser.add_argument("--exclude", action="append", default=[],
                        help="x0,y0,x1,y1 plan rectangle to ignore (repeatable)")
    args = parser.parse_args()

    excludes = []
    for text in args.exclude:
        x0, y0, x1, y1 = (float(v) for v in text.split(","))
        excludes.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))

    with laspy.open(args.las) as reader:
        header = reader.header
        min_x, min_y, _ = header.mins
        max_x, max_y, _ = header.maxs
    width = int(math.ceil((max_x - min_x) / CELL))
    height = int(math.ceil((max_y - min_y) / CELL))

    chm = np.zeros((height, width), dtype=np.float32)
    low_z = np.full((height, width), np.inf, dtype=np.float32)
    trunk_count = np.zeros((height, width), dtype=np.uint32)

    def cells(x, y):
        col = ((x - min_x) / CELL).astype(np.int32)
        row = ((max_y - y) / CELL).astype(np.int32)
        ok = (col >= 0) & (col < width) & (row >= 0) & (row < height)
        return row[ok], col[ok], ok

    with laspy.open(args.las) as reader:
        for points in reader.chunk_iterator(CHUNK):
            if args.every > 1:
                points = points[::args.every]
            x = np.asarray(points.x)
            y = np.asarray(points.y)
            z = np.asarray(points.z, dtype=np.float32)
            row, col, ok = cells(x, y)
            np.minimum.at(low_z, (row, col), z[ok])

    # Coarse ground reference (10th percentile of cell minima per block).
    blocks = max(1, int(round(GROUND_CELL / CELL)))
    ground = np.full((math.ceil(height / blocks), math.ceil(width / blocks)), np.nan, dtype=np.float32)
    for r in range(ground.shape[0]):
        for c in range(ground.shape[1]):
            block = low_z[r * blocks:(r + 1) * blocks, c * blocks:(c + 1) * blocks]
            block = block[np.isfinite(block)]
            if block.size:
                ground[r, c] = np.percentile(block, 10)
    ground = np.where(np.isnan(ground), np.nanmedian(ground), ground)

    def ground_at(row, col):
        return ground[np.minimum(row // blocks, ground.shape[0] - 1),
                      np.minimum(col // blocks, ground.shape[1] - 1)]

    with laspy.open(args.las) as reader:
        for points in reader.chunk_iterator(CHUNK):
            if args.every > 1:
                points = points[::args.every]
            x = np.asarray(points.x)
            y = np.asarray(points.y)
            z = np.asarray(points.z, dtype=np.float32)
            row, col, ok = cells(x, y)
            z_rel = z[ok] - ground_at(row, col)
            canopy = z_rel > 2.0
            np.maximum.at(chm, (row[canopy], col[canopy]), z_rel[canopy])
            trunk = (z_rel > 0.4) & (z_rel < 2.2)
            np.add.at(trunk_count, (row[trunk], col[trunk]), 1)

    for x0, y0, x1, y1 in excludes:
        c0 = max(0, int((x0 - min_x) / CELL))
        c1 = min(width, int(math.ceil((x1 - min_x) / CELL)))
        r0 = max(0, int((max_y - y1) / CELL))
        r1 = min(height, int(math.ceil((max_y - y0) / CELL)))
        chm[r0:r1, c0:c1] = 0

    smoothed = smooth3(chm)
    radius = max(1, int(round(args.min_separation / CELL)))
    peaks = (smoothed >= args.min_height) & (smoothed >= sliding_max(smoothed, radius))
    rows, cols = np.nonzero(peaks)
    order = np.argsort(-smoothed[rows, cols])
    rows, cols = rows[order][:args.max_trees], cols[order][:args.max_trees]

    candidates = []
    trunk_radius_cells = max(1, int(round(1.2 / CELL)))
    canopy_probe = int(round(8.0 / CELL))
    for index, (r, c) in enumerate(zip(rows, cols)):
        top = float(smoothed[r, c])
        # Canopy radius: walk outward until CHM falls below 35% of top height.
        radii = []
        for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            for step in range(1, canopy_probe):
                rr, cc = r + dr * step, c + dc * step
                if not (0 <= rr < height and 0 <= cc < width) or smoothed[rr, cc] < top * 0.35:
                    radii.append(step * CELL)
                    break
            else:
                radii.append(canopy_probe * CELL)
        canopy_radius = float(np.clip(np.mean(radii), 0.8, 8.0))
        trunk_window = trunk_count[max(0, r - trunk_radius_cells):r + trunk_radius_cells + 1,
                                   max(0, c - trunk_radius_cells):c + trunk_radius_cells + 1]
        trunk_evidence = int(trunk_window.sum())
        candidates.append({
            "id": f"tree-cand-{index:03d}",
            "center": [round(min_x + (c + 0.5) * CELL, 3), round(max_y - (r + 0.5) * CELL, 3)],
            "heightM": round(top, 2),
            "canopyRadiusM": round(canopy_radius, 2),
            "trunkReturnCount": trunk_evidence,
            "trunkVerified": trunk_evidence >= 12,
        })

    payload = {"las": args.las.name, "cellSizeM": CELL, "minHeightM": args.min_height,
               "minSeparationM": args.min_separation, "excluded": excludes,
               "candidateCount": len(candidates), "candidates": candidates}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "candidates": len(candidates),
                      "trunkVerified": sum(1 for cand in candidates if cand["trunkVerified"]),
                      "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
