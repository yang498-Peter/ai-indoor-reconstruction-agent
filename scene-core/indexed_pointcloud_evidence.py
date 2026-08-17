#!/usr/bin/env python3
"""Render standard point-cloud evidence from a CaptureIndex.

The original LAS/LAZ is not opened here.  Global evidence replays compact
index tiles, while a local elevation reads only tiles intersecting its source-
metre corridor.  Every output is bound to the exact index fingerprint.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from capture_index import CaptureIndex
from pointcloud_evidence import (
    DEFAULT_BANDS,
    Grid,
    elevation_colormap,
    parse_bands,
    parse_crop,
    parse_line,
    parse_point,
    save_png,
)


def _add_stats(total: dict[str, int], part: dict[str, int]) -> None:
    for key, value in part.items():
        total[key] = total.get(key, 0) + int(value)


def render_overview(
    index: CaptureIndex,
    output: Path,
    *,
    cell: float = 0.08,
    ground_cell: float = 1.0,
    bands_text: str = DEFAULT_BANDS,
    crop: tuple[float, float, float, float] | None = None,
    every: int = 1,
) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    bands = parse_bands(bands_text)
    bounds = index.manifest["bounds"]
    assert isinstance(bounds, dict)
    min_x = float(bounds["minX"])
    min_y = float(bounds["minY"])
    max_x = float(bounds["maxX"])
    max_y = float(bounds["maxY"])
    if crop is not None:
        min_x, min_y, max_x, max_y = crop
    if cell <= 0 or ground_cell <= 0 or every < 1:
        raise ValueError("overview raster parameters are invalid")
    width = max(1, int(math.ceil((max_x - min_x) / cell)))
    height = max(1, int(math.ceil((max_y - min_y) / cell)))
    if width * height > 60_000_000:
        raise ValueError(f"grid {width}x{height} too large; raise cell")
    grid = Grid(min_x, max_y, width, height, cell)
    has_color = bool(index.manifest.get("hasColor"))
    query_stats: dict[str, int] = {}

    top_z = np.full((height, width), -np.inf, dtype=np.float32)
    top_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    low_z = np.full((height, width), np.inf, dtype=np.float32)
    low_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    count = np.zeros((height, width), dtype=np.uint32)

    for points in index.iter_bbox(min_x, min_y, max_x, max_y, every=every):
        _add_stats(query_stats, points.stats)
        row, col, ok = grid.indices(points.x, points.y)
        z_ok = points.z[ok].astype(np.float32, copy=False)
        rgb_ok = points.rgb[ok] if has_color else None
        order = np.argsort(z_ok, kind="stable")
        r_sorted, c_sorted, z_sorted = row[order], col[order], z_ok[order]
        np.maximum.at(top_z, (r_sorted, c_sorted), z_sorted)
        np.minimum.at(low_z, (r_sorted, c_sorted), z_sorted)
        np.add.at(count, (row, col), 1)
        if rgb_ok is not None:
            rgb_sorted = rgb_ok[order]
            is_top = z_sorted >= top_z[r_sorted, c_sorted] - 1e-4
            top_rgb[r_sorted[is_top], c_sorted[is_top]] = rgb_sorted[is_top]
            is_low = z_sorted <= low_z[r_sorted, c_sorted] + 1e-4
            low_rgb[r_sorted[is_low], c_sorted[is_low]] = rgb_sorted[is_low]

    valid = count > 0
    coarse = max(1, int(round(ground_cell / cell)))
    coarse_h = math.ceil(height / coarse)
    coarse_w = math.ceil(width / coarse)
    ground = np.full((coarse_h, coarse_w), np.nan, dtype=np.float32)
    for row_index in range(coarse_h):
        rows = slice(row_index * coarse, min((row_index + 1) * coarse, height))
        for col_index in range(coarse_w):
            cols = slice(col_index * coarse, min((col_index + 1) * coarse, width))
            block = low_z[rows, cols]
            block = block[np.isfinite(block)]
            if block.size:
                ground[row_index, col_index] = np.percentile(block, 10)
    global_ground = float(np.nanmedian(ground)) if np.isfinite(ground).any() else 0.0
    ground = np.where(np.isnan(ground), global_ground, ground)

    def ground_at(row: np.ndarray, col: np.ndarray) -> np.ndarray:
        return ground[
            np.minimum(row // coarse, coarse_h - 1),
            np.minimum(col // coarse, coarse_w - 1),
        ]

    band_count = [np.zeros((height, width), dtype=np.uint32) for _ in bands]
    band_rgb_sum = [np.zeros((height, width, 3), dtype=np.uint64) for _ in bands]
    for points in index.iter_bbox(min_x, min_y, max_x, max_y, every=every):
        _add_stats(query_stats, points.stats)
        row, col, ok = grid.indices(points.x, points.y)
        z_relative = points.z[ok] - ground_at(row, col)
        rgb_ok = points.rgb[ok] if has_color else None
        for band_index, (_name, lo, hi) in enumerate(bands):
            selected = (z_relative >= lo) & (z_relative < hi)
            np.add.at(band_count[band_index], (row[selected], col[selected]), 1)
            if rgb_ok is not None:
                np.add.at(
                    band_rgb_sum[band_index],
                    (row[selected], col[selected]),
                    rgb_ok[selected].astype(np.uint64),
                )

    manifest: dict[str, object] = {
        "kind": "indexed-pointcloud-evidence",
        "sourceKind": "capture-index",
        "index": str(index.root),
        "indexFingerprint": index.manifest["indexFingerprint"],
        "indexedPointCount": index.manifest["indexedPointCount"],
        "decimation": every,
        "bounds": {"minX": min_x, "minY": min_y, "maxX": max_x, "maxY": max_y},
        "groundReference": (
            f"per-cell 10th percentile of lowest returns, {ground_cell} m blocks; "
            "global median fallback"
        ),
        "grid": grid.meta(),
        "queryStats": {**query_stats, "indexPasses": 2},
        "images": {},
    }
    images = manifest["images"]
    assert isinstance(images, dict)

    top_image = np.where(valid[..., None], top_rgb, 24).astype(np.uint8)
    save_png(output / "ortho-top.png", top_image)
    images["ortho-top.png"] = {"kind": "top-surface color"}

    low_image = np.where(valid[..., None], low_rgb, 24).astype(np.uint8)
    save_png(output / "ortho-ground.png", low_image)
    images["ortho-ground.png"] = {"kind": "lowest-surface color"}

    save_png(output / "heightmap.png", elevation_colormap(np.where(valid, top_z, np.nan), valid))
    images["heightmap.png"] = {"kind": "top elevation colormap"}

    for band_index, (name, lo, hi) in enumerate(bands):
        counts = band_count[band_index]
        occupied = counts > 0
        image = np.full((height, width, 3), 24, dtype=np.uint8)
        if occupied.any():
            mean_rgb = np.zeros_like(image)
            if has_color:
                mean_rgb[occupied] = (
                    band_rgb_sum[band_index][occupied] // counts[occupied][..., None]
                ).astype(np.uint8)
            else:
                mean_rgb[occupied] = 190
            density = np.clip(
                counts / max(1.0, float(np.percentile(counts[occupied], 90))),
                0.3,
                1.0,
            )
            image = np.clip(
                mean_rgb.astype(np.float32) * (0.55 + 0.65 * density[..., None]),
                0,
                255,
            ).astype(np.uint8)
            image[~occupied] = 24
        file_name = f"band-{name}.png"
        save_png(output / file_name, image)
        images[file_name] = {"kind": f"ground-relative band [{lo}, {hi}) m"}

    (output / "evidence-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def render_elevation(
    index: CaptureIndex,
    output: Path,
    line: list[tuple[float, float]],
    *,
    corridor_width: float = 0.6,
    zrange: tuple[float, float] = (-3.0, 12.0),
    cell: float = 0.04,
    name: str = "elevation",
    every: int = 1,
) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    (x0, y0), (x1, y1) = line
    ux, uy = x1 - x0, y1 - y0
    length = math.hypot(ux, uy)
    if length < 0.05 or corridor_width <= 0 or cell <= 0 or every < 1:
        raise ValueError("elevation parameters are invalid")
    ux, uy = ux / length, uy / length
    half = corridor_width * 0.5
    z_min, z_max = zrange
    cols = max(1, int(math.ceil(length / cell)))
    rows = max(1, int(math.ceil((z_max - z_min) / cell)))
    rgb_sum = np.zeros((rows, cols, 3), dtype=np.uint64)
    hits = np.zeros((rows, cols), dtype=np.uint32)
    stats: dict[str, int] = {}
    bbox = (
        min(x0, x1) - half,
        min(y0, y1) - half,
        max(x0, x1) + half,
        max(y0, y1) + half,
    )
    has_color = bool(index.manifest.get("hasColor"))
    for points in index.iter_bbox(*bbox, z_min=z_min, z_max=z_max, every=every):
        _add_stats(stats, points.stats)
        x = points.x - x0
        y = points.y - y0
        along = x * ux + y * uy
        lateral = -x * uy + y * ux
        keep = (np.abs(lateral) <= half) & (along >= 0) & (along <= length)
        if not keep.any():
            continue
        col = np.minimum((along[keep] / cell).astype(np.int32), cols - 1)
        row = np.minimum(((z_max - points.z[keep]) / cell).astype(np.int32), rows - 1)
        np.add.at(hits, (row, col), 1)
        if has_color:
            np.add.at(rgb_sum, (row, col), points.rgb[keep].astype(np.uint64))

    image = np.full((rows, cols, 3), 24, dtype=np.uint8)
    occupied = hits > 0
    if occupied.any():
        if has_color:
            image[occupied] = (rgb_sum[occupied] // hits[occupied][..., None]).astype(np.uint8)
        else:
            image[occupied] = 190
    image_path = output / f"{name}.png"
    save_png(image_path, image)
    meta: dict[str, object] = {
        "kind": "indexed-pointcloud-elevation",
        "sourceKind": "capture-index",
        "index": str(index.root),
        "indexFingerprint": index.manifest["indexFingerprint"],
        "line": {
            "start": [x0, y0],
            "end": [x1, y1],
            "lengthM": length,
            "corridorWidthM": corridor_width,
        },
        "zRange": [z_min, z_max],
        "cellSizeM": cell,
        "widthPx": cols,
        "heightPx": rows,
        "queryStats": stats,
        "pixelToSource": "along_m = px*cellSizeM (from line start); z = zMax - py*cellSizeM",
    }
    (output / f"{name}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    overview = sub.add_parser("overview")
    overview.add_argument("--index", required=True, type=Path)
    overview.add_argument("--output", required=True, type=Path)
    overview.add_argument("--cell", type=float, default=0.08)
    overview.add_argument("--ground-cell", type=float, default=1.0)
    overview.add_argument("--bands", default=DEFAULT_BANDS)
    overview.add_argument("--crop", type=parse_crop)
    overview.add_argument("--every", type=int, default=1)
    overview.add_argument("--validate-source", action="store_true")

    elevation = sub.add_parser("elevation")
    elevation.add_argument("--index", required=True, type=Path)
    elevation.add_argument("--output", required=True, type=Path)
    elevation.add_argument("--line", required=True, type=parse_line)
    elevation.add_argument("--width", type=float, default=0.6)
    elevation.add_argument("--zrange", type=parse_point, default=(-3.0, 12.0))
    elevation.add_argument("--cell", type=float, default=0.04)
    elevation.add_argument("--name", default="elevation")
    elevation.add_argument("--every", type=int, default=1)
    elevation.add_argument("--validate-source", action="store_true")
    args = parser.parse_args()

    index = CaptureIndex.open(args.index, validate_source=args.validate_source)
    if args.command == "overview":
        result = render_overview(
            index,
            args.output,
            cell=args.cell,
            ground_cell=args.ground_cell,
            bands_text=args.bands,
            crop=args.crop,
            every=args.every,
        )
    else:
        result = render_elevation(
            index,
            args.output,
            args.line,
            corridor_width=args.width,
            zrange=args.zrange,
            cell=args.cell,
            name=args.name,
            every=args.every,
        )
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
