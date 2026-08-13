#!/usr/bin/env python3
"""Generic point-cloud evidence generator (indoor and outdoor).

Streams a LAS/LAZ once per pass (chunked, memory-safe for hundreds of
millions of points) and produces the standard evidence set the agent
workflow consumes:

  overview  - colored top orthophoto (top surface and ground surface),
              elevation colormap, and ground-relative height-band slices
              (default bands suit both rooms and building sites);
  elevation - a vertical cross-section along an arbitrary plan line.

Every image ships with a metadata JSON entry that binds pixel coordinates
to SOURCE plan meters, so a pick on the image converts losslessly into
scene_api coordinates:

  source_x = originX + px * cellSizeM
  source_y = originY - py * cellSizeM      (row 0 is the +Y / north edge)

The capture stays read-only; all outputs go to --output.
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

from PIL import Image

CHUNK = 2_000_000
DEFAULT_BANDS = "ground=-0.30:0.30,low=0.30:1.00,walls=1.00:2.60,high=2.60:6.00,canopy=6.00:40.00"


def parse_bands(text: str) -> list[tuple[str, float, float]]:
    bands = []
    for chunk in text.split(","):
        name, _, span = chunk.partition("=")
        lo, _, hi = span.partition(":")
        bands.append((name.strip(), float(lo), float(hi)))
    return bands


def color_scale(points: "laspy.ScaleAwarePointRecord") -> np.ndarray | None:
    if "red" not in points.point_format.dimension_names:
        return None
    red = np.asarray(points.red, dtype=np.uint16)
    # LAS colors are nominally 16-bit; some writers store 8-bit values.
    shift = 8 if red.max(initial=0) > 255 else 0
    return np.stack([
        np.asarray(points.red) >> shift,
        np.asarray(points.green) >> shift,
        np.asarray(points.blue) >> shift,
    ], axis=1).astype(np.uint8)


def save_png(path: Path, array: np.ndarray) -> None:
    Image.fromarray(array).save(path)


def elevation_colormap(values: np.ndarray, valid: np.ndarray) -> np.ndarray:
    out = np.zeros((*values.shape, 3), dtype=np.uint8)
    if valid.any():
        lo = float(np.percentile(values[valid], 2))
        hi = float(np.percentile(values[valid], 98))
        span = max(hi - lo, 1e-6)
        t = np.clip((values - lo) / span, 0, 1)
        # Compact blue->green->yellow->red ramp without a matplotlib import.
        r = np.clip(1.5 * t - 0.25, 0, 1)
        g = np.clip(1.0 - np.abs(2.0 * t - 1.0) * 0.7 + 0.15, 0, 1)
        b = np.clip(1.0 - 1.6 * t, 0, 1)
        ramp = (np.stack([r, g, b], axis=-1) * 255).astype(np.uint8)
        out[valid] = ramp[valid]
    return out


class Grid:
    def __init__(self, min_x: float, max_y: float, width: int, height: int, cell: float):
        self.min_x = min_x
        self.max_y = max_y
        self.width = width
        self.height = height
        self.cell = cell

    def indices(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        col = ((x - self.min_x) / self.cell).astype(np.int32)
        row = ((self.max_y - y) / self.cell).astype(np.int32)
        ok = (col >= 0) & (col < self.width) & (row >= 0) & (row < self.height)
        return row[ok], col[ok], ok

    def meta(self) -> dict:
        return {
            "originX": self.min_x,
            "originY": self.max_y,
            "cellSizeM": self.cell,
            "widthPx": self.width,
            "heightPx": self.height,
            "pixelToSource": "source_x = originX + px*cellSizeM; source_y = originY - py*cellSizeM",
        }


def chunked_points(las_path: Path, every: int):
    with laspy.open(las_path) as reader:
        for points in reader.chunk_iterator(CHUNK):
            if every > 1:
                points = points[::every]
            yield points


def cmd_overview(args) -> int:
    las_path: Path = args.las
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)
    bands = parse_bands(args.bands)

    with laspy.open(las_path) as reader:
        header = reader.header
        min_x, min_y, _ = header.mins
        max_x, max_y, _ = header.maxs
        total = header.point_count
    if args.crop:
        min_x, min_y, max_x, max_y = args.crop
    cell = args.cell
    width = max(1, int(math.ceil((max_x - min_x) / cell)))
    height = max(1, int(math.ceil((max_y - min_y) / cell)))
    if width * height > 60_000_000:
        raise SystemExit(f"grid {width}x{height} too large; raise --cell")
    grid = Grid(min_x, max_y, width, height, cell)

    # Pass 1: ground surface (low percentile z per coarse cell) + top surface.
    top_z = np.full((height, width), -np.inf, dtype=np.float32)
    top_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    low_z = np.full((height, width), np.inf, dtype=np.float32)
    low_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    count = np.zeros((height, width), dtype=np.uint32)

    for points in chunked_points(las_path, args.every):
        x = np.asarray(points.x)
        y = np.asarray(points.y)
        z = np.asarray(points.z, dtype=np.float32)
        rgb = color_scale(points)
        row, col, ok = grid.indices(x, y)
        z_ok = z[ok]
        rgb_ok = rgb[ok] if rgb is not None else None
        # np.maximum.at handles duplicate cells; color needs an argmax pass.
        order = np.argsort(z_ok, kind="stable")  # ascending: later wins for top
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
    # Smooth ground reference on a coarse grid so single low outliers do not
    # poison the band classification.
    coarse = max(1, int(round(args.ground_cell / cell)))
    coarse_h = math.ceil(height / coarse)
    coarse_w = math.ceil(width / coarse)
    ground = np.full((coarse_h, coarse_w), np.nan, dtype=np.float32)
    for r in range(coarse_h):
        rows = slice(r * coarse, min((r + 1) * coarse, height))
        for c in range(coarse_w):
            cols = slice(c * coarse, min((c + 1) * coarse, width))
            block = low_z[rows, cols]
            block = block[np.isfinite(block)]
            if block.size:
                ground[r, c] = np.percentile(block, 10)
    # Fill holes with the global median so relative bands stay defined.
    global_ground = float(np.nanmedian(ground)) if np.isfinite(ground).any() else 0.0
    ground = np.where(np.isnan(ground), global_ground, ground)

    def ground_at(row: np.ndarray, col: np.ndarray) -> np.ndarray:
        return ground[np.minimum(row // coarse, coarse_h - 1), np.minimum(col // coarse, coarse_w - 1)]

    # Pass 2: ground-relative band accumulation.
    band_count = [np.zeros((height, width), dtype=np.uint32) for _ in bands]
    band_rgb_sum = [np.zeros((height, width, 3), dtype=np.uint64) for _ in bands]
    for points in chunked_points(las_path, args.every):
        x = np.asarray(points.x)
        y = np.asarray(points.y)
        z = np.asarray(points.z, dtype=np.float32)
        rgb = color_scale(points)
        row, col, ok = grid.indices(x, y)
        z_rel = z[ok] - ground_at(row, col)
        rgb_ok = rgb[ok] if rgb is not None else None
        for index, (_, lo, hi) in enumerate(bands):
            mask = (z_rel >= lo) & (z_rel < hi)
            np.add.at(band_count[index], (row[mask], col[mask]), 1)
            if rgb_ok is not None:
                np.add.at(band_rgb_sum[index], (row[mask], col[mask]), rgb_ok[mask].astype(np.uint64))

    manifest = {"las": las_path.name, "pointCount": total, "decimation": args.every,
                "bounds": {"minX": min_x, "minY": min_y, "maxX": max_x, "maxY": max_y},
                "groundReference": "per-cell 10th percentile of lowest returns, "
                                   f"{args.ground_cell} m blocks; global median fallback",
                "grid": grid.meta(), "images": {}}

    top_img = np.where(valid[..., None], top_rgb, 24).astype(np.uint8)
    save_png(output / "ortho-top.png", top_img)
    manifest["images"]["ortho-top.png"] = {"kind": "top-surface color (roofs & canopy)"}

    low_img = np.where(valid[..., None], low_rgb, 24).astype(np.uint8)
    save_png(output / "ortho-ground.png", low_img)
    manifest["images"]["ortho-ground.png"] = {"kind": "lowest-surface color (ground under canopy)"}

    save_png(output / "heightmap.png", elevation_colormap(np.where(valid, top_z, np.nan), valid))
    manifest["images"]["heightmap.png"] = {"kind": "top elevation colormap blue(low)->red(high), 2-98 percentile"}

    for index, (name, lo, hi) in enumerate(bands):
        counts = band_count[index]
        occupied = counts > 0
        image = np.zeros((height, width, 3), dtype=np.uint8)
        image[...] = 24
        if occupied.any():
            mean_rgb = np.zeros_like(image)
            mean_rgb[occupied] = (band_rgb_sum[index][occupied] // counts[occupied][..., None]).astype(np.uint8)
            # Density-weighted brightness keeps solid structure readable
            # against sparse vegetation noise in the same band, with a floor
            # so single-return pixels stay visible for picking.
            density = np.clip(counts / max(1.0, float(np.percentile(counts[occupied], 90))), 0.3, 1.0)
            image = np.clip(mean_rgb.astype(np.float32) * (0.55 + 0.65 * density[..., None]), 0, 255).astype(np.uint8)
            image[~occupied] = 24
        file_name = f"band-{name}.png"
        save_png(output / file_name, image)
        manifest["images"][file_name] = {"kind": f"ground-relative band [{lo}, {hi}) m mean color x density"}

    (output / "evidence-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output), "grid": grid.meta(),
                      "images": list(manifest["images"])}, ensure_ascii=False))
    return 0


def cmd_elevation(args) -> int:
    las_path: Path = args.las
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)
    (x0, y0), (x1, y1) = args.line
    ux, uy = x1 - x0, y1 - y0
    length = math.hypot(ux, uy)
    if length < 0.05:
        raise SystemExit("line too short")
    ux, uy = ux / length, uy / length
    half = args.width / 2

    cell = args.cell
    z_min, z_max = args.zrange
    cols = max(1, int(math.ceil(length / cell)))
    rows = max(1, int(math.ceil((z_max - z_min) / cell)))
    rgb_img = np.zeros((rows, cols, 3), dtype=np.uint64)
    hits = np.zeros((rows, cols), dtype=np.uint32)

    for points in chunked_points(las_path, args.every):
        x = np.asarray(points.x) - x0
        y = np.asarray(points.y) - y0
        z = np.asarray(points.z, dtype=np.float32)
        along = x * ux + y * uy
        lateral = -x * uy + y * ux
        keep = (np.abs(lateral) <= half) & (along >= 0) & (along <= length) & (z >= z_min) & (z < z_max)
        if not keep.any():
            continue
        col = np.minimum((along[keep] / cell).astype(np.int32), cols - 1)
        row = np.minimum(((z_max - z[keep]) / cell).astype(np.int32), rows - 1)
        np.add.at(hits, (row, col), 1)
        rgb = color_scale(points)
        if rgb is not None:
            np.add.at(rgb_img, (row, col), rgb[keep].astype(np.uint64))

    image = np.zeros((rows, cols, 3), dtype=np.uint8)
    image[...] = 24
    occupied = hits > 0
    if occupied.any():
        image[occupied] = (rgb_img[occupied] // hits[occupied][..., None]).astype(np.uint8)
    name = args.name or "elevation"
    save_png(output / f"{name}.png", image)
    meta = {
        "line": {"start": [x0, y0], "end": [x1, y1], "lengthM": length, "corridorWidthM": args.width},
        "zRange": [z_min, z_max], "cellSizeM": cell, "widthPx": cols, "heightPx": rows,
        "pixelToSource": "along_m = px*cellSizeM (from line start); z = zMax - py*cellSizeM",
    }
    (output / f"{name}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "image": str(output / (name + '.png')), **meta}, ensure_ascii=False))
    return 0


def parse_point(text: str) -> tuple[float, float]:
    x, y = (float(v) for v in text.split(","))
    return (x, y)


def parse_line(text: str) -> list[tuple[float, float]]:
    parts = [parse_point(chunk) for chunk in text.split(";")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("expected x0,y0;x1,y1")
    return parts


def parse_crop(text: str) -> tuple[float, float, float, float]:
    a, b, c, d = (float(v) for v in text.split(","))
    return (a, b, c, d)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("overview", help="orthophotos + heightmap + ground-relative band slices")
    p.add_argument("--las", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--cell", type=float, default=0.08, help="plan cell size in meters")
    p.add_argument("--ground-cell", type=float, default=1.0, help="ground reference block size in meters")
    p.add_argument("--bands", default=DEFAULT_BANDS)
    p.add_argument("--crop", type=parse_crop, help="minX,minY,maxX,maxY plan crop")
    p.add_argument("--every", type=int, default=1, help="keep every Nth point (decimation)")

    p = sub.add_parser("elevation", help="vertical cross-section along a plan line")
    p.add_argument("--las", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--line", required=True, type=parse_line, help="x0,y0;x1,y1 source meters")
    p.add_argument("--width", type=float, default=0.6, help="corridor width in meters")
    p.add_argument("--zrange", type=parse_point, default=(-3.0, 12.0), help="zmin,zmax")
    p.add_argument("--cell", type=float, default=0.04)
    p.add_argument("--name")
    p.add_argument("--every", type=int, default=1)

    args = parser.parse_args()
    return cmd_overview(args) if args.command == "overview" else cmd_elevation(args)


if __name__ == "__main__":
    raise SystemExit(main())
