#!/usr/bin/env python3
"""Robust RANSAC floor/ceiling plane survey over a CaptureIndex.

Replaces the scalar-floorZ single point of failure: the survey fits
near-horizontal planes ``z = a*x + b*y + c`` so tilted slabs, mezzanines and
multi-storey captures produce explicit ranked candidates instead of one bad
histogram peak.  The output file is the survey consumed by
cleanroom_macro_builder.py: top-level ``floor`` / ``ceiling`` planes plus
``source.indexFingerprint`` binding, with fit evidence alongside.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np

CORE_DIR = Path(__file__).resolve().parent
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from capture_index import CaptureIndex, CaptureIndexError  # noqa: E402


def _plane_dict(a: float, b: float, c: float) -> dict[str, float]:
    return {"a": round(float(a), 8), "b": round(float(b), 8), "c": round(float(c), 6)}


def _tilt_deg(a: float, b: float) -> float:
    return math.degrees(math.atan(math.hypot(a, b)))


def _lstsq_plane(dx: np.ndarray, dy: np.ndarray, z: np.ndarray) -> tuple[float, float, float]:
    design = np.column_stack((dx, dy, np.ones(len(dx))))
    solution, *_ = np.linalg.lstsq(design, z, rcond=None)
    return float(solution[0]), float(solution[1]), float(solution[2])


def _ransac_horizontal_plane(
    dx: np.ndarray,
    dy: np.ndarray,
    z: np.ndarray,
    *,
    inlier_threshold_m: float,
    max_tilt_deg: float,
    iterations: int,
    rng: np.random.Generator,
) -> tuple[tuple[float, float, float], np.ndarray] | None:
    count = len(z)
    if count < 3:
        return None
    max_slope = math.tan(math.radians(max_tilt_deg))
    best_model = None
    best_inliers = None
    best_count = 0
    for _ in range(iterations):
        pick = rng.integers(0, count, size=3)
        if len(set(pick.tolist())) < 3:
            continue
        design = np.column_stack((dx[pick], dy[pick], np.ones(3)))
        try:
            a, b, c = np.linalg.solve(design, z[pick])
        except np.linalg.LinAlgError:
            continue
        # Tilt gate: refuses vertical wall triplets and steep accidental
        # planes through desk arrays before any inlier counting happens.
        if math.hypot(float(a), float(b)) > max_slope:
            continue
        residual = np.abs(z - (a * dx + b * dy + c))
        mask = residual <= inlier_threshold_m
        inliers = int(np.count_nonzero(mask))
        if inliers > best_count:
            best_count = inliers
            best_model = (float(a), float(b), float(c))
            best_inliers = mask
    if best_model is None or best_count < 3:
        return None
    # Two least-squares polish rounds over re-selected inliers.
    model, mask = best_model, best_inliers
    for _ in range(2):
        a, b, c = _lstsq_plane(dx[mask], dy[mask], z[mask])
        if math.hypot(a, b) > max_slope:
            break
        model = (a, b, c)
        mask = np.abs(z - (a * dx + b * dy + c)) <= inlier_threshold_m
        if int(np.count_nonzero(mask)) < 3:
            break
    return model, mask


def survey_levels(
    index: CaptureIndex,
    *,
    max_points: int = 1_500_000,
    inlier_threshold_m: float = 0.02,
    max_tilt_deg: float = 10.0,
    iterations: int = 400,
    max_planes: int = 5,
    min_support_ratio: float = 0.02,
    seed: int = 0,
) -> dict:
    if inlier_threshold_m <= 0 or max_tilt_deg <= 0 or iterations < 10 or max_planes < 1:
        raise ValueError("survey parameters are invalid")
    indexed = int(index.manifest["indexedPointCount"])
    stride = max(1, int(math.ceil(indexed / max_points)))
    query = index.query_all(every=stride)
    total = query.point_count
    if total < 500:
        raise ValueError(f"too few sampled points for a level survey ({total} < 500)")

    # Centered plan frame keeps the normal equations conditioned under large
    # projected coordinates; coefficients are mapped back to source frame.
    x0 = float(np.mean(query.x))
    y0 = float(np.mean(query.y))
    dx = query.x - x0
    dy = query.y - y0
    z = query.z.copy()

    rng = np.random.default_rng(seed)
    min_support = max(300, int(min_support_ratio * total))
    remaining = np.ones(total, dtype=bool)
    candidates: list[dict] = []
    for _ in range(max_planes):
        active = np.flatnonzero(remaining)
        if len(active) < min_support:
            break
        fit = _ransac_horizontal_plane(
            dx[active], dy[active], z[active],
            inlier_threshold_m=inlier_threshold_m,
            max_tilt_deg=max_tilt_deg,
            iterations=iterations, rng=rng,
        )
        if fit is None:
            break
        (a, b, c_centered), local_mask = fit
        support = int(np.count_nonzero(local_mask))
        if support < min_support:
            break
        member = active[local_mask]
        inlier_z = z[member]
        residual = np.abs(inlier_z - (a * dx[member] + b * dy[member] + c_centered))
        c_source = c_centered - a * x0 - b * y0
        candidates.append({
            "plane": _plane_dict(a, b, c_source),
            "supportPointCount": support,
            "inlierRatio": round(support / total, 5),
            "tiltDeg": round(_tilt_deg(a, b), 4),
            "medianZ": round(float(np.median(inlier_z)), 5),
            "residualP50M": round(float(np.percentile(residual, 50)), 6),
            "residualP90M": round(float(np.percentile(residual, 90)), 6),
        })
        remaining[member] = False
    if not candidates:
        raise ValueError("no near-horizontal plane candidate reached the support floor")

    candidates.sort(key=lambda item: -item["supportPointCount"])
    best_support = candidates[0]["supportPointCount"]
    # The floor is the LOWEST strong plane, not the densest: a ceiling or an
    # upper storey can out-support a partially occluded ground floor.
    strong = [item for item in candidates if item["supportPointCount"] >= 0.5 * best_support]
    floor = min(strong, key=lambda item: item["medianZ"])

    warnings: list[str] = []
    ceiling = None
    ceiling_pool = [
        item for item in candidates
        if item is not floor and 2.0 <= item["medianZ"] - floor["medianZ"] <= 4.5
    ]
    if ceiling_pool:
        ceiling = max(ceiling_pool, key=lambda item: item["supportPointCount"])
        ceiling_source = "ransac-plane"
        ceiling_plane = ceiling["plane"]
    else:
        # Consumer requires a ceiling plane; degrade to a parallel offset from
        # the height distribution rather than failing the whole survey.
        height = z - (
            float(floor["plane"]["a"]) * (dx + x0)
            + float(floor["plane"]["b"]) * (dy + y0)
            + float(floor["plane"]["c"])
        )
        offset = float(np.percentile(height, 98))
        if not 2.0 <= offset <= 4.5:
            offset = 2.9
            warnings.append("height distribution gave no plausible ceiling offset; defaulted to 2.9 m")
        ceiling_source = "parallel-fallback"
        ceiling_plane = _plane_dict(
            float(floor["plane"]["a"]), float(floor["plane"]["b"]),
            float(floor["plane"]["c"]) + offset,
        )
        warnings.append("no ceiling plane candidate; ceiling is a floor-parallel fallback")

    if floor["tiltDeg"] > 3.0:
        warnings.append(f"floor tilt {floor['tiltDeg']:.2f} deg is unusually high; review the capture leveling")

    alternatives = [item for item in candidates if item is not floor and item is not ceiling]
    manifest_path = index.root / "capture-index.json"
    fingerprint = index.manifest["indexFingerprint"]
    return {
        "schemaVersion": 1,
        "kind": "level-survey",
        "status": "DEGRADED" if warnings else "REVIEW_REQUIRED",
        # Consumer contract (cleanroom_macro_builder.build): floor / ceiling /
        # source.indexFingerprint.  The *Plane mirrors are the survey-report
        # names used by fit tooling; both refer to the same planes.
        "floor": floor["plane"],
        "ceiling": ceiling_plane,
        "floorPlane": floor["plane"],
        "ceilingPlane": ceiling_plane,
        "inlierRatio": floor["inlierRatio"],
        "supportPointCount": floor["supportPointCount"],
        "tiltDeg": floor["tiltDeg"],
        "residualP50M": floor["residualP50M"],
        "residualP90M": floor["residualP90M"],
        "ceilingSource": ceiling_source,
        "alternatives": alternatives,
        "indexFingerprint": fingerprint,
        "source": {
            "index": str(index.root),
            "indexFingerprint": fingerprint,
            "indexManifestSha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        },
        "sampling": {"stride": stride, "pointCount": total},
        "parameters": {
            "inlierThresholdM": inlier_threshold_m,
            "maxTiltDeg": max_tilt_deg,
            "iterations": iterations,
            "maxPlanes": max_planes,
            "minSupportPointCount": min_support,
            "seed": seed,
        },
        "warnings": warnings,
        "authorityRule": "raw plane survey; an Agent must review candidates before downstream use",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-points", type=int, default=1_500_000)
    parser.add_argument("--threshold", type=float, default=0.02)
    parser.add_argument("--max-tilt", type=float, default=10.0)
    parser.add_argument("--iterations", type=int, default=400)
    parser.add_argument("--max-planes", type=int, default=5)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--validate-source", action="store_true")
    args = parser.parse_args()
    index = CaptureIndex.open(args.index, validate_source=args.validate_source)
    result = survey_levels(
        index,
        max_points=args.max_points,
        inlier_threshold_m=args.threshold,
        max_tilt_deg=args.max_tilt,
        iterations=args.iterations,
        max_planes=args.max_planes,
        seed=args.seed,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CaptureIndexError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
