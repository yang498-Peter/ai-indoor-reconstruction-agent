#!/usr/bin/env python3
"""Fit an indoor floor plane from a read-only CaptureIndex.

The estimator works on one low-surface sample per plan cell so furniture and
wall density cannot dominate the fit.  It writes a review-required survey for
``cleanroom_macro_builder.py``; it never edits source data or Scene V2.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from capture_index import CaptureIndex


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fit_least_squares(x: np.ndarray, y: np.ndarray, z: np.ndarray) -> np.ndarray:
    design = np.column_stack((x, y, np.ones_like(x)))
    coefficients, *_ = np.linalg.lstsq(design, z, rcond=None)
    return coefficients.astype(np.float64)


def cell_low_surface(
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray,
    *,
    cell_size_m: float,
    percentile: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    min_x, min_y = float(np.min(x)), float(np.min(y))
    ix = np.floor((x - min_x) / cell_size_m).astype(np.int64)
    iy = np.floor((y - min_y) / cell_size_m).astype(np.int64)
    width = int(ix.max(initial=0)) + 1
    keys = iy * width + ix
    order = np.argsort(keys, kind="stable")
    sorted_keys = keys[order]
    starts = np.flatnonzero(np.r_[True, sorted_keys[1:] != sorted_keys[:-1]])
    ends = np.r_[starts[1:], len(order)]
    cell_x: list[float] = []
    cell_y: list[float] = []
    cell_z: list[float] = []
    for start, end in zip(starts, ends):
        members = order[start:end]
        if len(members) < 3:
            continue
        cell_x.append(float(np.median(x[members])))
        cell_y.append(float(np.median(y[members])))
        cell_z.append(float(np.percentile(z[members], percentile)))
    return np.asarray(cell_x), np.asarray(cell_y), np.asarray(cell_z)


def robust_plane(
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray,
    *,
    seed: int = 155,
    trials: int = 1200,
    inlier_tolerance_m: float = 0.035,
    max_slope: float = 0.18,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if len(x) < 30:
        raise ValueError("too few low-surface cells for a floor plane")
    rng = np.random.default_rng(seed)
    best_mask: np.ndarray | None = None
    best_score = (-1, -math.inf)
    for _ in range(trials):
        sample = rng.choice(len(x), size=3, replace=False)
        design = np.column_stack((x[sample], y[sample], np.ones(3)))
        if abs(float(np.linalg.det(design))) < 1e-7:
            continue
        coefficients = np.linalg.solve(design, z[sample])
        if math.hypot(float(coefficients[0]), float(coefficients[1])) > max_slope:
            continue
        residual = np.abs(z - (coefficients[0] * x + coefficients[1] * y + coefficients[2]))
        mask = residual <= inlier_tolerance_m
        count = int(mask.sum())
        median = float(np.median(residual[mask])) if count else math.inf
        score = (count, -median)
        if score > best_score:
            best_score, best_mask = score, mask
    if best_mask is None or int(best_mask.sum()) < 30:
        raise ValueError("no stable floor plane found")
    coefficients = _fit_least_squares(x[best_mask], y[best_mask], z[best_mask])
    for _ in range(4):
        residual = z - (coefficients[0] * x + coefficients[1] * y + coefficients[2])
        center = float(np.median(residual[best_mask]))
        mad = float(np.median(np.abs(residual[best_mask] - center)))
        tolerance = max(inlier_tolerance_m, 3.5 * 1.4826 * mad)
        best_mask = np.abs(residual - center) <= tolerance
        coefficients = _fit_least_squares(x[best_mask], y[best_mask], z[best_mask])
    residual = z - (coefficients[0] * x + coefficients[1] * y + coefficients[2])
    return coefficients, best_mask, residual


def _parse_crop(value: str) -> tuple[float, float, float, float]:
    values = tuple(float(item) for item in value.split(","))
    if len(values) != 4 or values[0] >= values[2] or values[1] >= values[3]:
        raise argparse.ArgumentTypeError("crop must be minX,minY,maxX,maxY")
    return values


def build_survey(
    index_root: Path,
    output: Path,
    crop: tuple[float, float, float, float],
    *,
    cell_size_m: float,
    low_percentile: float,
    ceiling_height_m: float,
    floor_z_seed: float | None,
    floor_seed_window_m: float,
    every: int,
    seed: int,
) -> dict[str, object]:
    index = CaptureIndex.open(index_root, validate_source=True)
    query = index.query_bbox(*crop, every=every)
    if query.point_count < 10_000:
        raise ValueError("crop contains too few indexed points")
    fit_x, fit_y, fit_z = query.x, query.y, query.z
    if floor_z_seed is not None:
        seed_mask = np.abs(fit_z - floor_z_seed) <= floor_seed_window_m
        fit_x, fit_y, fit_z = fit_x[seed_mask], fit_y[seed_mask], fit_z[seed_mask]
        if len(fit_x) < 10_000:
            raise ValueError("floor seed window contains too few indexed points")
    cell_x, cell_y, cell_z = cell_low_surface(
        fit_x, fit_y, fit_z, cell_size_m=cell_size_m, percentile=low_percentile,
    )
    coefficients, mask, residual = robust_plane(cell_x, cell_y, cell_z, seed=seed)
    inlier_residual = np.abs(residual[mask])
    a, b, c = (float(value) for value in coefficients)
    normal = np.asarray([-a, -b, 1.0], dtype=np.float64)
    normal /= np.linalg.norm(normal)
    manifest_path = index.root / "capture-index.json"
    plane = {
        "a": round(a, 10), "b": round(b, 10), "c": round(c, 10),
        "equation": "z = a*x + b*y + c",
        "normal": [round(float(value), 10) for value in normal],
    }
    survey = {
        "schemaVersion": 1,
        "kind": "raw-index-plane-level-survey",
        "status": "REVIEW_REQUIRED",
        "source": {
            "indexFingerprint": index.manifest["indexFingerprint"],
            "indexManifestSha256": _sha256(manifest_path),
            "sourceIdentity": index.manifest["sourceIdentity"],
            "crop": list(crop),
            "queriedPointCount": query.point_count,
        },
        "parameters": {
            "cellSizeM": cell_size_m,
            "lowPercentile": low_percentile,
            "floorZSeed": floor_z_seed,
            "floorSeedWindowM": floor_seed_window_m,
            "samplingEvery": every,
            "seed": seed,
        },
        "floor": {
            **plane,
            "candidateCellCount": int(len(cell_x)),
            "inlierCellCount": int(mask.sum()),
            "inlierRatio": round(float(mask.mean()), 6),
            "residualP50M": round(float(np.percentile(inlier_residual, 50)), 6),
            "residualP90M": round(float(np.percentile(inlier_residual, 90)), 6),
            "residualMaxM": round(float(np.max(inlier_residual)), 6),
        },
        "ceiling": {
            "a": round(a, 10), "b": round(b, 10),
            "c": round(c + ceiling_height_m, 10),
            "equation": "z = a*x + b*y + c",
            "normal": [round(float(value), 10) for value in normal],
            "heightAboveFloorM": ceiling_height_m,
            "status": "SUPPORTED_INFERRED_PARALLEL_PLANE",
        },
        "authorityRule": "Agent must inspect plane-relative evidence before accepting the level survey",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(survey, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return survey


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--crop", required=True, type=_parse_crop)
    parser.add_argument("--cell", type=float, default=0.14)
    parser.add_argument("--low-percentile", type=float, default=8.0)
    parser.add_argument("--ceiling-height", type=float, default=2.51)
    parser.add_argument("--floor-z-seed", type=float)
    parser.add_argument("--floor-seed-window", type=float, default=0.16)
    parser.add_argument("--every", type=int, default=2)
    parser.add_argument("--seed", type=int, default=155)
    args = parser.parse_args()
    survey = build_survey(
        args.index, args.output, args.crop, cell_size_m=args.cell,
        low_percentile=args.low_percentile, ceiling_height_m=args.ceiling_height,
        floor_z_seed=args.floor_z_seed, floor_seed_window_m=args.floor_seed_window,
        every=args.every, seed=args.seed,
    )
    print(json.dumps({"ok": True, "output": str(args.output), "floor": survey["floor"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
