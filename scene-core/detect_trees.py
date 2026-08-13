#!/usr/bin/env python3
"""Tree candidate detector for outdoor captures - trunk-first.

Positions come from TRUNK clusters, not canopy peaks: a canopy-height-model
maximum can sit meters away from the stem (winter deciduous crowns are
asymmetric), which visibly misplaces the modeled tree against the raw cloud.

Stage 1 - trunk clustering: ground-relative band (default 0.5-2.2 m) on a
fine plan grid; connected high-density cells form clusters that must be
small in plan (a stem, not a wall or bush) and vertically continuous.
Stage 2 - per-trunk attributes: tree height and canopy radius measured from
the cloud around each verified stem.
Stage 3 - CHM peaks with no trunk nearby are still reported, but flagged
`trunkVerified: false` so the agent reviews them instead of accepting.

Output is CANDIDATES ONLY. Nothing is auto-accepted.

  python scene-core/detect_trees.py --las cloud.las --output trees.json \
      [--min-height 3] [--exclude x0,y0,x1,y1]...
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
TRUNK_CELL = 0.15
CHM_CELL = 0.4
GROUND_CELL = 2.0
TRUNK_BAND = (0.5, 2.2)
# A real stem keeps returning points ABOVE the trunk band; fence posts and
# rails do not, even when a neighbouring crown overhangs them.
STEM_CONTINUITY_BAND = (2.2, 4.6)
STEM_CONTINUITY_MIN_POINTS = 20
STEM_CONTINUITY_RADIUS_CELLS = 2
TRUNK_MIN_POINTS = 45
TRUNK_MIN_SPAN_M = 1.5
TRUNK_MAX_DIAMETER_M = 1.1
CELL_MIN_POINTS = 6


def label_clusters(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """4-connected components without scipy."""
    labels = np.zeros(mask.shape, dtype=np.int32)
    current = 0
    stack: list[tuple[int, int]] = []
    height, width = mask.shape
    for r0 in range(height):
        for c0 in range(width):
            if not mask[r0, c0] or labels[r0, c0]:
                continue
            current += 1
            stack.append((r0, c0))
            labels[r0, c0] = current
            while stack:
                r, c = stack.pop()
                for rr, cc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                    if 0 <= rr < height and 0 <= cc < width and mask[rr, cc] and not labels[rr, cc]:
                        labels[rr, cc] = current
                        stack.append((rr, cc))
    return labels, current


def sliding_max(array: np.ndarray, radius: int) -> np.ndarray:
    result = array.copy()
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx == 0 and dy == 0:
                continue
            shifted = np.roll(np.roll(array, dy, axis=0), dx, axis=1)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--las", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--min-height", type=float, default=3.0)
    parser.add_argument("--max-trees", type=int, default=150)
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

    t_width = int(math.ceil((max_x - min_x) / TRUNK_CELL))
    t_height = int(math.ceil((max_y - min_y) / TRUNK_CELL))
    c_width = int(math.ceil((max_x - min_x) / CHM_CELL))
    c_height = int(math.ceil((max_y - min_y) / CHM_CELL))

    low_z = np.full((c_height, c_width), np.inf, dtype=np.float32)

    def coarse_cells(x, y):
        col = ((x - min_x) / CHM_CELL).astype(np.int32)
        row = ((max_y - y) / CHM_CELL).astype(np.int32)
        ok = (col >= 0) & (col < c_width) & (row >= 0) & (row < c_height)
        return row[ok], col[ok], ok

    # Pass 1: cell minima for the ground reference.
    with laspy.open(args.las) as reader:
        for points in reader.chunk_iterator(CHUNK):
            if args.every > 1:
                points = points[::args.every]
            row, col, ok = coarse_cells(np.asarray(points.x), np.asarray(points.y))
            np.minimum.at(low_z, (row, col), np.asarray(points.z, dtype=np.float32)[ok])

    blocks = max(1, int(round(GROUND_CELL / CHM_CELL)))
    g_height, g_width = math.ceil(c_height / blocks), math.ceil(c_width / blocks)
    ground = np.full((g_height, g_width), np.nan, dtype=np.float32)
    for r in range(g_height):
        for c in range(g_width):
            block = low_z[r * blocks:(r + 1) * blocks, c * blocks:(c + 1) * blocks]
            block = block[np.isfinite(block)]
            if block.size:
                ground[r, c] = np.percentile(block, 10)
    ground = np.where(np.isnan(ground), np.nanmedian(ground), ground)

    def ground_for(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        gr = np.minimum(((max_y - y) / GROUND_CELL).astype(np.int32), g_height - 1)
        gc = np.minimum(((x - min_x) / GROUND_CELL).astype(np.int32), g_width - 1)
        return ground[np.maximum(gr, 0), np.maximum(gc, 0)]

    # Pass 2: trunk-band accumulation (fine grid) + CHM (coarse grid).
    trunk_count = np.zeros((t_height, t_width), dtype=np.uint32)
    trunk_zmin = np.full((t_height, t_width), np.inf, dtype=np.float32)
    trunk_zmax = np.full((t_height, t_width), -np.inf, dtype=np.float32)
    stem_above = np.zeros((t_height, t_width), dtype=np.uint32)
    chm = np.zeros((c_height, c_width), dtype=np.float32)

    with laspy.open(args.las) as reader:
        for points in reader.chunk_iterator(CHUNK):
            if args.every > 1:
                points = points[::args.every]
            x = np.asarray(points.x)
            y = np.asarray(points.y)
            z = np.asarray(points.z, dtype=np.float32)
            z_rel = z - ground_for(x, y)

            trunk = (z_rel >= TRUNK_BAND[0]) & (z_rel < TRUNK_BAND[1])
            tx, ty, tz = x[trunk], y[trunk], z_rel[trunk]
            tcol = ((tx - min_x) / TRUNK_CELL).astype(np.int32)
            trow = ((max_y - ty) / TRUNK_CELL).astype(np.int32)
            ok = (tcol >= 0) & (tcol < t_width) & (trow >= 0) & (trow < t_height)
            np.add.at(trunk_count, (trow[ok], tcol[ok]), 1)
            np.minimum.at(trunk_zmin, (trow[ok], tcol[ok]), tz[ok])
            np.maximum.at(trunk_zmax, (trow[ok], tcol[ok]), tz[ok])

            upper = (z_rel >= STEM_CONTINUITY_BAND[0]) & (z_rel < STEM_CONTINUITY_BAND[1])
            ux_, uy_ = x[upper], y[upper]
            ucol = ((ux_ - min_x) / TRUNK_CELL).astype(np.int32)
            urow = ((max_y - uy_) / TRUNK_CELL).astype(np.int32)
            uok = (ucol >= 0) & (ucol < t_width) & (urow >= 0) & (urow < t_height)
            np.add.at(stem_above, (urow[uok], ucol[uok]), 1)

            canopy = z_rel > 2.0
            row, col, okc = coarse_cells(x[canopy], y[canopy])
            np.maximum.at(chm, (row, col), z_rel[canopy][okc])

    def excluded(px: float, py: float) -> bool:
        return any(x0 <= px <= x1 and y0 <= py <= y1 for x0, y0, x1, y1 in excludes)

    # Stage 1: trunk clusters.
    mask = trunk_count >= CELL_MIN_POINTS
    labels, cluster_count = label_clusters(mask)
    smoothed_chm = chm  # canopy attributes tolerate raw CHM

    candidates = []
    for label in range(1, cluster_count + 1):
        rows, cols = np.nonzero(labels == label)
        points_total = int(trunk_count[rows, cols].sum())
        if points_total < TRUNK_MIN_POINTS:
            continue
        span_r = (rows.max() - rows.min() + 1) * TRUNK_CELL
        span_c = (cols.max() - cols.min() + 1) * TRUNK_CELL
        if max(span_r, span_c) > TRUNK_MAX_DIAMETER_M:
            continue
        z_span = float(trunk_zmax[rows, cols].max() - trunk_zmin[rows, cols].min())
        if z_span < TRUNK_MIN_SPAN_M:
            continue
        weights = trunk_count[rows, cols].astype(np.float64)
        cx = float(min_x + (cols * TRUNK_CELL + TRUNK_CELL / 2) @ weights / weights.sum())
        cy = float(max_y - (rows * TRUNK_CELL + TRUNK_CELL / 2) @ weights / weights.sum())
        if excluded(cx, cy):
            continue
        # Stem continuity: fence posts/rails end at ~2 m; a tree keeps its
        # stem going. Check a small neighbourhood above the cluster centroid.
        crow = int((max_y - cy) / TRUNK_CELL)
        ccol = int((cx - min_x) / TRUNK_CELL)
        rad = STEM_CONTINUITY_RADIUS_CELLS
        window = stem_above[max(0, crow - rad):crow + rad + 1, max(0, ccol - rad):ccol + rad + 1]
        continuity = int(window.sum())
        if continuity < STEM_CONTINUITY_MIN_POINTS:
            continue
        # Stage 2: height and canopy radius around the stem.
        cr = int((max_y - cy) / CHM_CELL)
        cc = int((cx - min_x) / CHM_CELL)
        probe = int(round(6.0 / CHM_CELL))
        r0, r1 = max(0, cr - probe), min(c_height, cr + probe + 1)
        c0, c1 = max(0, cc - probe), min(c_width, cc + probe + 1)
        local = smoothed_chm[r0:r1, c0:c1]
        height_m = float(local.max()) if local.size else 0.0
        if height_m < args.min_height:
            continue
        radii = []
        for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            for step in range(1, probe):
                rr, cc2 = cr + dr * step, cc + dc * step
                if not (0 <= rr < c_height and 0 <= cc2 < c_width) or smoothed_chm[rr, cc2] < height_m * 0.3:
                    radii.append(step * CHM_CELL)
                    break
            else:
                radii.append(probe * CHM_CELL)
        canopy_radius = float(np.clip(np.mean(radii), 0.8, 7.0))
        candidates.append({
            "id": f"trunk-{len(candidates):03d}",
            "center": [round(cx, 3), round(cy, 3)],
            "heightM": round(height_m, 2),
            "canopyRadiusM": round(canopy_radius, 2),
            "trunkReturnCount": points_total,
            "trunkDiameterM": round(max(span_r, span_c), 2),
            "trunkZSpanM": round(z_span, 2),
            "stemContinuityCount": continuity,
            "trunkVerified": True,
            "method": "trunk-cluster",
        })

    # Deduplicate stems closer than 1.2 m (multi-stem clumps keep the denser).
    candidates.sort(key=lambda cand: -cand["trunkReturnCount"])
    kept: list[dict] = []
    for cand in candidates:
        if all(math.dist(cand["center"], other["center"]) >= 1.2 for other in kept):
            kept.append(cand)
    kept = kept[:args.max_trees]

    # Stage 3: CHM peaks with no stem nearby -> unverified candidates.
    radius = max(1, int(round(3.0 / CHM_CELL)))
    peaks = (chm >= max(args.min_height, 5.0)) & (chm >= sliding_max(chm, radius))
    prow, pcol = np.nonzero(peaks)
    for r, c in zip(prow, pcol):
        px = float(min_x + (c + 0.5) * CHM_CELL)
        py = float(max_y - (r + 0.5) * CHM_CELL)
        if excluded(px, py):
            continue
        if any(math.dist((px, py), k["center"]) < 3.0 for k in kept):
            continue
        kept.append({
            "id": f"crown-{len(kept):03d}",
            "center": [round(px, 3), round(py, 3)],
            "heightM": round(float(chm[r, c]), 2),
            "canopyRadiusM": 1.5,
            "trunkReturnCount": 0,
            "trunkVerified": False,
            "method": "chm-peak-no-stem",
            "note": "crown mass without a detected stem below - occluded trunk or neighbour overhang; REVIEW before accepting",
        })

    payload = {"las": args.las.name, "trunkCellM": TRUNK_CELL, "trunkBandM": list(TRUNK_BAND),
               "minHeightM": args.min_height, "excluded": excludes,
               "candidateCount": len(kept), "candidates": kept}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "trunks": sum(1 for k in kept if k["trunkVerified"]),
                      "unverifiedCrowns": sum(1 for k in kept if not k["trunkVerified"]),
                      "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
