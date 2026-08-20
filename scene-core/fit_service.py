#!/usr/bin/env python3
"""Robust wall-line refinement service for Agent-authored rough geometry.

An Agent draws a rough centerline from visual evidence (allowed to be off by
about 0.3 m laterally and 8 degrees); this service pulls that line back onto
the real wall face using raw CaptureIndex points and reports measurement
evidence (support, residuals, inlier ratio, double-face pairing).  It never
writes Scene V2 -- the algorithm measures, the Agent decides.

Residual field names follow the pointcloud_scene_metrics convention
(residualP50M / residualP90M, meters).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import numpy as np

CORE_DIR = Path(__file__).resolve().parent
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from capture_index import CaptureIndex, CaptureIndexError  # noqa: E402


STATUS_FIT_OK = "FIT_OK"
STATUS_LOW_SUPPORT = "LOW_SUPPORT"
STATUS_AMBIGUOUS = "AMBIGUOUS"

# Wall thickness search window for opposite-face pairing, matching the
# structural_proposals pairing defaults.
MIN_THICKNESS_M = 0.07
MAX_THICKNESS_M = 0.45


def _point(value: np.ndarray) -> list[float]:
    return [round(float(value[0]), 6), round(float(value[1]), 6)]


def _as_xy(value, name: str) -> np.ndarray:
    point = np.asarray(list(value), dtype=np.float64)
    if point.shape != (2,) or not np.isfinite(point).all():
        raise ValueError(f"{name} must be a finite plan point [x, y]")
    return point


def _plane_z(plane: dict, x, y):
    return float(plane["a"]) * x + float(plane["b"]) * y + float(plane["c"])


def _validate_floor(floor_z, floor_plane) -> dict | None:
    if floor_plane is not None:
        if not isinstance(floor_plane, dict) or not all(k in floor_plane for k in ("a", "b", "c")):
            raise ValueError("floor_plane must be a {a, b, c} dict for z = a*x + b*y + c")
        values = [float(floor_plane[k]) for k in ("a", "b", "c")]
        if not all(math.isfinite(v) for v in values):
            raise ValueError("floor_plane coefficients must be finite")
        return {"a": values[0], "b": values[1], "c": values[2]}
    if floor_z is None:
        raise ValueError("either floor_z or floor_plane is required")
    if not math.isfinite(float(floor_z)):
        raise ValueError("floor_z must be finite")
    return None


def _gather_band_points(
    index: CaptureIndex,
    start: np.ndarray,
    end: np.ndarray,
    *,
    floor_z,
    floor_plane,
    band: tuple[float, float],
    half_width_m: float,
    along_margin_m: float,
    max_points: int,
) -> tuple[np.ndarray, dict]:
    """Return (N, 2) plan points inside the structural band and rough corridor."""
    band_lo, band_hi = float(band[0]), float(band[1])
    if band_lo < 0 or band_hi <= band_lo:
        raise ValueError("structural height band is invalid")
    plane = _validate_floor(floor_z, floor_plane)

    vector = end - start
    length = float(np.linalg.norm(vector))
    if length < 0.3:
        raise ValueError("rough line is too short (< 0.3 m)")
    unit = vector / length
    normal = np.asarray([-unit[1], unit[0]], dtype=np.float64)

    margin = half_width_m + along_margin_m
    min_x = min(start[0], end[0]) - margin
    max_x = max(start[0], end[0]) + margin
    min_y = min(start[1], end[1]) - margin
    max_y = max(start[1], end[1]) + margin

    if plane is None:
        z_lo = float(floor_z) + band_lo
        z_hi = float(floor_z) + band_hi
    else:
        # The tilted plane varies across the query bbox; widen the coarse
        # z-filter to the corner extremes and re-filter per point below.
        corners = [(min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y)]
        corner_z = [_plane_z(plane, cx, cy) for cx, cy in corners]
        z_lo = min(corner_z) + band_lo
        z_hi = max(corner_z) + band_hi

    indexed = int(index.manifest["indexedPointCount"])
    stride = max(1, int(math.ceil(indexed / max_points)))
    query = index.query_bbox(min_x, min_y, max_x, max_y, z_min=z_lo, z_max=z_hi, every=stride)

    xy = np.column_stack((query.x, query.y))
    keep = np.ones(len(xy), dtype=bool)
    if plane is not None and len(xy):
        height = query.z - _plane_z(plane, query.x, query.y)
        keep &= (height >= band_lo) & (height <= band_hi)
    if len(xy):
        relative = xy - start
        along = relative @ unit
        lateral = relative @ normal
        keep &= (along >= -along_margin_m) & (along <= length + along_margin_m)
        keep &= np.abs(lateral) <= half_width_m
    xy = xy[keep]
    meta = {
        "stride": stride,
        "gatheredPointCount": int(len(xy)),
        "roughUnit": unit,
        "roughNormal": normal,
        "roughLengthM": length,
        "queryStats": query.stats,
    }
    return xy, meta


def _ransac_line(
    xy: np.ndarray,
    rough_unit: np.ndarray,
    *,
    angle_tolerance_deg: float,
    inlier_threshold_m: float,
    iterations: int,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray, int] | None:
    """Best (point, unit, inlier_count) among angle-constrained pair models."""
    count = len(xy)
    if count < 2:
        return None
    tolerance = math.radians(angle_tolerance_deg)
    rough_angle = math.atan2(float(rough_unit[1]), float(rough_unit[0])) % math.pi
    best = None
    for _ in range(iterations):
        i, j = rng.integers(0, count, size=2)
        if i == j:
            continue
        delta = xy[j] - xy[i]
        span = float(np.linalg.norm(delta))
        if span < 0.4:
            continue
        angle = math.atan2(float(delta[1]), float(delta[0])) % math.pi
        angle_delta = abs((angle - rough_angle) % math.pi)
        if min(angle_delta, math.pi - angle_delta) > tolerance:
            continue
        unit = delta / span
        normal = np.asarray([-unit[1], unit[0]], dtype=np.float64)
        residual = np.abs((xy - xy[i]) @ normal)
        inliers = int(np.count_nonzero(residual <= inlier_threshold_m))
        if best is None or inliers > best[2]:
            best = (xy[i].copy(), unit.copy(), inliers)
    return best


def _irls_line(
    xy: np.ndarray,
    anchor: np.ndarray,
    unit: np.ndarray,
    *,
    seed_window_m: float,
    iterations: int = 8,
) -> tuple[np.ndarray, np.ndarray]:
    """Tukey-biweight iteratively reweighted total-least-squares line fit.

    The robust scale is capped at 3 cm: a wall face is a thin sheet, and an
    uncapped MAD over a corridor containing the opposite face or furniture
    would widen the weight window until the fit drifts to the corridor mean.
    """
    normal = np.asarray([-unit[1], unit[0]], dtype=np.float64)
    residual = np.abs((xy - anchor) @ normal)
    subset = xy[residual <= seed_window_m]
    if len(subset) < 8:
        return anchor, unit
    center = subset.mean(axis=0)
    for _ in range(iterations):
        normal = np.asarray([-unit[1], unit[0]], dtype=np.float64)
        r = (subset - center) @ normal
        mad = float(np.median(np.abs(r - np.median(r))))
        scale = min(max(1.4826 * mad, 0.006), 0.03)
        t = r / (4.685 * scale)
        weights = np.where(np.abs(t) < 1.0, (1.0 - t * t) ** 2, 0.0)
        total = float(weights.sum())
        if total < 8.0:
            break
        center = (subset * weights[:, None]).sum(axis=0) / total
        centered = subset - center
        cov = (centered * weights[:, None]).T @ centered / total
        values, vectors = np.linalg.eigh(cov)
        fitted = vectors[:, int(np.argmax(values))]
        if float(np.dot(fitted, unit)) < 0:
            fitted = -fitted
        unit = fitted
    return center, unit


def _pca_line(xy: np.ndarray, rough_unit: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    center = xy.mean(axis=0)
    cov = np.cov((xy - center).T)
    values, vectors = np.linalg.eigh(cov)
    unit = vectors[:, int(np.argmax(values))]
    if float(np.dot(unit, rough_unit)) < 0:
        unit = -unit
    return center, unit


def _lateral_peaks(lateral: np.ndarray, *, bin_m: float = 0.02) -> list[dict]:
    """Density peaks of signed lateral offsets, merged within 5 cm."""
    if len(lateral) == 0:
        return []
    lo = float(np.min(lateral)) - bin_m
    hi = float(np.max(lateral)) + bin_m
    bins = max(8, int(math.ceil((hi - lo) / bin_m)))
    counts, edges = np.histogram(lateral, bins=bins, range=(lo, hi))
    kernel = np.asarray([1, 2, 3, 2, 1], dtype=np.float64)
    smooth = np.convolve(counts.astype(np.float64), kernel / kernel.sum(), mode="same")
    peaks: list[dict] = []
    for i in range(1, len(smooth) - 1):
        if smooth[i] >= smooth[i - 1] and smooth[i] >= smooth[i + 1] and smooth[i] > 0:
            center = float((edges[i] + edges[i + 1]) * 0.5)
            support = int(np.count_nonzero(np.abs(lateral - center) <= 0.05))
            peaks.append({"offsetM": center, "supportPointCount": support})
    peaks.sort(key=lambda item: -item["supportPointCount"])
    merged: list[dict] = []
    for peak in peaks:
        if all(abs(peak["offsetM"] - kept["offsetM"]) > 0.05 for kept in merged):
            merged.append(peak)
    return merged


def line_deviation(a_start, a_end, b_start, b_end) -> dict:
    """Deviation report between two plan segments (a = reference, b = refined)."""
    a0, a1 = _as_xy(a_start, "a_start"), _as_xy(a_end, "a_end")
    b0, b1 = _as_xy(b_start, "b_start"), _as_xy(b_end, "b_end")
    a_vec, b_vec = a1 - a0, b1 - b0
    a_len, b_len = float(np.linalg.norm(a_vec)), float(np.linalg.norm(b_vec))
    if a_len < 1e-9 or b_len < 1e-9:
        raise ValueError("deviation requires two non-degenerate segments")
    a_angle = math.atan2(float(a_vec[1]), float(a_vec[0])) % math.pi
    b_angle = math.atan2(float(b_vec[1]), float(b_vec[0])) % math.pi
    delta = abs((a_angle - b_angle) % math.pi)
    unit = a_vec / a_len
    normal = np.asarray([-unit[1], unit[0]], dtype=np.float64)
    mid_b = (b0 + b1) * 0.5
    return {
        "startDeltaM": round(float(np.linalg.norm(b0 - a0)), 5),
        "endDeltaM": round(float(np.linalg.norm(b1 - a1)), 5),
        "midLateralM": round(float(np.dot(mid_b - a0, normal)), 5),
        "angleDeltaDeg": round(math.degrees(min(delta, math.pi - delta)), 4),
        "lengthDeltaM": round(b_len - a_len, 5),
    }


def refine_wall_line(
    index: CaptureIndex,
    start,
    end,
    *,
    floor_z: float | None = None,
    floor_plane: dict | None = None,
    band: tuple[float, float] = (0.5, 2.4),
    corridor_m: float = 0.35,
    robust: bool = True,
    search_margin_m: float = 0.45,
    angle_tolerance_deg: float = 12.0,
    inlier_threshold_m: float = 0.05,
    min_support: int = 60,
    max_points: int = 400_000,
    seed: int = 0,
) -> dict:
    """Refine a rough Agent line onto the real wall face and report evidence."""
    rough_start = _as_xy(start, "start")
    rough_end = _as_xy(end, "end")
    if not 0.0 < corridor_m <= 1.5:
        raise ValueError("corridor_m must be in (0, 1.5]")
    half_width = corridor_m + search_margin_m
    xy, meta = _gather_band_points(
        index, rough_start, rough_end,
        floor_z=floor_z, floor_plane=floor_plane, band=band,
        half_width_m=half_width, along_margin_m=0.5, max_points=max_points,
    )
    parameters = {
        "band": [float(band[0]), float(band[1])],
        "corridorM": corridor_m,
        "searchMarginM": search_margin_m,
        "robust": bool(robust),
        "inlierThresholdM": inlier_threshold_m,
        "floorZ": None if floor_z is None else float(floor_z),
        "floorPlane": floor_plane,
    }
    base = {
        "kind": "wall-line-fit",
        "input": {"start": _point(rough_start), "end": _point(rough_end)},
        "parameters": parameters,
        "sampling": {"stride": meta["stride"], "gatheredPointCount": meta["gatheredPointCount"]},
        "indexFingerprint": index.manifest["indexFingerprint"],
        "authorityRule": "measurement only; this service never writes Scene V2",
    }
    rough_unit = meta["roughUnit"]

    if len(xy) < min_support:
        return {
            **base,
            "status": STATUS_LOW_SUPPORT,
            "reason": f"only {len(xy)} band points inside the search corridor (< {min_support})",
            "start": _point(rough_start), "end": _point(rough_end),
            "supportPointCount": int(len(xy)), "inlierRatio": 0.0,
            "residualP50M": None, "residualP90M": None,
            "doubleSided": {"detected": False, "thicknessCandidatesM": []},
        }

    if robust:
        rng = np.random.default_rng(seed)
        model = _ransac_line(
            xy, rough_unit,
            angle_tolerance_deg=angle_tolerance_deg,
            inlier_threshold_m=inlier_threshold_m,
            iterations=300, rng=rng,
        )
        if model is None:
            center, unit = _pca_line(xy, rough_unit)
        else:
            center, unit = _irls_line(xy, model[0], model[1], seed_window_m=3.0 * inlier_threshold_m)
    else:
        center, unit = _pca_line(xy, rough_unit)
    if float(np.dot(unit, rough_unit)) < 0:
        unit = -unit
    normal = np.asarray([-unit[1], unit[0]], dtype=np.float64)

    lateral = (xy - center) @ normal
    inlier_mask = np.abs(lateral) <= inlier_threshold_m
    inliers = xy[inlier_mask]

    # Endpoint extent: inlier percentiles, restricted to the Agent's intended
    # along-range so a long continuous wall does not silently extend the claim.
    along_all = (xy - center) @ unit
    rough_lo = float(np.dot(rough_start - center, unit))
    rough_hi = float(np.dot(rough_end - center, unit))
    rough_lo, rough_hi = min(rough_lo, rough_hi), max(rough_lo, rough_hi)
    extent_mask = inlier_mask & (along_all >= rough_lo - 0.3) & (along_all <= rough_hi + 0.3)
    extent_along = along_all[extent_mask]

    corridor_mask = np.abs(lateral) <= corridor_m
    denominator = int(np.count_nonzero(corridor_mask))
    support = int(np.count_nonzero(extent_mask))
    inlier_ratio = float(support / denominator) if denominator else 0.0
    abs_residual = np.abs(lateral[extent_mask])
    p50 = float(np.percentile(abs_residual, 50)) if support else None
    p90 = float(np.percentile(abs_residual, 90)) if support else None

    if support < min_support or (support and float(np.ptp(extent_along)) < 0.4):
        return {
            **base,
            "status": STATUS_LOW_SUPPORT,
            "reason": (
                f"refined line keeps {support} inliers over "
                f"{0.0 if not support else round(float(np.ptp(extent_along)), 3)} m; "
                f"need >= {min_support} points and >= 0.4 m extent"
            ),
            "start": _point(rough_start), "end": _point(rough_end),
            "supportPointCount": support, "inlierRatio": round(inlier_ratio, 4),
            "residualP50M": None if p50 is None else round(p50, 5),
            "residualP90M": None if p90 is None else round(p90, 5),
            "doubleSided": {"detected": False, "thicknessCandidatesM": []},
        }

    lo, hi = np.percentile(extent_along, [1.0, 99.0])
    fitted_start = center + unit * float(lo)
    fitted_end = center + unit * float(hi)

    peaks = _lateral_peaks(lateral)
    primary = min(peaks, key=lambda item: abs(item["offsetM"])) if peaks else None
    thickness_candidates: list[dict] = []
    competitors: list[dict] = []
    if primary is not None:
        for peak in peaks:
            if peak is primary:
                continue
            separation = abs(peak["offsetM"] - primary["offsetM"])
            if MIN_THICKNESS_M <= separation <= MAX_THICKNESS_M and (
                peak["supportPointCount"] >= 0.2 * max(1, primary["supportPointCount"])
            ):
                thickness_candidates.append(
                    {"thicknessM": round(separation, 5),
                     "offsetM": round(peak["offsetM"], 5),
                     "supportPointCount": peak["supportPointCount"]}
                )
            elif separation > MAX_THICKNESS_M and (
                peak["supportPointCount"] >= 0.5 * max(1, primary["supportPointCount"])
            ):
                competitors.append(
                    {"offsetM": round(peak["offsetM"], 5),
                     "supportPointCount": peak["supportPointCount"]}
                )
    thickness_candidates.sort(key=lambda item: -item["supportPointCount"])

    double_sided: dict = {"detected": bool(thickness_candidates), "thicknessCandidatesM": thickness_candidates}
    if thickness_candidates:
        best = thickness_candidates[0]
        shift = (best["offsetM"] + (0.0 if primary is None else primary["offsetM"])) * 0.5
        double_sided["thicknessM"] = best["thicknessM"]
        double_sided["pairedCenterline"] = {
            "start": _point(fitted_start + normal * shift),
            "end": _point(fitted_end + normal * shift),
        }

    status = STATUS_FIT_OK
    reason = "refined line converged on a supported wall face"
    if competitors:
        status = STATUS_AMBIGUOUS
        offsets = ", ".join(f"{item['offsetM']:+.3f} m ({item['supportPointCount']} pts)" for item in competitors)
        reason = (
            "multiple parallel wall faces beyond pairing distance share the search corridor; "
            f"competing lateral offsets: {offsets}. Narrow the rough line or corridor_m."
        )

    angle = math.atan2(float(unit[1]), float(unit[0])) % math.pi
    return {
        **base,
        "status": status,
        "reason": reason,
        "start": _point(fitted_start),
        "end": _point(fitted_end),
        "lengthM": round(float(np.linalg.norm(fitted_end - fitted_start)), 4),
        "angleDeg": round(math.degrees(angle), 4),
        "supportPointCount": support,
        "inlierRatio": round(inlier_ratio, 4),
        "residualP50M": round(p50, 5),
        "residualP90M": round(p90, 5),
        "doubleSided": double_sided,
        "deviationFromInput": line_deviation(rough_start, rough_end, fitted_start, fitted_end),
    }


def probe_plane_gap(
    index: CaptureIndex,
    start,
    end,
    *,
    floor_z: float | None = None,
    floor_plane: dict | None = None,
    band: tuple[float, float] = (0.5, 2.4),
    corridor_m: float = 0.25,
    bin_m: float = 0.10,
    min_points_per_bin: int = 2,
    min_gap_m: float = 0.25,
    max_points: int = 400_000,
) -> dict:
    """Report occupancy gaps along a (refined) wall line for opening review."""
    line_start = _as_xy(start, "start")
    line_end = _as_xy(end, "end")
    if bin_m <= 0 or min_points_per_bin < 1 or min_gap_m < 0:
        raise ValueError("gap probe parameters are invalid")
    xy, meta = _gather_band_points(
        index, line_start, line_end,
        floor_z=floor_z, floor_plane=floor_plane, band=band,
        half_width_m=corridor_m, along_margin_m=0.0, max_points=max_points,
    )
    length = meta["roughLengthM"]
    unit = meta["roughUnit"]
    bin_count = max(1, int(math.ceil(length / bin_m)))
    counts = np.zeros(bin_count, dtype=np.int64)
    if len(xy):
        along = (xy - line_start) @ unit
        bins = np.clip((along / bin_m).astype(np.int64), 0, bin_count - 1)
        np.add.at(counts, bins, 1)
    empty = counts < min_points_per_bin
    gaps: list[dict] = []
    run_start = None
    for i in range(bin_count + 1):
        is_empty = bool(empty[i]) if i < bin_count else False
        if is_empty and run_start is None:
            run_start = i
        elif not is_empty and run_start is not None:
            gap_lo = run_start * bin_m
            gap_hi = min(i * bin_m, length)
            if gap_hi - gap_lo >= min_gap_m:
                gaps.append({
                    "startM": round(gap_lo, 4),
                    "endM": round(gap_hi, 4),
                    "lengthM": round(gap_hi - gap_lo, 4),
                    "startPoint": _point(line_start + unit * gap_lo),
                    "endPoint": _point(line_start + unit * gap_hi),
                    "touchesLineEnd": run_start == 0 or i == bin_count,
                })
            run_start = None
    occupied_ratio = float(np.count_nonzero(~empty) / bin_count)
    return {
        "kind": "wall-plane-gap-probe",
        "input": {"start": _point(line_start), "end": _point(line_end)},
        "parameters": {
            "band": [float(band[0]), float(band[1])], "corridorM": corridor_m,
            "binM": bin_m, "minPointsPerBin": int(min_points_per_bin), "minGapM": min_gap_m,
            "floorZ": None if floor_z is None else float(floor_z), "floorPlane": floor_plane,
        },
        "lengthM": round(length, 4),
        "binCount": bin_count,
        "supportPointCount": int(len(xy)),
        "occupiedRatio": round(occupied_ratio, 4),
        "gaps": gaps,
        "sampling": {"stride": meta["stride"]},
        "indexFingerprint": index.manifest["indexFingerprint"],
        "authorityRule": "measurement only; opening decisions stay with the Agent",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_pair(value: str, name: str) -> tuple[float, float]:
    parts = value.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"{name} expects two comma-separated numbers")
    return float(parts[0]), float(parts[1])


def _parse_plane(value: str) -> dict:
    parts = value.split(",")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("--floor-plane expects a,b,c")
    return {"a": float(parts[0]), "b": float(parts[1]), "c": float(parts[2])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--index", required=True, type=Path)
        # Values may start with '-'; callers should use --flag=value form.
        p.add_argument("--start", required=True, help="x,y in source plan meters")
        p.add_argument("--end", required=True, help="x,y in source plan meters")
        p.add_argument("--floor-z", type=float)
        p.add_argument("--floor-plane", help="a,b,c for z = a*x + b*y + c")
        p.add_argument("--band", default="0.5,2.4", help="min,max meters above the floor")
        p.add_argument("--max-points", type=int, default=400_000)
        p.add_argument("--output", type=Path)

    refine = sub.add_parser("refine-wall-line", help="robustly refine a rough Agent wall line")
    common(refine)
    refine.add_argument("--corridor", type=float, default=0.35)
    refine.add_argument("--no-robust", action="store_true")
    refine.add_argument("--seed", type=int, default=0)

    probe = sub.add_parser("probe-plane-gap", help="report occupancy gaps along a wall line")
    common(probe)
    probe.add_argument("--corridor", type=float, default=0.25)
    probe.add_argument("--bin", type=float, default=0.10)
    probe.add_argument("--min-bin-points", type=int, default=2)
    probe.add_argument("--min-gap", type=float, default=0.25)

    args = parser.parse_args()
    start = _parse_pair(args.start, "--start")
    end = _parse_pair(args.end, "--end")
    band = _parse_pair(args.band, "--band")
    floor_plane = _parse_plane(args.floor_plane) if args.floor_plane else None
    index = CaptureIndex.open(args.index)

    if args.command == "refine-wall-line":
        result = refine_wall_line(
            index, start, end,
            floor_z=args.floor_z, floor_plane=floor_plane, band=band,
            corridor_m=args.corridor, robust=not args.no_robust,
            max_points=args.max_points, seed=args.seed,
        )
    else:
        result = probe_plane_gap(
            index, start, end,
            floor_z=args.floor_z, floor_plane=floor_plane, band=band,
            corridor_m=args.corridor, bin_m=args.bin,
            min_points_per_bin=args.min_bin_points, min_gap_m=args.min_gap,
            max_points=args.max_points,
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
